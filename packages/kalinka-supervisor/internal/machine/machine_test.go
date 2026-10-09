package machine

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/wifi"
	"kalinka/supervisor/internal/wifi/wifitest"
	"os"
	"sync"
	"testing"
	"time"
)

type fakeWifi struct {
	mu      sync.Mutex
	address string
	scan    func(context.Context) ([]protocol.Network, error)
	join    func(context.Context, protocol.Command, func(byte)) (string, error)
}

func (f *fakeWifi) Address(context.Context) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.address, nil
}
func (f *fakeWifi) Scan(ctx context.Context, _ string) ([]protocol.Network, error) {
	if f.scan != nil {
		return f.scan(ctx)
	}
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
	m.Disconnected("a")
	if err := send(m, "b", joinCommand); err != protocol.Busy {
		t.Fatal("join owner lost")
	}
	close(release)
	await(t, func() bool { return m.Status()[2] == protocol.Joined })
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
func TestOpenWhileOnline(t *testing.T) {
	now := time.Unix(1000, 0)
	m := New(context.Background(), &fakeWifi{address: "192.0.2.1"}, Config{Now: func() time.Time { return now }})
	defer m.Close()
	tick := func(active bool, state byte) {
		t.Helper()
		got, err := m.Tick(context.Background())
		if got != active || err != nil || m.Status()[2] != state {
			t.Fatalf("active=%v state=%d error=%v", got, m.Status()[2], err)
		}
	}
	tick(false, protocol.Online)
	if err := m.Open(10 * time.Minute); err != nil {
		t.Fatal(err)
	}
	tick(true, protocol.Idle)
	now = now.Add(10*time.Minute - time.Second)
	tick(true, protocol.Idle)
	now = now.Add(time.Second)
	tick(false, protocol.Online)
}
func TestOpenWaitsForPhones(t *testing.T) {
	scanned := make(chan struct{})
	joined := make(chan struct{})
	rollback := make(chan struct{})
	f := &fakeWifi{
		scan: func(ctx context.Context) ([]protocol.Network, error) {
			select {
			case <-scanned:
			case <-ctx.Done():
			}
			return nil, nil
		},
		join: func(ctx context.Context, _ protocol.Command, _ func(byte)) (string, error) {
			select {
			case <-joined:
				return "192.0.2.5", nil
			case <-ctx.Done():
			}
			<-rollback
			return "", protocol.Timeout
		},
	}
	m := New(context.Background(), f, Config{Test: true})
	defer m.Close()
	_, _ = m.Tick(context.Background())
	refused := func(during string, want error) {
		t.Helper()
		if err := m.Open(time.Minute); err != want {
			t.Fatalf("Open %s = %v, want %v", during, err, want)
		}
	}
	if err := send(m, "a", `{"v":1,"op":"scan","country":"GB"}`); err != nil {
		t.Fatal(err)
	}
	refused("during a scan", protocol.Busy)
	close(scanned)
	await(t, func() bool { return m.Open(time.Minute) == nil })
	if err := send(m, "a", joinCommand); err != nil {
		t.Fatal(err)
	}
	refused("during a join", protocol.Busy)
	if err := send(m, "a", `{"v":1,"op":"change_network"}`); err != nil {
		t.Fatal(err)
	}
	refused("during a rollback", protocol.Busy)
	close(rollback)
	await(t, func() bool { return m.Open(time.Minute) == nil })
	close(joined)
	if err := send(m, "a", joinCommand); err != nil {
		t.Fatal(err)
	}
	await(t, func() bool { return m.Status()[2] == protocol.Joined })
	refused("during a handoff", protocol.Busy)
	m.Close()
	refused("after Close", protocol.Unavailable)
}
func TestFailedJoinAfterOpenLeavesTheBoxOnline(t *testing.T) {
	now := time.Unix(1000, 0)
	// The backend has rolled back to the old network by the time a failed Join returns.
	f := &fakeWifi{address: "192.0.2.1", join: func(context.Context, protocol.Command, func(byte)) (string, error) {
		return "", protocol.WrongPassword
	}}
	m := New(context.Background(), f, Config{Now: func() time.Time { return now }})
	defer m.Close()
	tick := func() bool {
		t.Helper()
		active, err := m.Tick(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		return active
	}
	if tick() {
		t.Fatal("setup open on an online box")
	}
	if err := m.Open(10 * time.Minute); err != nil || !tick() {
		t.Fatalf("setup not opened: %v", err)
	}
	if err := send(m, "phone", joinCommand); err != nil {
		t.Fatal(err)
	}
	await(t, func() bool { return m.Status()[2] == protocol.Failed })
	if !tick() {
		t.Fatal("a failed join closed the window early")
	}
	now = now.Add(10 * time.Minute)
	if tick() || m.Status()[2] != protocol.Online {
		t.Fatalf("after the window: state %d", m.Status()[2])
	}
}
func TestRollbackKeepsAnOpenWindow(t *testing.T) {
	now := time.Unix(1000, 0)
	m := New(context.Background(), &fakeWifi{address: "192.0.2.1"}, Config{Now: func() time.Time { return now }})
	defer m.Close()
	if err := m.Open(10 * time.Minute); err != nil {
		t.Fatal(err)
	}
	if err := send(m, "phone", `{"v":1,"op":"change_network"}`); err != nil {
		t.Fatal(err)
	}
	await(t, func() bool { return !m.ChangingNetwork() })
	now = now.Add(5 * time.Minute)
	if active, err := m.Tick(context.Background()); !active || err != nil {
		t.Fatalf("a rollback cut the window short: active=%v error=%v", active, err)
	}
}
func TestHandoffEndsAnOpenWindow(t *testing.T) {
	now := time.Unix(1000, 0)
	f := &fakeWifi{address: "192.0.2.1", join: func(context.Context, protocol.Command, func(byte)) (string, error) {
		return "192.0.2.5", nil
	}}
	m := New(context.Background(), f, Config{Now: func() time.Time { return now }})
	defer m.Close()
	_, _ = m.Tick(context.Background())
	if err := m.Open(10 * time.Minute); err != nil {
		t.Fatal(err)
	}
	if err := send(m, "phone", joinCommand); err != nil {
		t.Fatal(err)
	}
	await(t, func() bool { return m.Status()[2] == protocol.Joined })
	if err := send(m, "phone", `{"v":1,"op":"complete"}`); err != nil {
		t.Fatal(err)
	}
	if active, err := m.Tick(context.Background()); active || err != nil {
		t.Fatalf("handoff did not close setup: active=%v error=%v", active, err)
	}
	if !m.Window().IsZero() {
		t.Fatalf("a handoff left a window to hand on: %v", m.Window())
	}
	// The new network drops; setup reopens after the loss grace, not when the window would have ended.
	f.address = ""
	now = now.Add(5 * time.Minute)
	if active, err := m.Tick(context.Background()); !active || err != nil {
		t.Fatalf("the window outlived the handoff: active=%v error=%v", active, err)
	}
}
func TestWindowOutlivesItsMachine(t *testing.T) {
	now := time.Unix(1000, 0)
	cfg := Config{Now: func() time.Time { return now }}
	first := New(context.Background(), &fakeWifi{address: "192.0.2.1"}, cfg)
	if err := first.Open(10 * time.Minute); err != nil {
		t.Fatal(err)
	}
	first.Close()
	cfg.Window = first.Window()
	if !cfg.Window.Equal(now.Add(10 * time.Minute)) {
		t.Fatalf("window = %v", cfg.Window)
	}
	now = now.Add(time.Minute)
	m := New(context.Background(), &fakeWifi{address: "192.0.2.1"}, cfg)
	defer m.Close()
	if active, err := m.Tick(context.Background()); !active || err != nil || m.Status()[2] != protocol.Idle {
		t.Fatalf("the next machine closed the window: active=%v state=%d error=%v", active, m.Status()[2], err)
	}
	now = now.Add(9 * time.Minute)
	if active, err := m.Tick(context.Background()); active || err != nil || m.Status()[2] != protocol.Online {
		t.Fatalf("the window did not end on time: active=%v state=%d error=%v", active, m.Status()[2], err)
	}
	ended := New(context.Background(), &fakeWifi{address: "192.0.2.1"}, cfg)
	defer ended.Close()
	if active, err := ended.Tick(context.Background()); active || err != nil {
		t.Fatalf("a window that had ended opened setup: active=%v error=%v", active, err)
	}
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
