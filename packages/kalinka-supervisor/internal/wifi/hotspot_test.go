package wifi

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"kalinka/supervisor/internal/protocol"
)

type hotspotFake struct {
	active               bool
	address              string
	scans, starts, stops int
	countries            []string
	joinError            error
	hold, joining        chan struct{}
}

func (f *hotspotFake) Address(context.Context) (string, error) {
	if f.active {
		return APAddress, nil
	}
	return f.address, nil
}
func (f *hotspotFake) Scan(_ context.Context, country string) ([]protocol.Network, error) {
	f.scans++
	f.countries = append(f.countries, country)
	return []protocol.Network{{SSID: "Home", Security: "wpa2"}}, nil
}
func (f *hotspotFake) Join(context.Context, protocol.Command, func(byte)) (string, error) {
	if f.active {
		panic("joining with AP still running")
	}
	if f.joining != nil {
		close(f.joining)
		<-f.hold
	}
	if f.joinError != nil {
		return "", f.joinError
	}
	f.address = "192.168.1.9"
	return f.address, nil
}
func (*hotspotFake) Recover(context.Context) error           { return nil }
func (*hotspotFake) Close() error                            { return nil }
func (f *hotspotFake) StartAP(context.Context, string) error { f.active = true; f.starts++; return nil }
func (f *hotspotFake) StopAP(context.Context) error          { f.active = false; f.stops++; return nil }
func (*hotspotFake) APInterface() string                     { return "wlan0" }
func TestHotspotDoesNotReplaceWorkingWiredOrWiFi(t *testing.T) {
	f := &hotspotFake{address: "192.168.1.8"}
	h := NewHotspot(f, f, "Kalinka-Setup-ABCD")
	if err := h.Reconcile(context.Background(), true); err != nil || f.starts != 0 {
		t.Fatalf("replaced online connection: %v", err)
	}
}
func TestHotspotScanJoinAndRecovery(t *testing.T) {
	for _, failure := range []bool{false, true} {
		t.Run(map[bool]string{false: "joined", true: "wrong_password"}[failure], func(t *testing.T) {
			ctx := context.Background()
			f := &hotspotFake{}
			if failure {
				f.joinError = protocol.WrongPassword
			}
			h := NewHotspot(f, f, "Kalinka-Setup-ABCD")
			h.JoinDelay = 0
			h.AddressWithoutAP = func(context.Context, string) (string, error) { return "", nil }
			h.HasClients = func(context.Context, string) bool { return true }
			h.NearbyCountry = func(context.Context, string) string { return "" }
			if err := h.Reconcile(ctx, true); err != nil || !h.Active() {
				t.Fatal("AP unavailable", err)
			}
			if address, _ := h.Address(ctx); address != "" {
				t.Fatal("AP counted as online")
			}
			networks, err := h.Scan(ctx, "GB")
			if err != nil || len(networks) != 1 || f.scans != 1 {
				t.Fatal("single radio scan cache lost")
			}
			networks[0].SSID = "changed"
			networks, _ = h.Scan(ctx, "GB")
			if networks[0].SSID != "Home" {
				t.Fatal("scan cache mutable")
			}
			_, err = h.Join(ctx, protocol.Command{}, func(byte) {})
			if failure {
				if err != protocol.WrongPassword || !h.Active() || f.starts != 2 {
					t.Fatal("failed join did not reopen AP", err)
				}
			} else {
				if err != nil || h.Active() || f.starts != 1 {
					t.Fatal("successful join left AP running", err)
				}
			}
		})
	}
}
func TestHotspotRescansUnderANewCountryWithNobodyOnIt(t *testing.T) {
	ctx := context.Background()
	f := &hotspotFake{}
	h := NewHotspot(f, f, "Kalinka-Setup-ABCD")
	h.AddressWithoutAP = func(context.Context, string) (string, error) { return "", nil }
	h.HasClients = func(context.Context, string) bool { return false }
	h.NearbyCountry = func(context.Context, string) string { return "" }
	if err := h.Reconcile(ctx, true); err != nil || !h.Active() {
		t.Fatal("AP unavailable", err)
	}
	if _, err := h.Scan(ctx, "GB"); err != nil || h.Active() || !slices.Equal(f.countries, []string{"", "GB"}) {
		t.Fatalf("scans %q with AP active %v: %v", f.countries, h.Active(), err)
	}
	if err := h.Reconcile(ctx, true); err != nil || !h.Active() || !slices.Equal(f.countries, []string{"", "GB", "GB"}) {
		t.Fatalf("AP reopened after scans %q: %v", f.countries, err)
	}
	if _, err := h.Scan(ctx, "GB"); err != nil || !h.Active() || len(f.countries) != 3 {
		t.Fatalf("rescanned a list already under GB: %q", f.countries)
	}
}
func TestHotspotListsUnderTheCountryNearbyAccessPointsAnnounce(t *testing.T) {
	ctx := context.Background()
	f := &hotspotFake{}
	h := NewHotspot(f, f, "Kalinka-Setup-ABCD")
	h.AddressWithoutAP = func(context.Context, string) (string, error) { return "", nil }
	h.HasClients = func(context.Context, string) bool { return true }
	h.NearbyCountry = func(context.Context, string) string { return "DE" }
	if err := h.Reconcile(ctx, true); err != nil || !h.Active() || !slices.Equal(f.countries, []string{"", "DE"}) {
		t.Fatalf("scans %q before the AP: %v", f.countries, err)
	}
	if _, err := h.Scan(ctx, "DE"); err != nil || len(f.countries) != 2 {
		t.Fatalf("the list under DE was not the one kept: %q", f.countries)
	}
}
func TestHotspotReconcileDoesNotInterruptJoin(t *testing.T) {
	ctx := context.Background()
	f := &hotspotFake{joining: make(chan struct{}), hold: make(chan struct{})}
	h := NewHotspot(f, f, "Kalinka-Setup-ABCD")
	h.JoinDelay = 0
	done := make(chan struct{})
	go func() { defer close(done); _, _ = h.Join(ctx, protocol.Command{}, func(byte) {}) }()
	<-f.joining
	if err := h.Reconcile(ctx, true); err != nil {
		t.Fatal(err)
	}
	close(f.hold)
	<-done
	if f.starts != 0 {
		t.Fatal("AP started in middle of join")
	}
}

