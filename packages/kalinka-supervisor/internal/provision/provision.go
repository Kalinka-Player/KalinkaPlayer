// Package provision runs nearby BLE Wi-Fi setup as one restartable component of the supervisor.
package provision

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"sync/atomic"
	"time"

	"kalinka/supervisor/internal/gatt"
	"kalinka/supervisor/internal/machine"
	"kalinka/supervisor/internal/wifi"
)

// Options selects the radios, networking backend and policy for nearby setup.
type Options struct {
	Backend, Interface, Adapter, IdentityFile, State, Runtime string
	Test, AlwaysAdvertise                                     bool
	TestResult, TestAddress                                   string
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
}

// New returns a stopped service. port is asked for Core's HTTP port whenever setup reports it.
func New(o Options, port func() uint16) *Service {
	return &Service{opts: o, port: port}
}

// Blocked returns why setup cannot run on this host now, or "" when it can.
// root prefixes the /sys paths so tests can stage a host.
func Blocked(o Options, root string, lookPath func(string) (string, error)) string {
	if _, err := os.Stat(filepath.Join(root, "sys/class/bluetooth", o.Adapter)); err != nil {
		return "no Bluetooth adapter " + o.Adapter
	}
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
		return wifi.NewNetworkManager(o.Interface, o.State), nil
	}
	d, err := wifi.NewDietPi(o.Interface, "/", o.State, o.Runtime)
	if err != nil {
		return nil, err
	}
	return d, nil
}

// Run recovers any interrupted network change, then serves setup over BLE
// while the host is offline. It returns nil when ctx ends.
func (s *Service) Run(ctx context.Context) error {
	o := s.opts
	backend, err := s.backend()
	if err != nil {
		return err
	}
	defer backend.Close()
	s.recovering.Store(true)
	recovery, cancel := context.WithTimeout(ctx, 75*time.Second)
	err = backend.Recover(recovery)
	cancel()
	s.recovering.Store(false)
	if err != nil {
		return err
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
	radio, err := gatt.New(ctx, m, o.Adapter)
	if err != nil {
		return err
	}
	defer radio.Close()
	eventsCtx, stopEvents := context.WithCancel(ctx)
	defer stopEvents()
	events := make(chan error, 1)
	go func() { events <- radio.Events(eventsCtx) }()
	defer s.beat.Store(0)
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	advertising := false
	for {
		s.beat.Store(time.Now().UnixNano())
		active, err := m.Tick(ctx)
		if err != nil {
			return err
		}
		if active && !advertising {
			if err = radio.Enable(ctx); err != nil {
				return err
			}
			slog.Info("Nearby setup available", "name", radio.Name)
			advertising = true
		} else if !active && advertising {
			radio.Disable(ctx)
			advertising = false
			slog.Info("Nearby setup closed")
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
