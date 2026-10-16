package control

import (
	"bytes"
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"kalinka/supervisor/internal/machine"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/wifi"
	"kalinka/supervisor/internal/wifi/wifitest"
)

func TestNetworkStatusReportsOnlyTheConnectedSSID(t *testing.T) {
	wlan := wifitest.Link{Name: "wlan0", Flags: wifitest.Up, Physical: true, Wireless: true, Addresses: []string{"192.168.1.20/24"}}
	wired := wifitest.Link{Name: "eth0", Flags: wifitest.Up, Physical: true, Addresses: []string{"192.168.1.30/24"}}
	for _, tc := range []struct {
		name, routes string
		links        []wifitest.Link
		hotspot      bool
		lookupErr    error
		wantQueries  int
		want         map[string]any
	}{
		{"Wi-Fi", wifitest.WlanDefault, []wifitest.Link{wired, wlan}, false, nil, 1,
			map[string]any{"connected": true, "address": "192.168.1.20", "interface": "wlan0", "kind": "wifi", "ssid": "Home Wi-Fi"}},
		{"Ethernet", wifitest.EthDefault, []wifitest.Link{wired, wlan}, false, nil, 0,
			map[string]any{"connected": true, "address": "192.168.1.30", "interface": "eth0", "kind": "wired"}},
		{"SSID lookup failed", wifitest.WlanDefault, []wifitest.Link{wlan}, false, context.DeadlineExceeded, 1,
			map[string]any{"connected": true, "address": "192.168.1.20", "interface": "wlan0", "kind": "wifi"}},
		{"hotspot is not an upstream connection", wifitest.WlanDefault, []wifitest.Link{
			{Name: "wlan0", Flags: wifitest.Up, Physical: true, Wireless: true, Addresses: []string{wifi.APAddress + "/24"}},
		}, true, nil, 0, map[string]any{"connected": false, "address": "", "kind": "none"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			box := wifitest.Stage(t, tc.routes, tc.links...)
			queries := 0
			run := func(context.Context, time.Duration, string, ...string) (string, error) {
				queries++
				return "Connected to aa:bb:cc:dd:ee:ff\n\tSSID: Home Wi-Fi\n", tc.lookupErr
			}
			got := networkStatusOn(context.Background(), tc.hotspot, box.Host, run)
			if !reflect.DeepEqual(got, tc.want) || queries != tc.wantQueries {
				t.Fatalf("status %#v (%d lookups); want %#v (%d lookups)", got, queries, tc.want, tc.wantQueries)
			}
		})
	}
}

type setupFake struct{ m *machine.Machine }

func (f setupFake) Snapshot() map[string]any {
	return map[string]any{"available": true, "setup": f.m.Snapshot()}
}
func (f setupFake) Open() error                            { return f.m.Open() }
func (f setupFake) Command(owner string, raw []byte) error { return f.m.Command(owner, raw) }
func TestHTTPAndBLEShareSetupOwnership(t *testing.T) {
	m := machine.New(context.Background(), &wifi.DryRun{Delay: time.Hour}, machine.Config{Test: true})
	defer m.Close()
	_, _ = m.Tick(context.Background())
	h := NewHandler(nil, HandlerConfig{CorePort: func() int { return 8000 }, NetworkSetup: setupFake{m}})
	send := func(raw string, edits ...func(*http.Request)) *httptest.ResponseRecorder {
		return serve(h, http.MethodPost, "/v1/setup", raw, append([]func(*http.Request){func(r *http.Request) { r.Header.Set("X-Kalinka-Setup-Client", "0123456789abcdef") }}, edits...)...)
	}
	var logs bytes.Buffer
	old := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
	defer slog.SetDefault(old)
	command := `{"v":1,"op":"join","ssid":"Home","password":"never-log-this","country":"GB"}`
	w := send(command)
	if w.Code != 202 {
		t.Fatal(w.Code, w.Body.String())
	}
	for _, frame := range protocol.Frame([]byte(`{"v":1,"op":"scan","country":"GB"}`)) {
		if err := m.Write("ble-phone", frame); err != protocol.Busy {
			t.Fatal("BLE took HTTP ownership", err)
		}
	}
	if w := send(command, func(r *http.Request) { r.Header.Set("X-Kalinka-Setup-Client", "another-client-1234") }); w.Code != 409 {
		t.Fatal("second HTTP client admitted", w.Code)
	}
	m.Close()
	if strings.Contains(logs.String()+w.Body.String(), "never-log-this") {
		t.Fatal("credential leaked")
	}
}
func TestSetupRequestBoundaries(t *testing.T) {
	m := machine.New(context.Background(), &wifi.DryRun{}, machine.Config{Test: true})
	defer m.Close()
	_, _ = m.Tick(context.Background())
	h := NewHandler(nil, HandlerConfig{CorePort: func() int { return 8000 }, NetworkSetup: setupFake{m}})
	for _, tc := range []struct {
		name string
		edit func(*http.Request)
		body string
		want int
	}{
		{"external origin", func(r *http.Request) { r.Header.Set("Origin", "http://evil.example") }, `{"v":1,"op":"open"}`, 403},
		{"form post", func(r *http.Request) { r.Header.Set("Content-Type", "text/plain") }, `{"v":1,"op":"open"}`, 415},
		{"no owner", func(r *http.Request) { r.Header.Del("X-Kalinka-Setup-Client") }, `{"v":1,"op":"open"}`, 400},
		{"oversize", func(*http.Request) {}, strings.Repeat("x", 513), 400},
		{"invalid credentials", func(*http.Request) {}, `{"v":1,"op":"join","ssid":"home","password":"short","country":"GB"}`, 400},
		{"open online", func(*http.Request) {}, `{"v":1,"op":"open"}`, 202},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := serve(h, http.MethodPost, "/v1/setup", tc.body, func(r *http.Request) { r.Header.Set("X-Kalinka-Setup-Client", "0123456789abcdef"); tc.edit(r) })
			if w.Code != tc.want {
				t.Fatal(w.Code, w.Body.String())
			}
		})
	}
}
func TestCaptiveActivationServesStandardAppWithoutCore(t *testing.T) {
	dir := t.TempDir()
	_ = os.WriteFile(filepath.Join(dir, "index.html"), []byte("standard Kalinka Flutter bundle"), 0644)
	h := NewSetupHandler(nil, func() int { return 8000 }, dir)
	for _, path := range []string{"/generate_204", "/hotspot-detect.html", "/connecttest.txt", "/ncsi.txt"} {
		w := serve(h, http.MethodGet, path, "", func(r *http.Request) { r.Host = "connectivitycheck.example" })
		if w.Code != 302 || w.Header().Get("Location") != "http://192.168.4.1/activate" {
			t.Fatal("probe did not open activation", path, w.Code)
		}
	}
	w := serve(h, http.MethodGet, "/activate", "", func(r *http.Request) { r.Host = "192.168.4.1" })
	if !strings.Contains(w.Body.String(), "/?setup=network") {
		t.Fatal("activation does not open app")
	}
	w = serve(h, http.MethodGet, "/?setup=network", "", func(r *http.Request) { r.Host = "192.168.4.1" })
	if w.Code != 200 || w.Body.String() != "standard Kalinka Flutter bundle" {
		t.Fatal("did not reuse app bundle", w.Code, w.Body.String())
	}
	w = serve(h, http.MethodPost, "/v1/actions/reboot", `{}`, func(r *http.Request) { r.Host = "192.168.4.1" })
	if w.Code == 202 {
		t.Fatal("captive listener exposes machine control")
	}
}
