package machine

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/wifi"
	"kalinka/supervisor/internal/wifi/wifitest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeWifi struct {
	mu      sync.Mutex
	address string
	join    func(context.Context, protocol.Command, func(byte)) (string, error)
}

func (f *fakeWifi) Address(context.Context) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.address, nil
}
func (f *fakeWifi) Scan(context.Context, string) ([]protocol.Network, error) {
	return []protocol.Network{{SSID: "home", Signal: -42, Security: "wpa2"}}, nil
}
func (f *fakeWifi) Join(c context.Context, p protocol.Command, r func(byte)) (string, error) {
	return f.join(c, p, r)
}
func send(m *Machine, device, raw string) error {
	for _, b := range protocol.Frame([]byte(raw)) {
		if err := m.Write(device, b); err != nil {
			return err
		}
	}
	return nil
}
func await(t *testing.T, check func() bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for !check() {
		if time.Now().After(deadline) {
			t.Fatal("timed out")
		}
		time.Sleep(time.Millisecond)
	}
}

const joinCommand = `{"v":1,"op":"join","ssid":"home","password":"dummy-password","country":"GB"}`

type enablingWifi struct {
	fakeWifi
	started chan struct{}
}

func (f *enablingWifi) EnableRadio(ctx context.Context) error {
	close(f.started)
	<-ctx.Done()
	return ctx.Err()
}

func TestEnablingWiFiExcludesSetupOperationsAndHonorsCancellation(t *testing.T) {
	f := &enablingWifi{started: make(chan struct{})}
	m := New(context.Background(), f, Config{Test: true})
	defer m.Close()
	_, _ = m.Tick(context.Background())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- m.EnableWiFi(ctx) }()
	select {
	case <-f.started:
	case <-time.After(time.Second):
		t.Fatal("enable never started")
	}
	if !m.ChangingNetwork() {
		t.Fatal("radio change hidden from reboot guard")
	}
	if err := m.EnableWiFi(ctx); err != protocol.Busy {
		t.Fatal("overlapping enable", err)
	}
	for _, raw := range []string{joinCommand, `{"v":1,"op":"scan","country":"GB"}`} {
		if err := m.Command("phone", []byte(raw)); err != protocol.Busy {
			t.Fatal("overlapping setup", err)
		}
	}
	cancel()
	select {
	case err := <-done:
		if err != context.Canceled {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("request cancellation did not stop enabling")
	}
	if m.ChangingNetwork() {
		t.Fatal("busy flag leaked")
	}
	m.Close()
	if err := m.EnableWiFi(context.Background()); err != protocol.Unavailable {
		t.Fatal("closed machine accepted enable")
	}
}

