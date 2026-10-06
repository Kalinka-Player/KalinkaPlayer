// Package machine owns setup lifecycle independently of Bluetooth and the OS backend.
package machine

import (
	"context"
	"encoding/hex"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"kalinka/supervisor/internal/protocol"
)

// Wifi keeps cancellation and rollback inside the selected networking backend.
type Wifi interface {
	Address(context.Context) (string, error)
	Scan(context.Context, string) ([]protocol.Network, error)
	Join(context.Context, protocol.Command, func(byte)) (string, error)
}

// Config controls image policy; AlwaysAdvertise is only for explicit local tests.
type Config struct {
	IdentityFile, Marker                 string
	Port                                 uint16
	Test, AlwaysAdvertise                bool
	Now                                  func() time.Time
	BootGrace, LossGrace, HandoffTimeout time.Duration
}

// Machine serializes input and guards all state shared with backend workers.
type Machine struct {
	mu                                         sync.Mutex
	ctx                                        context.Context
	cancel                                     context.CancelFunc
	workers                                    sync.WaitGroup
	wifi                                       Wifi
	cfg                                        Config
	frames                                     protocol.Frames
	state, reason, stage                       byte
	address, owner                             string
	active, seen, completed, closed, resetting bool
	offlineSince, joinedAt, setupUntil         time.Time
	scanState                                  string
	scanReason                                 *string
	scanID, scanPage                           int
	networks                                   []protocol.Network
	operation                                  context.CancelFunc
	done                                       chan struct{}
	Changes                                    chan struct{}
}

