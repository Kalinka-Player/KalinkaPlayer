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
