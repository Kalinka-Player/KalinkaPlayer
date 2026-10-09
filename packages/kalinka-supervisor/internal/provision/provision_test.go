package provision

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"kalinka/supervisor/internal/machine"
	"kalinka/supervisor/internal/protocol"
)

func stageHost(t *testing.T, adapter, wifi string) string {
	t.Helper()
	root := t.TempDir()
	if adapter != "" {
		if err := os.MkdirAll(filepath.Join(root, "sys/class/bluetooth", adapter), 0755); err != nil {
			t.Fatal(err)
		}
	}
	if wifi != "" {
		if err := os.MkdirAll(filepath.Join(root, "sys/class/net", wifi, "wireless"), 0755); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func installed(tool string) func(string) (string, error) {
	return func(name string) (string, error) {
		if name == tool {
			return name, nil
		}
		return "", exec.ErrNotFound
	}
}

func TestBlocked(t *testing.T) {
	for _, tc := range []struct {
		name, adapter, wifi, tool string
		o                         Options
		want                      string
	}{
		{"NetworkManager", "hci0", "wlan0", "NetworkManager", Options{Backend: "nm"}, ""},
		{"DietPi", "hci0", "wlan0", "/boot/dietpi/dietpi-network", Options{Backend: "dietpi"}, ""},
		{"no adapter", "", "wlan0", "NetworkManager", Options{Backend: "nm"}, "no Bluetooth adapter hci0"},
		{"other adapter", "hci1", "wlan0", "NetworkManager", Options{Backend: "nm"}, "no Bluetooth adapter hci0"},
		{"no Wi-Fi", "hci0", "", "NetworkManager", Options{Backend: "nm"}, "no Wi-Fi interface"},
		{"named interface missing", "hci0", "wlan0", "NetworkManager", Options{Backend: "nm", Interface: "wlan1"}, "no Wi-Fi interface"},
		{"generic ifupdown", "hci0", "wlan0", "ifup", Options{Backend: "nm"}, "install network-manager"},
		{"explicit missing DietPi", "hci0", "wlan0", "NetworkManager", Options{Backend: "dietpi"}, "requires /boot/dietpi/dietpi-network"},
		{"simulation needs only the adapter", "hci0", "", "", Options{Backend: "nm", Test: true}, ""},
		{"simulation still needs the adapter", "", "", "", Options{Backend: "nm", Test: true}, "no Bluetooth adapter hci0"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tc.o.Adapter = "hci0"
			got := Blocked(tc.o, stageHost(t, tc.adapter, tc.wifi), installed(tc.tool))
			if (tc.want == "") != (got == "") || !strings.Contains(got, tc.want) {
				t.Fatalf("Blocked = %q, want %q", got, tc.want)
			}
		})
	}
}

type joiningWifi struct{ release chan struct{} }

func (w joiningWifi) Address(context.Context) (string, error) { return "", nil }
func (w joiningWifi) Scan(context.Context, string) ([]protocol.Network, error) {
	return nil, nil
}
func (w joiningWifi) Join(ctx context.Context, _ protocol.Command, _ func(byte)) (string, error) {
	select {
	case <-w.release:
	case <-ctx.Done():
	}
	return "", protocol.Timeout
}

func TestChangingNetwork(t *testing.T) {
	s := New(Options{}, nil)
	if s.ChangingNetwork() {
		t.Fatal("stopped service reports a network change")
	}
	s.recovering.Store(true)
	if !s.ChangingNetwork() {
		t.Fatal("startup recovery not reported")
	}
	s.recovering.Store(false)
	w := joiningWifi{make(chan struct{})}
	defer close(w.release)
	m := machine.New(context.Background(), w, machine.Config{Test: true})
	defer m.Close()
	s.machine.Store(m)
	if _, err := m.Tick(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, b := range protocol.Frame([]byte(`{"v":1,"op":"join","ssid":"home","password":"dummy-password","country":"GB"}`)) {
		if err := m.Write("phone", b); err != nil {
			t.Fatal(err)
		}
	}
	if !s.ChangingNetwork() {
		t.Fatal("join not reported")
	}
}

func TestOpenSetup(t *testing.T) {
	s := New(Options{}, nil)
	if s.Available() || s.OpenSetup(time.Minute) != protocol.Unavailable {
		t.Fatal("OpenSetup answered while setup is not running")
	}
	m := machine.New(context.Background(), joiningWifi{}, machine.Config{})
	defer m.Close()
	s.machine.Store(m)
	if !s.Available() {
		t.Fatal("running setup reported unavailable")
	}
	if err := s.OpenSetup(time.Minute); err != nil {
		t.Fatal(err)
	}
	if active, err := m.Tick(context.Background()); !active || err != nil {
		t.Fatalf("setup not opened before its boot grace: active=%v error=%v", active, err)
	}
}

type fakeRadio struct{}

func (fakeRadio) Name() string                 { return "Kalinka-TEST" }
func (fakeRadio) Enable(context.Context) error { return nil }
func (fakeRadio) Disable(context.Context)      {}
func (fakeRadio) Close()                       {}
func (fakeRadio) Events(ctx context.Context) error {
	<-ctx.Done()
	return nil
}

func eventually(t *testing.T, check func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !check() {
		if time.Now().After(deadline) {
			t.Fatal("timed out")
		}
		time.Sleep(time.Millisecond)
	}
}

func simulated() *Service {
	return New(Options{Test: true, TestAddress: "192.0.2.1"}, nil)
}

func TestSetupOpensOnlyOnceItsRadioIsUp(t *testing.T) {
	s := simulated()
	entered := make(chan struct{})
	release := make(chan struct{})
	s.openRadio = func(ctx context.Context, _ *machine.Machine, _ string) (peripheral, error) {
		close(entered)
		select {
		case <-release:
			return fakeRadio{}, nil
		case <-ctx.Done():
			return nil, protocol.Unavailable
		}
	}
	ctx, stop := context.WithCancel(context.Background())
	defer stop()
	done := make(chan error, 1)
	go func() { done <- s.Run(ctx) }()
	<-entered
	if s.Available() || s.OpenSetup(time.Minute) != protocol.Unavailable {
		t.Fatal("setup answered before its radio was up")
	}
	close(release)
	eventually(t, s.Available)
	if err := s.OpenSetup(time.Minute); err != nil {
		t.Fatal(err)
	}
	stop()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if s.Available() {
		t.Fatal("a stopped service reports setup available")
	}
}

func TestWindowSurvivesARestart(t *testing.T) {
	s := simulated()
	s.openRadio = func(context.Context, *machine.Machine, string) (peripheral, error) { return fakeRadio{}, nil }
	start := func() (stop func() error) {
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan error, 1)
		go func() { done <- s.Run(ctx) }()
		eventually(t, s.Available)
		return func() error { cancel(); return <-done }
	}
	stop := start()
	if err := s.OpenSetup(10 * time.Minute); err != nil {
		t.Fatal(err)
	}
	opened := s.machine.Load().Window()
	if err := stop(); err != nil {
		t.Fatal(err)
	}
	stop = start()
	defer stop()
	if got := s.machine.Load().Window(); !got.Equal(opened) {
		t.Fatalf("the restart lost the window: %v, want %v", got, opened)
	}
}

func TestFailedRadioKeepsTheWindow(t *testing.T) {
	s := simulated()
	s.openRadio = func(context.Context, *machine.Machine, string) (peripheral, error) { return nil, protocol.Unavailable }
	until := time.Now().Add(time.Hour)
	s.window = until
	if err := s.Run(context.Background()); err != protocol.Unavailable {
		t.Fatalf("Run without a radio = %v", err)
	}
	if !s.window.Equal(until) || s.Available() {
		t.Fatalf("after the radio failed: window %v, available %v", s.window, s.Available())
	}
}

func TestWedged(t *testing.T) {
	s := New(Options{}, nil)
	now := time.Now()
	if s.Wedged(now, time.Second) {
		t.Fatal("a service that is not ticking is not wedged")
	}
	s.beat.Store(now.UnixNano())
	if s.Wedged(now.Add(time.Second), time.Second) {
		t.Fatal("fresh beat reported wedged")
	}
	if !s.Wedged(now.Add(2*time.Second), time.Second) {
		t.Fatal("stale beat not reported")
	}
}
