// Package provision coordinates Bluetooth and hotspot network setup independently of Core.
package provision

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"kalinka/supervisor/internal/coreconf"
	"kalinka/supervisor/internal/gatt"
	"kalinka/supervisor/internal/machine"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/rfkill"
	"kalinka/supervisor/internal/wifi"
)

// Options selects the radios, networking backend and policy for nearby setup.
type Options struct {
	Backend, Interface, Adapter, IdentityFile, State, Runtime string
	// RadioRecovery is off, first-run, or always. Only offline setup may recover radios.
	RadioRecovery                                string
	Test, AlwaysAdvertise, DisableAP, DisableBLE bool
	TestResult, TestAddress                      string
	// KeepCountry leaves the host's Wi-Fi country alone, as a laptop under test needs.
	KeepCountry bool
}

// Service runs nearby setup until it fails or its context ends. Run is called
// by one goroutine at a time; ChangingNetwork and Wedged are safe from any
// goroutine.
type Service struct {
	opts       Options
	port       func() uint16
	machine    atomic.Pointer[machine.Machine]
	recovering atomic.Bool
	beat       atomic.Int64
	hotspot    atomic.Pointer[wifi.Hotspot]
	// Root prefixes /sys so tests can stage Bluetooth radio blocks.
	root string
	// openRadio is gatt.New, which tests replace so no session needs BlueZ.
	openRadio func(context.Context, *machine.Machine, string) (peripheral, string, error)
	mu        sync.Mutex
	problems  map[string]string
}

// peripheral is the part of gatt.Bluez a Bluetooth session drives.
type peripheral interface {
	Enable(context.Context) error
	Disable(context.Context)
	Events(context.Context) error
	Close()
}

func openBluez(ctx context.Context, m *machine.Machine, adapter string) (peripheral, string, error) {
	b, err := gatt.New(ctx, m, adapter)
	if err != nil {
		return nil, "", err
	}
	return b, b.Name, nil
}

// New returns a stopped service. port is asked for Core's HTTP port whenever setup reports it.
func New(o Options, port func() uint16) *Service {
	if o.Adapter == "" {
		o.Adapter = "hci0"
	}
	return &Service{opts: o, port: port, root: "/", openRadio: openBluez, problems: map[string]string{}}
}

// Blocked returns why setup cannot run on this host now, or "" when it can.
// root prefixes the /sys paths so tests can stage a host.
func Blocked(o Options, root string, lookPath func(string) (string, error)) string {
	if o.Test {
		return ""
	}
	pattern := filepath.Join(root, "sys/class/net/*/wireless")
	if o.Interface != "" {
		pattern = filepath.Join(root, "sys/class/net", o.Interface, "wireless")
	}
	if found, _ := filepath.Glob(pattern); len(found) == 0 {
		return "no Wi-Fi interface"
	}
	if o.Backend == "dietpi" {
		if _, err := lookPath("/boot/dietpi/dietpi-network"); err != nil {
			return "The DietPi backend requires /boot/dietpi/dietpi-network."
		}
	} else if _, err := lookPath("NetworkManager"); err != nil {
		return "NetworkManager is required outside DietPi; install network-manager. Generic ifupdown is not supported."
	}
	return ""
}

// ChangingNetwork reports whether a join, its rollback, or startup recovery of an interrupted one is underway.
func (s *Service) ChangingNetwork() bool {
	if s.recovering.Load() {
		return true
	}
	m := s.machine.Load()
	return m != nil && m.ChangingNetwork()
}

// Wedged reports whether the setup loop has stopped ticking for longer than limit. Startup and a stopped service are not wedged.
func (s *Service) Wedged(now time.Time, limit time.Duration) bool {
	beat := s.beat.Load()
	return beat != 0 && now.Sub(time.Unix(0, beat)) > limit
}

func (s *Service) backend() (wifi.Backend, error) {
	o := s.opts
	switch {
	case o.Test:
		return &wifi.DryRun{Result: o.TestResult, IP: o.TestAddress, Delay: time.Second}, nil
	case o.Backend == "nm":
		n := wifi.NewNetworkManager(o.Interface, o.State)
		n.Runtime = o.Runtime
		if o.KeepCountry {
			n.RegulatoryConf = ""
		}
		return n, nil
	}
	d, err := wifi.NewDietPi(o.Interface, "/", o.State, o.Runtime)
	if err != nil {
		return nil, err
	}
	return d, nil
}