func New(parent context.Context, wifi Wifi, cfg Config) *Machine {
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Port == 0 {
		cfg.Port = 8000
	}
	if cfg.BootGrace == 0 {
		cfg.BootGrace = 30 * time.Second
	}
	if cfg.LossGrace == 0 {
		cfg.LossGrace = 300 * time.Second
	}
	if cfg.HandoffTimeout == 0 {
		cfg.HandoffTimeout = 180 * time.Second
	}
	ctx, cancel := context.WithCancel(parent)
	_, err := os.Stat(cfg.Marker)
	return &Machine{ctx: ctx, cancel: cancel, wifi: wifi, cfg: cfg, seen: cfg.Marker != "" && err == nil, offlineSince: cfg.Now(), scanState: "idle", networks: []protocol.Network{}, Changes: make(chan struct{}, 1)}
}
func (m *Machine) changed() {
	select {
	case m.Changes <- struct{}{}:
	default:
	}
}
func (m *Machine) update(state, reason byte, address string) {
	if m.state == state && m.reason == reason && m.address == address {
		return
	}
	slog.Info("Setup state changed", "state", state, "reason", reason)
	m.state = state
	m.reason = reason
	m.address = address
	m.changed()
}
func (m *Machine) Status() []byte {
	m.mu.Lock()
	defer m.mu.Unlock()
	return protocol.Status(m.state, m.reason, m.address, m.cfg.Port, m.cfg.Test)
}
func (m *Machine) Progress() []byte { m.mu.Lock(); defer m.mu.Unlock(); return []byte{1, m.stage} }
func (m *Machine) Identity() []byte {
	f, err := os.Open(m.cfg.IdentityFile)
	if err != nil {
		return []byte{}
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, 65))
	if err != nil || len(b) > 64 {
		return []byte{}
	}
	s := strings.ReplaceAll(strings.TrimSpace(string(b)), "-", "")
	v, err := hex.DecodeString(s)
	if err != nil || len(v) != 16 {
		return []byte{}
	}
	return v
}
func (m *Machine) Networks() []byte {
	m.mu.Lock()
	defer m.mu.Unlock()
	start := min(m.scanPage*3, len(m.networks))
	end := min(start+3, len(m.networks))
	return protocol.JSON(protocol.NetworkPage{Version: 1, ID: m.scanID, State: m.scanState, Reason: m.scanReason, Page: m.scanPage, Pages: max(1, (len(m.networks)+2)/3), Networks: m.networks[start:end]})
}
func (m *Machine) Authorize(device string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.active || m.closed {
		return protocol.Unavailable
	}
	if m.owner != "" && m.owner != device {
		return protocol.Busy
	}
	return nil
}
func (m *Machine) Disconnected(device string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.frames.Owner == device {
		m.frames.Clear()
	}
	if m.owner == device && m.state != protocol.Joining && m.state != protocol.Joined && m.scanState != "scanning" {
		m.owner = ""
	}
}
func (m *Machine) report(stage byte) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.stage != stage {
		m.stage = stage
		slog.Info("Wi-Fi progress", "stage", stage)
		m.changed()
	}
}
func (m *Machine) Write(device string, data []byte) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.active || m.closed {
		return protocol.Unavailable
	}
	if m.owner != "" && m.owner != device || m.resetting {
		return protocol.Busy
	}
	raw, err := m.frames.Feed(device, data, m.cfg.Now())
	if err != nil || raw == nil {
		return err
	}
	defer clear(raw)
	cmd, err := protocol.Decode(raw)
	if err != nil {
		return err
	}
	if cmd.Op == "change_network" {
		m.owner = device
		m.stage = protocol.Preparing
		m.update(protocol.Joining, 0, "")
		m.resetting = true
		if m.operation != nil {
			m.operation()
		}
		previous := m.done
		m.workers.Add(1)
		go func() {
			defer m.workers.Done()
			if previous != nil {
				<-previous
			}
			m.mu.Lock()
			defer m.mu.Unlock()
			m.scanState = "idle"
			m.scanReason = nil
			m.networks = []protocol.Network{}
			m.scanPage = 0
			m.setupUntil = m.cfg.Now().Add(m.cfg.HandoffTimeout)
			m.completed = false
			m.owner = ""
			m.stage = protocol.Preparing
			m.resetting = false
			m.update(protocol.Idle, 0, "")
		}()
		return nil
	}
	if m.state == protocol.Joining {
		cmd.Password.Clear()
		return protocol.Busy
	}
	if cmd.Op == "networks" {
		if cmd.Page >= max(1, (len(m.networks)+2)/3) {
			return protocol.Invalid
		}
		m.scanPage = cmd.Page
		return nil
	}
	if m.scanState == "scanning" {
		cmd.Password.Clear()
		return protocol.Busy
	}
	if cmd.Op == "complete" {
		if m.state != protocol.Joined || m.owner != device {
			return protocol.Invalid
		}
		m.completed = true
		return nil
	}
	if m.state == protocol.Joined {
		cmd.Password.Clear()
		return protocol.Busy
	}
	ctx, cancel := context.WithCancel(m.ctx)
	m.operation = cancel
	done := make(chan struct{})
	m.done = done
	m.owner = device
	m.workers.Add(1)
	if cmd.Op == "scan" {
		m.scanID++
		m.scanPage = 0
		m.networks = []protocol.Network{}
		m.scanState = "scanning"
		m.scanReason = nil
		go func() {
			defer m.workers.Done()
			defer close(done)
			defer cancel()
			ctx, timeout := context.WithTimeout(ctx, 20*time.Second)
			defer timeout()
			networks, err := m.wifi.Scan(ctx, cmd.Country)
			m.mu.Lock()
			defer m.mu.Unlock()
			m.owner = ""
			if err != nil {
				reason := "unavailable"
				m.scanState = "failed"
				m.scanReason = &reason
			} else {
				m.scanState = "ready"
				if networks == nil {
					networks = []protocol.Network{}
				}
				m.networks = networks[:min(len(networks), 30)]
			}
		}()
	} else {
		m.stage = protocol.Preparing
		m.update(protocol.Joining, 0, "")
		go func() {
			defer m.workers.Done()
			defer close(done)
			defer cancel()
			defer cmd.Password.Clear()
			address, err := m.wifi.Join(ctx, cmd, m.report)
			m.mu.Lock()
			defer m.mu.Unlock()
			if err != nil {
				m.owner = ""
				m.update(protocol.Failed, protocol.Reason(err), "")
			} else {
				m.stage = protocol.Connected
				m.joinedAt = m.cfg.Now()
				m.completed = false
				m.update(protocol.Joined, 0, address)
			}
		}()
	}
	return nil
}
func (m *Machine) Tick(ctx context.Context) (bool, error) {
	m.mu.Lock()
	busy := m.state == protocol.Joining || m.scanState == "scanning" || m.resetting
	active := m.active
	m.mu.Unlock()
	if busy {
		return active, nil
	}
	address, err := m.wifi.Address(ctx)
	if err != nil {
		return active, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.closed {
		return false, nil
	}
	if m.state == protocol.Joining || m.scanState == "scanning" || m.resetting {
		return m.active, nil
	}
	now := m.cfg.Now()
	if address != "" && !m.cfg.Test {
		m.seen = true
		if m.cfg.Marker != "" {
			if err := os.MkdirAll(filepath.Dir(m.cfg.Marker), 0700); err != nil {
				return m.active, protocol.Storage
			}
			f, err := os.OpenFile(m.cfg.Marker, os.O_CREATE|os.O_WRONLY, 0600)
			if err != nil {
				return m.active, protocol.Storage
			}
			_ = f.Close()
		}
	}
	if m.state == protocol.Joined {
		if !m.completed && now.Sub(m.joinedAt) < m.cfg.HandoffTimeout {
			return m.active, nil
		}
		m.owner = ""
		m.frames.Clear()
		m.active = false
		state := protocol.Idle
		if address != "" {
			state = protocol.Online
		}
		m.update(state, 0, "")
		m.offlineSince = now
		return false, nil
	}
	if now.Before(m.setupUntil) {
		return m.active, nil
	}
	if address != "" && !m.cfg.Test && !m.cfg.AlwaysAdvertise {
		m.offlineSince = now
		m.owner = ""
		m.frames.Clear()
		m.active = false
		m.update(protocol.Online, 0, "")
	} else {
		grace := m.cfg.BootGrace
		if m.seen {
			grace = m.cfg.LossGrace
		}
		if m.cfg.Test || m.cfg.AlwaysAdvertise || now.Sub(m.offlineSince) >= grace {
			if !m.active {
				m.update(protocol.Idle, 0, "")
			}
			m.active = true
		}
	}
	return m.active, nil
}
func (m *Machine) Close() {
	m.mu.Lock()
	m.closed = true
	m.cancel()
	m.frames.Clear()
	m.mu.Unlock()
	m.workers.Wait()
}