type fakeAPProcess struct{ stopped bool }

func (p *fakeAPProcess) Stop()       { p.stopped = true }
func (p *fakeAPProcess) Alive() bool { return !p.stopped }
func TestDietPiAPKeepsSavedFilesAndRecoversCrash(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	state := t.TempDir()
	runtime := t.TempDir()
	d, _ := NewDietPi("wlan0", root, state, runtime)
	if err := AtomicWrite(d.Conf, []byte("saved network"), 0600); err != nil {
		t.Fatal(err)
	}
	var calls []string
	d.Run = func(_ context.Context, _ time.Duration, name string, args ...string) (string, error) {
		calls = append(calls, name+" "+strings.Join(args, " "))
		return "", nil
	}
	var processes []*fakeAPProcess
	d.StartAPProcess = func(_ context.Context, name string, args ...string) (APProcess, error) {
		if strings.Contains(strings.Join(args, " "), APPassword) {
			t.Fatal("password in process arguments")
		}
		p := &fakeAPProcess{}
		processes = append(processes, p)
		return p, nil
	}
	if err := d.StartAP(ctx, "Kalinka-Setup-ABCD"); err != nil {
		t.Fatal(err)
	}
	if len(processes) != 2 {
		t.Fatal("missing AP or DHCP/DNS process")
	}
	raw, _ := os.ReadFile(d.Conf)
	if string(raw) != "saved network" {
		t.Fatal("saved config changed")
	}
	dns, _ := os.ReadFile(filepath.Join(runtime, "hotspot-dnsmasq.conf"))
	for _, line := range []string{"address=/#/192.168.4.1", "bind-interfaces", "except-interface=lo", "dhcp-option=6,192.168.4.1", "no-resolv"} {
		if !strings.Contains(string(dns), line) {
			t.Fatal("missing captive DNS setting", line)
		}
	}
	if binary, err := exec.LookPath("dnsmasq"); err == nil {
		if out, err := exec.Command(binary, "--test", "--conf-file="+filepath.Join(runtime, "hotspot-dnsmasq.conf")).CombinedOutput(); err != nil {
			t.Fatalf("dnsmasq rejected captive configuration: %s", out)
		}
	}
	if err := d.StopAP(ctx); err != nil {
		t.Fatal(err)
	}
	for _, p := range processes {
		if !p.stopped {
			t.Fatal("daemon left running")
		}
	}
	if err := AtomicWrite(d.apRecord(), []byte("wlan0"), 0600); err != nil {
		t.Fatal(err)
	}
	calls = nil
	restarted, _ := NewDietPi("wlan0", root, state, runtime)
	restarted.Run = d.Run
	if err := restarted.Recover(ctx); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(calls, "\n"), "iw dev wlan0 set type managed") || !strings.Contains(strings.Join(calls, "\n"), "kalinka-wifi@wlan0.service") {
		t.Fatal("crash left interface in AP mode")
	}
}