// Run recovers interrupted networking, then shares setup across Bluetooth and HTTP.
// The fallback hotspot follows the offline policy. Run returns nil when ctx ends.
func (s *Service) Run(ctx context.Context) error {
	o := s.opts
	backend, err := s.backend()
	if err != nil {
		return err
	}
	defer func() { _ = backend.Close() }()
	s.recovering.Store(true)
	recovery, cancel := context.WithTimeout(ctx, 75*time.Second)
	err = backend.Recover(recovery)
	cancel()
	s.recovering.Store(false)
	if err != nil {
		return err
	}
	if !o.DisableAP && !o.Test {
		if ap, ok := backend.(wifi.AccessPoint); ok {
			identity, _ := os.ReadFile("/etc/machine-id")
			sum := sha256.Sum256(identity)
			h := wifi.NewHotspot(backend, ap, fmt.Sprintf("Kalinka-Setup-%X", sum[:2]))
			backend = h
			s.hotspot.Store(h)
			defer s.hotspot.Store(nil)
		}
	}
	marker := filepath.Join(o.State, "networked")
	if o.Test || o.AlwaysAdvertise {
		marker = ""
	}
	if marker != "" {
		if _, err = os.Stat("/var/lib/kalinka-image/networked"); err == nil {
			if err = wifi.AtomicWrite(marker, nil, 0600); err != nil {
				return err
			}
		}
	}
	m := machine.New(ctx, backend, machine.Config{IdentityFile: o.IdentityFile, Marker: marker, Port: s.port, Test: o.Test, AlwaysAdvertise: o.AlwaysAdvertise})
	s.machine.Store(m)
	defer s.machine.Store(nil)
	// Closing waits out a rollback, which must stay visible to ChangingNetwork until then.
	defer m.Close()
	if !o.DisableBLE {
		bleCtx, stopBLE := context.WithCancel(ctx)
		defer stopBLE()
		bleDone := make(chan struct{})
		defer func() { stopBLE(); <-bleDone }()
		go func() { defer close(bleDone); s.serveBluetooth(bleCtx, m) }()
	}
	radios := newRadioRecovery(s.radioSteps(m)...)
	defer radios.close()
	defer s.beat.Store(0)
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		s.beat.Store(time.Now().UnixNano())
		if err := s.tick(ctx, m, backend, radios); err != nil {
			return err
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}

func (s *Service) tick(ctx context.Context, m *machine.Machine, backend machine.Wifi, radios *radioRecovery) error {
	active, err := m.Tick(ctx)
	if err != nil {
		return err
	}
	address, err := backend.Address(ctx)
	if err != nil {
		// An unknown address must not start recovery or cancel a join in progress.
		return nil
	}
	allowed := active && address == "" && !s.opts.Test && !s.opts.AlwaysAdvertise &&
		(s.opts.RadioRecovery == "always" || s.opts.RadioRecovery == "first-run" && m.FirstRun())
	ready := radios.reconcile(ctx, allowed)
	if h := s.hotspot.Load(); h != nil {
		if err := h.Reconcile(ctx, ready && active && m.Snapshot().State != protocol.Joined && address == ""); err != nil {
			slog.Warn("Wi-Fi hotspot unavailable", "error", err)
		}
	}
	return nil
}

// radioSteps is called only by the opted-in offline setup recovery worker.
func (s *Service) radioSteps(m *machine.Machine) []func(context.Context) error {
	var steps []func(context.Context) error
	if !s.opts.DisableAP {
		steps = append(steps, s.reported("wifi", m.EnableWiFi, s.forget))
	}
	if !s.opts.DisableBLE {
		// A cleared block says nothing of bluetoothd; only advertising proves Bluetooth works.
		steps = append(steps, s.reported("bluetooth", func(ctx context.Context) error {
			if err := ctx.Err(); err != nil {
				return err
			}
			_, _, err := rfkill.Unblock(s.root, "bluetooth", s.opts.Adapter)
			return err
		}, s.forgetBlock))
	}
	return steps
}

// reported names a recovery step's radio in the log; works is what its success proves.
func (s *Service) reported(radio string, step func(context.Context) error, works func(string)) func(context.Context) error {
	return func(ctx context.Context) error {
		err := step(ctx)
		if err == nil {
			works(radio)
			return nil
		}
		s.report(ctx, radio, err)
		return fmt.Errorf("%s: %w", radio, err)
	}
}

// report keeps only a fixed code for clients. A cancelled or refused attempt,
// or one that met setup stopping, says nothing about the radio; one that ran
// out of its caller's time does, though the call inside may have seen that as
// cancellation.
func (s *Service) report(ctx context.Context, radio string, err error) {
	if err == nil {
		s.forget(radio)
		return
	}
	if errors.Is(ctx.Err(), context.Canceled) || (ctx.Err() == nil && errors.Is(err, context.Canceled)) ||
		errors.Is(err, protocol.Busy) || errors.Is(err, machine.ErrClosed) {
		return
	}
	code := s.problemCode(radio, err)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.problems[radio] = code
}

func (s *Service) forget(radio string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.problems, radio)
}

