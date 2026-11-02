package provision

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"kalinka/supervisor/internal/machine"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/rfkill/rfkilltest"
	"kalinka/supervisor/internal/wifi"
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
		{"no adapter", "", "wlan0", "NetworkManager", Options{Backend: "nm"}, ""},
		{"HTTP without either radio transport", "", "wlan0", "NetworkManager", Options{Backend: "nm", DisableAP: true}, ""},
		{"other adapter", "hci1", "wlan0", "NetworkManager", Options{Backend: "nm"}, ""},
		{"no Wi-Fi", "hci0", "", "NetworkManager", Options{Backend: "nm"}, "no Wi-Fi interface"},
		{"named interface missing", "hci0", "wlan0", "NetworkManager", Options{Backend: "nm", Interface: "wlan1"}, "no Wi-Fi interface"},
		{"generic ifupdown", "hci0", "wlan0", "ifup", Options{Backend: "nm"}, "install network-manager"},
		{"explicit missing DietPi", "hci0", "wlan0", "NetworkManager", Options{Backend: "dietpi"}, "requires /boot/dietpi/dietpi-network"},
		{"simulation needs only the adapter", "hci0", "", "", Options{Backend: "nm", Test: true}, ""},
		{"HTTP simulation needs no adapter", "", "", "", Options{Backend: "nm", Test: true, DisableAP: true}, ""},
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

func TestNetworkManagerCountry(t *testing.T) {
	for _, keep := range []bool{false, true} {
		b, err := New(Options{Backend: "nm", KeepCountry: keep}, nil).backend()
		if err != nil {
			t.Fatal(err)
		}
		if conf := b.(*wifi.NetworkManager).RegulatoryConf; (conf == "") != keep {
			t.Fatalf("keep-country %v gave %q", keep, conf)
		}
	}
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

type fakeRadio struct{ enable error }

func (r *fakeRadio) Enable(context.Context) error     { return r.enable }
func (r *fakeRadio) Disable(context.Context)          {}
func (r *fakeRadio) Events(ctx context.Context) error { <-ctx.Done(); return nil }
func (r *fakeRadio) Close()                           {}
func (r *fakeRadio) open(context.Context, *machine.Machine, string) (peripheral, string, error) {
	return r, "Kalinka-TEST", nil
}

func waitFor(t *testing.T, what string, done func() bool) {
	t.Helper()
	for deadline := time.Now().Add(time.Second); !done(); time.Sleep(time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal(what)
		}
	}
}

func TestBluetoothFailureReachesTheSnapshot(t *testing.T) {
	for _, tc := range []struct {
		name  string
		block rfkilltest.Radio
		want  string
	}{
		{"soft block", rfkilltest.Radio{Type: "bluetooth", Name: "hci0", Soft: true}, "radio_blocked"},
		{"hardware switch", rfkilltest.Radio{Type: "bluetooth", Name: "hci0", Soft: true, Hard: true}, "radio_hard_blocked"},
		{"only another adapter blocked", rfkilltest.Radio{Type: "bluetooth", Name: "hci1", Soft: true}, "unavailable"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var logs bytes.Buffer
			old := slog.Default()
			slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
			defer slog.SetDefault(old)
			m := machine.New(context.Background(), &radioBackend{}, machine.Config{})
			defer m.Close()
			if err := m.Open(); err != nil {
				t.Fatal(err)
			}
			s := New(Options{}, nil)
			s.root = rfkilltest.Stage(t, tc.block)
			s.openRadio = (&fakeRadio{enable: errors.New("set Powered: unavailable: Blocked through rfkill")}).open
			ctx, cancel := context.WithCancel(context.Background())
			done := make(chan struct{})
			go func() { defer close(done); s.serveBluetooth(ctx, m) }()
			waitFor(t, "the failure was not reported", func() bool { return s.Problems()["bluetooth"] != "" })
			cancel()
			<-done
			if got := s.Snapshot()["radio_problems"]; !reflect.DeepEqual(got, map[string]string{"bluetooth": tc.want}) {
				t.Fatalf("snapshot reports %v, want %s", got, tc.want)
			}
			if line := logs.String(); !strings.Contains(line, "Bluetooth setup unavailable; retrying") || !strings.Contains(line, "Blocked through rfkill") {
				t.Fatalf("the log does not say why: %s", line)
			}
		})
	}
}

func TestAdvertisingClearsTheBluetoothProblem(t *testing.T) {
	m := machine.New(context.Background(), &radioBackend{}, machine.Config{})
	defer m.Close()
	if err := m.Open(); err != nil {
		t.Fatal(err)
	}
	s := New(Options{}, nil)
	s.root = t.TempDir()
	s.report(context.Background(), "bluetooth", errors.New("unavailable: bluetoothd stopped"))
	if got := s.Problems()["bluetooth"]; got != "unavailable" {
		t.Fatalf("reported %q", got)
	}
	s.openRadio = (&fakeRadio{}).open
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- s.bluetoothSession(ctx, m) }()
	waitFor(t, "advertising did not clear the report", func() bool { return len(s.Problems()) == 0 })
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
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
