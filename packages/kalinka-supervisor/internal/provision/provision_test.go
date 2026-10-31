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

// clockedBox runs Run's tick every two seconds on a fake clock and records
// each time the radios are unblocked.
type clockedBox struct {
	t        *testing.T
	address  string
	boot     time.Time
	now      time.Time
	s        *Service
	m        *machine.Machine
	radios   offlineRadios
	unblocks []unblock
}

type unblock struct {
	at           time.Duration
	setupWasOpen bool
}

func newClockedBox(t *testing.T, address string) *clockedBox {
	b := &clockedBox{t: t, address: address, boot: time.Unix(1_700_000_000, 0), s: New(Options{}, nil)}
	b.now = b.boot
	b.m = machine.New(context.Background(), b, machine.Config{Now: func() time.Time { return b.now }})
	t.Cleanup(b.m.Close)
	b.radios = offlineRadios{settle: radioSettle, unblock: func() {
		b.unblocks = append(b.unblocks, unblock{b.now.Sub(b.boot), b.m.Snapshot().Active})
	}}
	return b
}

func (b *clockedBox) Address(context.Context) (string, error) { return b.address, nil }
func (b *clockedBox) Scan(context.Context, string) ([]protocol.Network, error) {
	return nil, nil
}
func (b *clockedBox) Join(context.Context, protocol.Command, func(byte)) (string, error) {
	return "", protocol.Timeout
}

func (b *clockedBox) run(d time.Duration) {
	b.t.Helper()
	for end := b.now.Add(d); b.now.Before(end); b.now = b.now.Add(2 * time.Second) {
		if err := b.s.tick(context.Background(), b.m, b, &b.radios, b.now); err != nil {
			b.t.Fatal(err)
		}
	}
}

func TestRadiosUnblockBeforeSetupOpens(t *testing.T) {
	b := newClockedBox(t, "")
	b.run(40 * time.Second)
	if len(b.unblocks) != 1 || b.unblocks[0].setupWasOpen || b.unblocks[0].at >= 30*time.Second {
		t.Fatalf("unblocks %+v", b.unblocks)
	}
	if !b.m.Snapshot().Active {
		t.Fatal("setup never opened")
	}
}

func TestRadiosUnblockOncePerSpellOffline(t *testing.T) {
	b := newClockedBox(t, "192.0.2.10")
	b.run(time.Minute)
	b.address = ""
	b.run(10 * time.Minute)
	if len(b.unblocks) != 1 || b.unblocks[0].setupWasOpen || b.unblocks[0].at > time.Minute+radioSettle {
		t.Fatalf("after going offline, unblocks %+v", b.unblocks)
	}
	if !b.m.Snapshot().Active {
		t.Fatal("setup never opened")
	}
	b.address = "192.0.2.10"
	b.run(time.Minute)
	b.address = ""
	b.run(10 * time.Minute)
	if len(b.unblocks) != 2 {
		t.Fatalf("after going offline again, unblocks %+v", b.unblocks)
	}
}

func TestRadiosLeftAloneOnline(t *testing.T) {
	for _, tc := range []struct {
		name    string
		leaseIn time.Duration
	}{{"cable up at start", 0}, {"cable's lease a few seconds after start", 6 * time.Second}} {
		t.Run(tc.name, func(t *testing.T) {
			b := newClockedBox(t, "")
			b.run(tc.leaseIn)
			b.address = "192.0.2.10"
			b.run(10 * time.Minute)
			if len(b.unblocks) != 0 {
				t.Fatalf("unblocks %+v", b.unblocks)
			}
		})
	}
}

func TestRadiosLeftAloneWhileSetupIsOpenFromTheLAN(t *testing.T) {
	b := newClockedBox(t, "192.0.2.10")
	b.run(time.Minute)
	if err := b.m.Open(); err != nil {
		t.Fatal(err)
	}
	b.run(time.Minute)
	if len(b.unblocks) != 0 {
		t.Fatalf("unblocks %+v", b.unblocks)
	}
}

type countingSwitch struct{ calls int }

func (c *countingSwitch) EnableRadio(context.Context) error { c.calls++; return nil }

func TestUnblockRadios(t *testing.T) {
	for _, tc := range []struct {
		name       string
		disableBLE bool
		wifiSwitch *countingSwitch
	}{
		{"NetworkManager", false, &countingSwitch{}},
		{"Bluetooth setup off", true, &countingSwitch{}},
		{"no Wi-Fi switch, as on DietPi", false, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := New(Options{DisableBLE: tc.disableBLE}, nil)
			s.root = rfkilltest.Stage(t, rfkilltest.Radio{Type: "bluetooth", Name: "hci0", Soft: true}, rfkilltest.Radio{Type: "wlan", Name: "phy0", Soft: true})
			var wifiSwitch radioSwitch
			if tc.wifiSwitch != nil {
				wifiSwitch = tc.wifiSwitch
			}
			s.unblockRadios(context.Background(), wifiSwitch)
			if tc.wifiSwitch != nil && tc.wifiSwitch.calls != 1 {
				t.Fatalf("Wi-Fi switched on %d times", tc.wifiSwitch.calls)
			}
			if rfkilltest.Soft(t, s.root, "rfkill0") != tc.disableBLE {
				t.Fatal("Bluetooth's block not as Options asked")
			}
			if !rfkilltest.Soft(t, s.root, "rfkill1") {
				t.Fatal("Wi-Fi's kernel block cleared ahead of its backend")
			}
		})
	}
}

// hotspotBackend is an offline backend that owns the Wi-Fi switch and can serve the hotspot.
type hotspotBackend struct{ switchedOn chan struct{} }

func (hotspotBackend) Address(context.Context) (string, error) { return "", nil }
func (hotspotBackend) Scan(context.Context, string) ([]protocol.Network, error) {
	return nil, nil
}
func (hotspotBackend) Join(context.Context, protocol.Command, func(byte)) (string, error) {
	return "", protocol.Timeout
}
func (hotspotBackend) Recover(context.Context) error         { return nil }
func (hotspotBackend) Close() error                          { return nil }
func (hotspotBackend) StartAP(context.Context, string) error { return nil }
func (hotspotBackend) StopAP(context.Context) error          { return nil }
func (hotspotBackend) APInterface() string                   { return "wlan0" }
func (b hotspotBackend) EnableRadio(context.Context) error {
	select {
	case b.switchedOn <- struct{}{}:
	default:
	}
	return nil
}

func TestRunSwitchesWiFiOnBehindTheHotspot(t *testing.T) {
	b := hotspotBackend{make(chan struct{}, 1)}
	s := New(Options{Backend: "nm", DisableBLE: true, State: t.TempDir()}, nil)
	s.open = func() (wifi.Backend, error) { return b, nil }
	s.root = t.TempDir()
	s.settle = 0
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- s.Run(ctx) }()
	select {
	case <-b.switchedOn:
	case <-time.After(10 * time.Second):
		t.Fatal("Wi-Fi never switched on")
	}
	if s.hotspot.Load() == nil {
		t.Fatal("the backend was not wrapped in the hotspot")
	}
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