func (s *Service) forgetBlock(radio string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if code := s.problems[radio]; code == "radio_blocked" || code == "radio_hard_blocked" {
		delete(s.problems, radio)
	}
}

func (s *Service) problemCode(radio string, err error) string {
	var soft, hard bool
	if radio == "bluetooth" {
		soft, hard, _ = rfkill.Blocked(s.root, "bluetooth", s.opts.Adapter)
	}
	switch {
	case hard || errors.Is(err, rfkill.ErrHardBlocked):
		return "radio_hard_blocked"
	case soft || errors.Is(err, rfkill.ErrSoftBlocked):
		return "radio_blocked"
	}
	return "unavailable"
}

// Problems maps each radio whose last attempt failed, "bluetooth" or "wifi",
// to radio_blocked, radio_hard_blocked or unavailable, until that radio next
// works. Safe from any goroutine.
func (s *Service) Problems() map[string]string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return maps.Clone(s.problems)
}

// EnableWiFi is an explicit request from the trusted LAN, including over Ethernet.
func (s *Service) EnableWiFi(ctx context.Context) error {
	if m := s.machine.Load(); m != nil {
		err := m.EnableWiFi(ctx)
		s.report(ctx, "wifi", err)
		if err != nil {
			slog.Warn("Could not enable Wi-Fi", "error", err)
		}
		return err
	}
	return protocol.Unavailable
}

// APActive reports whether the setup-only HTTP listener should be available.
func (s *Service) APActive() bool { h := s.hotspot.Load(); return h != nil && h.Active() }

// Snapshot exposes setup status even when a radio is absent or recovering.
func (s *Service) Snapshot() map[string]any {
	result := map[string]any{"available": false, "hotspot": s.APActive(), "server_id": coreconf.ReadID(s.opts.IdentityFile), "radio_problems": s.Problems()}
	if m := s.machine.Load(); m != nil {
		result["available"] = true
		result["setup"] = m.Snapshot()
	}
	if h := s.hotspot.Load(); h != nil {
		result["hotspot_name"] = h.Name
		result["networks_cached"] = h.Active()
	}
	return result
}

func (s *Service) Open() error {
	if m := s.machine.Load(); m != nil {
		return m.Open()
	}
	return protocol.Unavailable
}

func (s *Service) Command(owner string, raw []byte) error {
	if m := s.machine.Load(); m != nil {
		return m.Command(owner, raw)
	}
	return protocol.Unavailable
}

func (s *Service) serveBluetooth(ctx context.Context, m *machine.Machine) {
	for ctx.Err() == nil {
		if err := s.bluetoothSession(ctx, m); err != nil && ctx.Err() == nil {
			slog.Warn("Bluetooth setup unavailable; retrying", "error", err)
			s.report(ctx, "bluetooth", err)
		}
		_ = wifi.Pause(ctx, 10*time.Second)
	}
}

func (s *Service) bluetoothSession(ctx context.Context, m *machine.Machine) error {
	radio, name, err := s.openRadio(ctx, m, s.opts.Adapter)
	if err != nil {
		return err
	}
	defer radio.Close()
	eventsCtx, stop := context.WithCancel(ctx)
	defer stop()
	events := make(chan error, 1)
	go func() { events <- radio.Events(eventsCtx) }()
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	advertising := false
	for {
		active := m.Snapshot().Active
		if active && !advertising {
			if err := radio.Enable(ctx); err != nil {
				return err
			}
			s.forget("bluetooth")
			slog.Info("Nearby setup available", "name", name)
			advertising = true
		} else if !active && advertising {
			radio.Disable(ctx)
			advertising = false
		}
		select {
		case <-ctx.Done():
			return nil
		case err := <-events:
			return err
		case <-ticker.C:
		}
	}
}