func TestJoinSurvivesDisconnectAndExcludesSecondPhone(t *testing.T) {
	release := make(chan struct{})
	started := make(chan struct{})
	f := &fakeWifi{join: func(ctx context.Context, c protocol.Command, r func(byte)) (string, error) {
		close(started)
		r(protocol.Authenticating)
		select {
		case <-release:
			return "192.0.2.5", nil
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}}
	m := New(context.Background(), f, Config{Test: true})
	defer m.Close()
	_, _ = m.Tick(context.Background())
	if err := send(m, "a", joinCommand); err != nil {
		t.Fatal(err)
	}
	<-started
	if snapshot := m.Snapshot(); snapshot.TargetSSID != "home" {
		t.Fatalf("joining target %q", snapshot.TargetSSID)
	}
	public, _ := json.Marshal(m.Snapshot())
	if strings.Contains(string(public), "dummy-password") {
		t.Fatal("join credentials reached public status")
	}
	m.Disconnected("a")
	if err := send(m, "b", joinCommand); err != protocol.Busy {
		t.Fatal("join owner lost")
	}
	close(release)
	await(t, func() bool { return m.Status()[2] == protocol.Joined })
	if m.Snapshot().TargetSSID != "" {
		t.Fatal("completed join retained its target")
	}
	if err := m.Authorize("b"); err != protocol.Busy {
		t.Fatal("second phone read result")
	}
	if err := send(m, "a", `{"v":1,"op":"complete"}`); err != nil {
		t.Fatal(err)
	}
	if active, err := m.Tick(context.Background()); err != nil || active {
		t.Fatal("handoff did not close")
	}
}
func TestChangeWaitsForRollback(t *testing.T) {
	rollback := make(chan struct{})
	cancelled := make(chan struct{})
	f := &fakeWifi{join: func(ctx context.Context, c protocol.Command, r func(byte)) (string, error) {
		<-ctx.Done()
		close(cancelled)
		<-rollback
		return "", protocol.Timeout
	}}
	m := New(context.Background(), f, Config{Test: true})
	defer m.Close()
	_, _ = m.Tick(context.Background())
	if m.ChangingNetwork() {
		t.Fatal("idle machine reports a network change")
	}
	_ = send(m, "a", joinCommand)
	if !m.ChangingNetwork() {
		t.Fatal("join not reported as a network change")
	}
	if err := send(m, "a", `{"v":1,"op":"change_network"}`); err != nil {
		t.Fatal(err)
	}
	<-cancelled
	if m.Snapshot().TargetSSID != "" {
		t.Fatal("cancelled join retained its target")
	}
	if err := send(m, "a", joinCommand); err != protocol.Busy {
		t.Fatal("new join during rollback")
	}
	if !m.ChangingNetwork() {
		t.Fatal("rollback not reported as a network change")
	}
	close(rollback)
	await(t, func() bool { return m.Status()[2] == protocol.Idle })
	if m.ChangingNetwork() {
		t.Fatal("network change outlived its rollback")
	}
}
func TestOfflinePolicyAndIdentity(t *testing.T) {
	now := time.Unix(1000, 0)
	f := &fakeWifi{}
	marker := t.TempDir() + "/networked"
	identity := t.TempDir() + "/id"
	_ = os.WriteFile(identity, []byte("ee4d496c-3f6c-4388-a02e-f6a2f17829cd"), 0600)
	m := New(context.Background(), f, Config{Now: func() time.Time { return now }, Marker: marker, IdentityFile: identity})
	defer m.Close()
	tick := func(want bool) {
		t.Helper()
		got, err := m.Tick(context.Background())
		if got != want || err != nil {
			t.Fatalf("active=%v error=%v", got, err)
		}
	}
	tick(false)
	now = now.Add(30 * time.Second)
	tick(true)
	f.address = "192.0.2.1"
	tick(false)
	if _, err := os.Stat(marker); err != nil {
		t.Fatal(err)
	}
	if len(m.Identity()) != 16 {
		t.Fatal("identity")
	}
	f.address = ""
	now = now.Add(299 * time.Second)
	tick(false)
	now = now.Add(time.Second)
	tick(true)
}
func TestNetworkPagesMatchPython(t *testing.T) {
	var gold struct{ Pages []string }
	b, _ := os.ReadFile("../../testdata/python-v1.json")
	if err := json.Unmarshal(b, &gold); err != nil {
		t.Fatal(err)
	}
	m := New(context.Background(), &fakeWifi{}, Config{})
	defer m.Close()
	if string(m.Networks()) != gold.Pages[0] {
		t.Fatal("empty page differs")
	}
	m.scanID = 1
	m.scanState = "ready"
	m.networks = []protocol.Network{{SSID: "Café & home", Signal: -42, Security: "wpa2"}, {SSID: string(makeRepeat('"', 32)), Signal: -42, Security: "wpa2"}, {SSID: string(makeRepeat('\\', 32)), Signal: -42, Security: "wpa2"}, {SSID: "fourth", Signal: -42, Security: "wpa2"}}
	for page := 0; page < 2; page++ {
		m.scanPage = page
		if got := string(m.Networks()); got != gold.Pages[page+1] {
			t.Fatalf("page differs: %s", got)
		}
	}
}
func TestStatusFollowsCorePort(t *testing.T) {
	port := uint16(8000)
	m := New(context.Background(), &fakeWifi{}, Config{Test: true, Port: func() uint16 { return port }})
	defer m.Close()
	port = 8123
	if got := binary.BigEndian.Uint16(m.Status()[8:]); got != 8123 {
		t.Fatalf("status port = %d", got)
	}
}
func TestSetupOpensWhenOnlyAnUnroutedCableRemains(t *testing.T) {
	box := wifitest.Stage(t, wifitest.WlanDefault+wifitest.CableSubnet,
		wifitest.Link{Name: "eth0", Flags: wifitest.Up, Physical: true, Addresses: []string{"10.10.10.1/24"}},
		wifitest.Link{Name: "wlan0", Flags: wifitest.Up, Physical: true, Wireless: true, Addresses: []string{"192.168.1.20/24"}})
	backend := wifi.NewNetworkManager("wlan0", t.TempDir())
	backend.AddressFor = box.Address
	now := time.Unix(1000, 0)
	m := New(context.Background(), backend, Config{Now: func() time.Time { return now }})
	defer m.Close()
	tick := func(want bool) {
		t.Helper()
		if got, err := m.Tick(context.Background()); got != want || err != nil {
			t.Fatalf("active=%v error=%v", got, err)
		}
	}
	tick(false)
	if m.Status()[2] != protocol.Online {
		t.Fatal("Wi-Fi with the default route did not count as online")
	}
	box.Route(t, wifitest.CableSubnet)
	box.Unaddress("wlan0")
	now = now.Add(299 * time.Second)
	tick(false)
	now = now.Add(time.Second)
	tick(true)
}
func makeRepeat(b byte, n int) []byte {
	v := make([]byte, n)
	for i := range v {
		v[i] = b
	}
	return v
}

func TestHTTPCommandReleasesExpiredBluetoothFragment(t *testing.T) {
	now := time.Now()
	m := New(context.Background(), &fakeWifi{}, Config{Test: true, Now: func() time.Time { return now }})
	defer m.Close()
	_, _ = m.Tick(context.Background())
	if err := m.Write("ble-phone", []byte{1, '{'}); err != nil {
		t.Fatal(err)
	}
	command := []byte(`{"v":1,"op":"scan","country":"GB"}`)
	if err := m.Command("http:client", command); err != protocol.Busy {
		t.Fatal("HTTP bypassed partial BLE command", err)
	}
	now = now.Add(11 * time.Second)
	if err := m.Command("http:client", command); err != nil {
		t.Fatal("expired BLE frame blocked HTTP", err)
	}
}
