package control

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"kalinka/supervisor/internal/soundcard"
)

const (
	testID     = "ee4d496c-3f6c-4388-a02e-f6a2f17829cd"
	rendererID = "5c0ffee0-1d2e-4a3b-8c4d-5e6f7a8b9c0d"
)

type fakeCoordinator struct {
	begun    []Request
	refuse   error
	run      func(context.Context) (Target, error)
	shutdown func(context.Context) error
	status   Status
}

type fakeOperation struct {
	c   *fakeCoordinator
	req Request
}

// Run names the shutdown a terminal action ends in, as the controller does, unless the test says otherwise.
func (o fakeOperation) Run(ctx context.Context) (Target, error) {
	if o.c.run != nil {
		return o.c.run(ctx)
	}
	target, _ := o.req.Action.target()
	return target, nil
}

func (o fakeOperation) Shutdown(ctx context.Context) error {
	if o.c.shutdown != nil {
		return o.c.shutdown(ctx)
	}
	return nil
}

func (f *fakeCoordinator) Begin(_ context.Context, r Request) (Operation, error) {
	f.begun = append(f.begun, r)
	if f.refuse != nil {
		return nil, f.refuse
	}
	return fakeOperation{f, r}, nil
}
func (f *fakeCoordinator) Status(context.Context) (Status, error) {
	if f.refuse != nil {
		return Status{}, f.refuse
	}
	return f.status, nil
}

type fakeSoundcards struct {
	supported bool
	offered   []string
}

func (f *fakeSoundcards) Supported() bool { return f.supported }
func (f *fakeSoundcards) Offers(name string) bool {
	return f.supported && slices.Contains(f.offered, name)
}
func (f *fakeSoundcards) Report() soundcard.Report {
	name := "HiFiBerry Digi2 Pro"
	return soundcard.Report{Supported: f.supported, Name: &name, USB: []soundcard.Active{}, Suggested: []string{"hifiberry-digi-pro"}, Options: []soundcard.Card{{Name: "hifiberry-digi-pro", Label: "HiFiBerry Digi+ Pro or Digi2 Pro", Vendor: "HiFiBerry", Kind: soundcard.Digital}}}
}

// testBox is the box a handler serves: which identities it has, whether Core is installed, and its sound card.
type testBox struct {
	server, renderer bool
	noCore           bool
	cards            Soundcards
}

func pi() *fakeSoundcards {
	return &fakeSoundcards{supported: true, offered: []string{"hifiberry-digi", "none"}}
}

func newTestHandler(t *testing.T, coord Coordinator, server bool) http.Handler {
	return newBoxHandler(t, coord, testBox{server: server, cards: pi()})
}

func newBoxHandler(t *testing.T, coord Coordinator, box testBox) http.Handler {
	t.Helper()
	dir := t.TempDir()
	write := func(name, id string) string {
		path := filepath.Join(dir, name)
		if id != "" {
			if err := os.WriteFile(path, []byte(id+"\n"), 0600); err != nil {
				t.Fatal(err)
			}
		}
		return path
	}
	server, renderer := "", ""
	if box.server {
		server = testID
	}
	if box.renderer {
		renderer = rendererID
	}
	return NewHandler(coord, HandlerConfig{
		Version: "1.2.3", ServerIDFile: write("server_id", server), RendererIDFile: write("renderer_id", renderer),
		CoreInstalled: func() bool { return !box.noCore },
		Soundcards:    box.cards,
		CorePort:      func() int { return 8000 },
		Setup:         func() string { return "waiting" },
		Dashboard:     func(context.Context) any { return map[string]string{"host": "kalinka"} },
		Reinstalls:    Record{LogFile: filepath.Join(dir, "reinstall.log")},
		Removals:      PluginRemovals{Record: Record{LogFile: filepath.Join(dir, "plugin-removal.log")}},
	})
}

func serve(h http.Handler, method, path, body string, edit ...func(*http.Request)) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	r.RemoteAddr = "192.168.1.20:50000"
	r.Host = "kalinka.local:8001"
	if body != "" {
		r.Header.Set("Content-Type", "application/json")
	}
	for _, e := range edit {
		e(r)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func detail(t *testing.T, w *httptest.ResponseRecorder) (int, string) {
	t.Helper()
	var body struct {
		Detail struct{ Code, Message string }
	}
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	return w.Code, body.Detail.Code
}

func TestInfo(t *testing.T) {
	for _, box := range []testBox{{server: true, renderer: true}, {server: true}, {renderer: true}, {}} {
		box.cards = pi()
		w := serve(newBoxHandler(t, &fakeCoordinator{}, box), http.MethodGet, "/info", "")
		var info map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &info); err != nil || w.Code != http.StatusOK {
			t.Fatalf("info: %d %s", w.Code, w.Body)
		}
		want := map[string]any{
			"name": "kalinka-supervisor", "version": "1.2.3", "protocol": 1.0, "min_protocol": 1.0,
			"actions":   []any{"restart_core", "reboot", "poweroff", "reinstall", "set_soundcard", "remove_plugin"},
			"server_id": nil, "renderer_id": nil,
			"poweroff_timer": true,
		}
		if box.server {
			want["server_id"] = testID
		}
		if box.renderer {
			want["renderer_id"] = rendererID
		}
		got, _ := json.Marshal(info)
		expected, _ := json.Marshal(want)
		if !bytes.Equal(got, expected) {
			t.Fatalf("info = %s, want %s", got, expected)
		}
		if w.Header().Get("Cache-Control") != "no-store" {
			t.Fatal("info may be cached")
		}
	}
}

func TestStatusReply(t *testing.T) {
	finished := time.Date(2026, 10, 7, 9, 30, 0, 0, time.UTC)
	coord := &fakeCoordinator{status: Status{
		Core: "failed", Upgrading: true, Pending: PowerOff,
		Reinstall:     Outcome{"failed", finished},
		PluginRemoval: Outcome{"succeeded", finished.Add(time.Hour)}, RemovedPackage: "kalinka-plugin-qobuz",
	}}
	w := serve(newTestHandler(t, coord, true), http.MethodGet, "/v1/status", "")
	want := `{"core":"failed","core_port":8000,"pending":"poweroff",` +
		`"plugin_removal":{"finished_at":"2026-10-07T10:30:00Z","package":"kalinka-plugin-qobuz","state":"succeeded"},` +
		`"reinstall":{"finished_at":"2026-10-07T09:30:00Z","state":"failed"},"setup":"waiting","upgrading":true}`
	if got := strings.TrimSpace(w.Body.String()); got != want {
		t.Fatalf("status = %s", got)
	}
	coord.status = Status{Core: "active", Reinstall: Outcome{State: "idle"}, PluginRemoval: Outcome{State: "idle"}}
	got := serve(newTestHandler(t, coord, true), http.MethodGet, "/v1/status", "").Body.String()
	for _, field := range []string{`"pending":null`, `"plugin_removal":{"finished_at":null,"package":null,"state":"idle"}`} {
		if !strings.Contains(got, field) {
			t.Fatalf("status = %s", got)
		}
	}
	coord.refuse = refuse(CodeUnavailable)
	if code, c := detail(t, serve(newTestHandler(t, coord, true), http.MethodGet, "/v1/status", "")); code != 503 || c != CodeUnavailable {
		t.Fatalf("status failure = %d %s", code, c)
	}
}

func TestActionRequests(t *testing.T) {
	withType := func(v string) func(*http.Request) { return func(r *http.Request) { r.Header.Set("Content-Type", v) } }
	idBody := `{"server_id":"` + testID + `"}`
	rendererBody := `{"renderer_id":"` + rendererID + `"}`
	card := func(id, name string) string { return `{` + id + `,"soundcard":` + name + `}` }
	serverID, rendererIDField := `"server_id":"`+testID+`"`, `"renderer_id":"`+rendererID+`"`
	server, rendererOnly, both, blank := testBox{server: true}, testBox{renderer: true}, testBox{server: true, renderer: true}, testBox{}
	for _, tc := range []struct {
		name   string
		box    testBox
		path   string
		body   string
		edit   []func(*http.Request)
		status int
		code   string
	}{
		{"accepted", server, "/v1/actions/restart_core", idBody, nil, 202, ""},
		{"charset parameter", server, "/v1/actions/reboot", idBody, []func(*http.Request){withType("application/json; charset=utf-8")}, 202, ""},
		{"no identity yet", blank, "/v1/actions/poweroff", `{}`, nil, 202, ""},
		{"identity missing from body", server, "/v1/actions/reboot", `{}`, nil, 409, "wrong_server"},
		{"other box", server, "/v1/actions/reboot", `{"server_id":"other"}`, nil, 409, "wrong_server"},
		{"identity where none exists", blank, "/v1/actions/reboot", idBody, nil, 409, "wrong_server"},
		{"renderer-only box", rendererOnly, "/v1/actions/reboot", rendererBody, nil, 202, ""},
		{"renderer-only box named by a server", rendererOnly, "/v1/actions/reboot", idBody, nil, 409, "wrong_server"},
		{"renderer-only box named by nothing", rendererOnly, "/v1/actions/reboot", `{}`, nil, 409, "wrong_server"},
		{"another renderer", rendererOnly, "/v1/actions/reboot", `{"renderer_id":"other"}`, nil, 409, "wrong_server"},
		{"server box named by its renderer alone", both, "/v1/actions/reboot", rendererBody, nil, 409, "wrong_server"},
		{"server box named by both", both, "/v1/actions/reboot", `{` + serverID + `,` + rendererIDField + `}`, nil, 202, ""},
		{"server box with another box's renderer", both, "/v1/actions/reboot", `{` + serverID + `,"renderer_id":"other"}`, nil, 409, "wrong_server"},
		{"no content type", server, "/v1/actions/reboot", idBody, []func(*http.Request){withType("")}, 415, "unsupported_media_type"},
		{"simple-request smuggling", server, "/v1/actions/reboot", idBody, []func(*http.Request){withType("text/plain; x=application/json")}, 415, "unsupported_media_type"},
		{"unknown field", server, "/v1/actions/reboot", `{"server_id":"` + testID + `","force":true}`, nil, 400, "invalid_request"},
		{"a card for another action", server, "/v1/actions/reboot", card(serverID, `"hifiberry-digi"`), nil, 400, "invalid_request"},
		{"trailing data", server, "/v1/actions/reboot", idBody + `{}`, nil, 400, "invalid_request"},
		{"not an object", server, "/v1/actions/reboot", `[]`, nil, 400, "invalid_request"},
		{"oversized", server, "/v1/actions/reboot", `{"server_id":"` + strings.Repeat("a", 2048) + `"}`, nil, 400, "invalid_request"},
		{"unknown action", server, "/v1/actions/format_disk", idBody, nil, 404, "not_found"},
		{"unknown path", server, "/v2/actions/reboot", idBody, nil, 404, "not_found"},
		{"sound card", server, "/v1/actions/set_soundcard", card(serverID, `"hifiberry-digi"`), nil, 202, ""},
		{"sound card on a renderer-only box", rendererOnly, "/v1/actions/set_soundcard", card(rendererIDField, `"none"`), nil, 202, ""},
		{"sound card for another box", server, "/v1/actions/set_soundcard", card(`"server_id":"other"`, `"hifiberry-digi"`), nil, 409, "wrong_server"},
		{"sound card not named", server, "/v1/actions/set_soundcard", idBody, nil, 400, "invalid_request"},
		{"sound card that is no name", server, "/v1/actions/set_soundcard", card(serverID, `5`), nil, 400, "invalid_request"},
		{"sound card that is a path", server, "/v1/actions/set_soundcard", card(serverID, `"../../etc/passwd"`), nil, 400, "invalid_request"},
		{"sound card with an option", server, "/v1/actions/set_soundcard", `{` + serverID + `,"soundcard":"hifiberry-digi","force":true}`, nil, 400, "invalid_request"},
		{"sound card this box does not offer", server, "/v1/actions/set_soundcard", card(serverID, `"justboom-dac"`), nil, 422, "unknown_soundcard"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			coord := &fakeCoordinator{}
			if tc.box.cards == nil {
				tc.box.cards = pi()
			}
			w := serve(newBoxHandler(t, coord, tc.box), http.MethodPost, tc.path, tc.body, tc.edit...)
			if status, code := detail(t, w); status != tc.status || code != tc.code {
				t.Fatalf("got %d %q: %s", status, code, w.Body)
			}
			if (tc.status == 202) != (len(coord.begun) == 1) {
				t.Fatalf("coordinator asked %v", coord.begun)
			}
		})
	}
}

func TestActionReplies(t *testing.T) {
	coord := &fakeCoordinator{}
	h := newTestHandler(t, coord, true)
	if w := serve(h, http.MethodPost, "/v1/actions/restart_core", `{"server_id":"`+testID+`"}`); strings.TrimSpace(w.Body.String()) != `{"action":"restart_core"}` {
		t.Fatalf("restart reply = %s", w.Body)
	}
	w := serve(h, http.MethodPost, "/v1/actions/set_soundcard", `{"server_id":"`+testID+`","soundcard":"hifiberry-digi"}`)
	if w.Code != 202 || strings.TrimSpace(w.Body.String()) != `{"action":"set_soundcard","reboot":false}` {
		t.Fatalf("sound card reply = %d %s", w.Code, w.Body)
	}
	if got := coord.begun[len(coord.begun)-1]; !reflect.DeepEqual(got, Request{Action: SetSoundcard, Soundcard: "hifiberry-digi"}) {
		t.Fatalf("coordinator asked %+v", got)
	}
}

func TestActionsFollowWhatTheBoxHas(t *testing.T) {
	actions := func(box testBox) []any {
		var info struct{ Actions []any }
		_ = json.Unmarshal(serve(newBoxHandler(t, &fakeCoordinator{}, box), http.MethodGet, "/info", "").Body.Bytes(), &info)
		return info.Actions
	}
	if got := actions(testBox{renderer: true, noCore: true, cards: pi()}); !slices.Equal(got, []any{"reboot", "poweroff", "set_soundcard"}) {
		t.Fatalf("a box with only a renderer offers %v", got)
	}
	if got := actions(testBox{server: true, cards: &fakeSoundcards{}}); slices.Contains(got, any("set_soundcard")) {
		t.Fatalf("a box that is no Pi offers %v", got)
	}
	if got := actions(testBox{server: true}); slices.Contains(got, any("set_soundcard")) {
		t.Fatalf("a supervisor without sound-card support offers %v", got)
	}
	coord := &fakeCoordinator{}
	h := newBoxHandler(t, coord, testBox{renderer: true, noCore: true, cards: &fakeSoundcards{}})
	for _, action := range []string{"restart_core", "reinstall", "set_soundcard"} {
		if status, code := detail(t, serve(h, http.MethodPost, "/v1/actions/"+action, `{"renderer_id":"`+rendererID+`"}`)); status != 404 || code != "not_found" {
			t.Errorf("%s on a box that cannot = %d %s", action, status, code)
		}
	}
	if len(coord.begun) != 0 {
		t.Fatalf("coordinator asked %v", coord.begun)
	}
}

func TestSoundcardReport(t *testing.T) {
	w := serve(newTestHandler(t, &fakeCoordinator{}, true), http.MethodGet, "/v1/soundcard", "")
	want := `{"supported":true,"name":"HiFiBerry Digi2 Pro","hat":null,"configured":null,"configured_by":null,"active":null,"usb":[],"suggested":["hifiberry-digi-pro"],"options":[{"name":"hifiberry-digi-pro","label":"HiFiBerry Digi+ Pro or Digi2 Pro","vendor":"HiFiBerry","kind":"digital"}]}`
	if w.Code != 200 || strings.TrimSpace(w.Body.String()) != want || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("report = %d %s", w.Code, w.Body)
	}
	if status, code := detail(t, serve(newBoxHandler(t, &fakeCoordinator{}, testBox{server: true}), http.MethodGet, "/v1/soundcard", "")); status != 404 || code != "not_found" {
		t.Fatalf("without sound-card support = %d %s", status, code)
	}
}

func TestMethods(t *testing.T) {
	h := newTestHandler(t, &fakeCoordinator{}, true)
	w := serve(h, http.MethodGet, "/v1/actions/reboot", "")
	if status, code := detail(t, w); status != 405 || code != "method_not_allowed" || w.Header().Get("Allow") != "POST, OPTIONS" {
		t.Fatalf("GET action = %d %s %q", status, code, w.Header().Get("Allow"))
	}
	if status, code := detail(t, serve(h, http.MethodPost, "/info", `{}`)); status != 405 || code != "method_not_allowed" {
		t.Fatalf("POST info = %d %s", status, code)
	}
	if w := serve(h, http.MethodHead, "/info", ""); w.Code != 200 {
		t.Fatalf("HEAD info = %d", w.Code)
	}
	if w := serve(h, http.MethodGet, "/v1/soundcard", ""); w.Code != 200 {
		t.Fatalf("GET soundcard = %d", w.Code)
	}
	if status, code := detail(t, serve(h, http.MethodPost, "/v1/soundcard", `{}`)); status != 405 || code != "method_not_allowed" {
		t.Fatalf("POST soundcard = %d %s", status, code)
	}
}

func TestRefusals(t *testing.T) {
	for _, tc := range []struct {
		refusal    error
		status     int
		retryAfter string
	}{
		{refuse(CodeBusy), 409, ""},
		{refuse(CodeShuttingDown), 409, ""},
		{refuse(CodeUpgrade), 409, ""},
		{refuse(CodeNetworkChange), 409, ""},
		{&Refusal{Code: CodeTooSoon, RetryAfter: 9200 * time.Millisecond}, 429, "10"},
		{refuse(CodeUnavailable), 503, ""},
		{refuse(CodeSoundcard), 500, ""},
		{refuse(CodeInvalidPackage), 400, ""},
		{refuse(CodeNotInstalled), 409, ""},
	} {
		w := serve(newTestHandler(t, &fakeCoordinator{refuse: tc.refusal}, true), http.MethodPost, "/v1/actions/restart_core", `{"server_id":"`+testID+`"}`)
		if status, code := detail(t, w); status != tc.status || code != tc.refusal.Error() || w.Header().Get("Retry-After") != tc.retryAfter {
			t.Fatalf("%v: %d %s retry %q", tc.refusal, status, code, w.Header().Get("Retry-After"))
		}
	}
	failing := &fakeCoordinator{run: func(context.Context) (Target, error) { return "", refuse(CodeUnavailable) }}
	if status, code := detail(t, serve(newTestHandler(t, failing, true), http.MethodPost, "/v1/actions/restart_core", `{"server_id":"`+testID+`"}`)); status != 503 || code != CodeUnavailable {
		t.Fatalf("failed restart = %d %s", status, code)
	}
}

func TestRequestOrigin(t *testing.T) {
	h := newTestHandler(t, &fakeCoordinator{}, true)
	from := func(remote, host, origin string) func(*http.Request) {
		return func(r *http.Request) {
			r.RemoteAddr, r.Host = remote, host
			if origin != "" {
				r.Header.Set("Origin", origin)
			}
		}
	}
	for _, tc := range []struct {
		name, remote, host, origin string
		code                       string
	}{
		{"internet peer", "203.0.113.5:4000", "kalinka.local:8001", "", "not_local"},
		{"rebound name", "192.168.1.20:4000", "attacker.example.com:8001", "", "host_not_allowed"},
		{"foreign page", "192.168.1.20:4000", "kalinka.local:8001", "https://attacker.example.com", "origin_not_allowed"},
		{"sandboxed page", "192.168.1.20:4000", "kalinka.local:8001", "null", "origin_not_allowed"},
	} {
		if status, code := detail(t, serve(h, http.MethodGet, "/info", "", from(tc.remote, tc.host, tc.origin))); status != 403 || code != tc.code {
			t.Errorf("%s: %d %s", tc.name, status, code)
		}
	}
	player := from("192.168.1.20:4000", "kalinka.local:8001", "http://kalinka.local:8000")
	w := serve(h, http.MethodGet, "/info", "", player)
	if w.Code != 200 || w.Header().Get("Access-Control-Allow-Origin") != "http://kalinka.local:8000" || w.Header().Get("Vary") != "Origin" {
		t.Fatalf("web player read = %d %v", w.Code, w.Header())
	}
	w = serve(h, http.MethodOptions, "/v1/actions/poweroff", "", player)
	if w.Code != 204 || w.Header().Get("Access-Control-Allow-Origin") != "http://kalinka.local:8000" ||
		w.Header().Get("Access-Control-Allow-Methods") != "POST" || w.Header().Get("Access-Control-Allow-Headers") != "Content-Type" {
		t.Fatalf("preflight = %d %v", w.Code, w.Header())
	}
}

func TestShutdownReplyArrivesBeforeShutdown(t *testing.T) {
	for _, tc := range []struct {
		name, path, body, want string
		run                    func(context.Context) (Target, error)
	}{
		{name: "power off", path: "/v1/actions/poweroff", body: `{"server_id":"` + testID + `"}`, want: `202 Accepted {"action":"poweroff"}`},
		{
			name: "a sound card that needs a restart", path: "/v1/actions/set_soundcard",
			body: `{"server_id":"` + testID + `","soundcard":"hifiberry-digi"}`,
			want: `202 Accepted {"action":"set_soundcard","reboot":true}`,
			run:  func(context.Context) (Target, error) { return RebootTarget, nil },
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			release := make(chan struct{})
			ran := make(chan struct{})
			coord := &fakeCoordinator{run: tc.run, shutdown: func(context.Context) error {
				<-release
				close(ran)
				return nil
			}}
			server := httptest.NewServer(newTestHandler(t, coord, true))
			defer server.Close()
			replied := make(chan string, 1)
			go func() {
				resp, err := http.Post(server.URL+tc.path, "application/json", strings.NewReader(tc.body))
				if err != nil {
					replied <- err.Error()
					return
				}
				defer resp.Body.Close()
				body, _ := io.ReadAll(resp.Body)
				replied <- resp.Status + " " + strings.TrimSpace(string(body))
			}()
			select {
			case got := <-replied:
				if got != tc.want {
					t.Fatalf("reply = %s", got)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("reply held back until the shutdown ran")
			}
			close(release)
			<-ran
		})
	}
}

func TestAuditLine(t *testing.T) {
	var logs bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
	defer slog.SetDefault(previous)
	h := newTestHandler(t, &fakeCoordinator{}, true)
	serve(h, http.MethodPost, "/v1/actions/reboot", `{"server_id":"`+testID+`"}`)
	serve(h, http.MethodPost, "/v1/actions/reboot", `{"server_id":"wrong-box"}`)
	serve(h, http.MethodGet, "/info", "")
	lines := strings.Split(strings.TrimSpace(logs.String()), "\n")
	if len(lines) != 2 || !strings.Contains(lines[0], "outcome=accepted") || !strings.Contains(lines[1], "outcome=wrong_server") {
		t.Fatalf("audit = %q", lines)
	}
	if strings.Contains(logs.String(), "wrong-box") || !strings.Contains(lines[0], "remote=192.168.1.20:50000") {
		t.Fatalf("audit leaks the body or misses the peer: %q", lines)
	}
}

func TestPage(t *testing.T) {
	h := newTestHandler(t, &fakeCoordinator{}, true)
	for path, contentType := range map[string]string{
		"/":          "text/html; charset=utf-8",
		"/app.js":    "text/javascript; charset=utf-8",
		"/style.css": "text/css; charset=utf-8",
		"/logo.svg":  "image/svg+xml",
		"/icon.svg":  "image/svg+xml",
	} {
		w := serve(h, http.MethodGet, path, "")
		if w.Code != 200 || w.Header().Get("Content-Type") != contentType || w.Body.Len() == 0 {
			t.Fatalf("%s = %d %q (%d bytes)", path, w.Code, w.Header().Get("Content-Type"), w.Body.Len())
		}
		if !strings.Contains(w.Header().Get("Content-Security-Policy"), "frame-ancestors 'none'") || w.Header().Get("X-Frame-Options") != "DENY" {
			t.Fatalf("%s can be framed: %v", path, w.Header())
		}
	}
	if !strings.Contains(serve(h, http.MethodGet, "/", "").Body.String(), "<title>Kalinka Supervisor</title>") {
		t.Fatal("the page is not the control page")
	}
	if status, code := detail(t, serve(h, http.MethodPost, "/", `{}`)); status != 405 || code != "method_not_allowed" {
		t.Fatalf("POST / = %d %s", status, code)
	}
}

func TestPageAssetsStayWithinItsPolicy(t *testing.T) {
	h := newTestHandler(t, &fakeCoordinator{}, true)
	page := serve(h, http.MethodGet, "/", "").Body.String()
	for _, inline := range []string{"<script>", "style=", "onclick=", "http://", "https://"} {
		if strings.Contains(page, inline) {
			t.Errorf("the page carries %q, which its policy refuses or which needs the internet", inline)
		}
	}
	for _, svg := range []string{"/logo.svg", "/icon.svg"} {
		if strings.Contains(serve(h, http.MethodGet, svg, "").Body.String(), "style=") {
			t.Errorf("%s colours itself with inline CSS, which its policy refuses", svg)
		}
	}
}

func TestDashboard(t *testing.T) {
	w := serve(newTestHandler(t, &fakeCoordinator{}, true), http.MethodGet, "/v1/dashboard", "")
	if w.Code != 200 || strings.TrimSpace(w.Body.String()) != `{"host":"kalinka"}` {
		t.Fatalf("dashboard = %d %s", w.Code, w.Body)
	}
}

func TestReinstallLog(t *testing.T) {
	dir := t.TempDir()
	log := filepath.Join(dir, "reinstall.log")
	h := NewHandler(&fakeCoordinator{}, HandlerConfig{
		ServerIDFile: filepath.Join(dir, "server_id"), CorePort: func() int { return 8000 },
		Reinstalls: Record{LogFile: log},
	})
	if w := serve(h, http.MethodGet, "/v1/reinstall/log", ""); w.Code != 200 || w.Body.Len() != 0 {
		t.Fatalf("no reinstall yet = %d %q", w.Code, w.Body)
	}
	if err := os.WriteFile(log, []byte(">> Fetching installer\n>> Installed\n"), 0600); err != nil {
		t.Fatal(err)
	}
	w := serve(h, http.MethodGet, "/v1/reinstall/log", "")
	if w.Code != 200 || w.Header().Get("Content-Type") != "text/plain; charset=utf-8" || w.Body.String() != ">> Fetching installer\n>> Installed\n" {
		t.Fatalf("log = %d %q", w.Code, w.Body)
	}
}

func TestThePageMayAct(t *testing.T) {
	coord := &fakeCoordinator{}
	page := func(r *http.Request) {
		r.Host = "kalinka.local:8001"
		r.Header.Set("Origin", "http://kalinka.local:8001")
	}
	w := serve(newTestHandler(t, coord, true), http.MethodPost, "/v1/actions/reinstall", `{"server_id":"`+testID+`"}`, page)
	if w.Code != 202 || len(coord.begun) != 1 || coord.begun[0].Action != Reinstall {
		t.Fatalf("the supervisor's own page was refused: %d %s", w.Code, w.Body)
	}
}

func TestRemovalRequests(t *testing.T) {
	idBody := `{"server_id":"` + testID + `"}`
	withPackage := `{"server_id":"` + testID + `","package":"kalinka-plugin-qobuz"}`
	for _, tc := range []struct {
		name, path, body string
		status           int
		code             string
	}{
		{"accepted", "/v1/actions/remove_plugin", withPackage, 202, ""},
		{"without a package", "/v1/actions/remove_plugin", idBody, 400, "invalid_request"},
		{"package on another action", "/v1/actions/reboot", withPackage, 400, "invalid_request"},
		{"renderer-only box", "/v1/actions/remove_plugin", withPackage, 404, "not_found"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			coord := &fakeCoordinator{}
			box := testBox{server: true, cards: pi(), noCore: tc.name == "renderer-only box"}
			w := serve(newBoxHandler(t, coord, box), http.MethodPost, tc.path, tc.body)
			if status, code := detail(t, w); status != tc.status || code != tc.code {
				t.Fatalf("got %d %q: %s", status, code, w.Body)
			}
			if tc.status != 202 {
				if len(coord.begun) != 0 {
					t.Fatalf("coordinator asked %v", coord.begun)
				}
				return
			}
			want := Request{Action: RemovePlugin, Package: "kalinka-plugin-qobuz"}
			if len(coord.begun) != 1 || !reflect.DeepEqual(coord.begun[0], want) || strings.TrimSpace(w.Body.String()) != `{"action":"remove_plugin","package":"kalinka-plugin-qobuz"}` {
				t.Fatalf("removal asked %+v, answered %s", coord.begun, w.Body)
			}
		})
	}
}

func TestPluginRemovalLog(t *testing.T) {
	dir := t.TempDir()
	log := filepath.Join(dir, "plugin-removal.log")
	h := NewHandler(&fakeCoordinator{}, HandlerConfig{
		ServerIDFile: filepath.Join(dir, "server_id"), CorePort: func() int { return 8000 },
		Removals: PluginRemovals{Record: Record{LogFile: log}},
	})
	if w := serve(h, http.MethodGet, "/v1/plugin-removal/log", ""); w.Code != 200 || w.Body.Len() != 0 {
		t.Fatalf("no removal yet = %d %q", w.Code, w.Body)
	}
	if err := os.WriteFile(log, []byte(">> Removing kalinka-plugin-qobuz\n>> Removed kalinka-plugin-qobuz\n"), 0600); err != nil {
		t.Fatal(err)
	}
	w := serve(h, http.MethodGet, "/v1/plugin-removal/log", "")
	if w.Code != 200 || w.Header().Get("Content-Type") != "text/plain; charset=utf-8" || !strings.HasSuffix(w.Body.String(), ">> Removed kalinka-plugin-qobuz\n") {
		t.Fatalf("log = %d %q", w.Code, w.Body)
	}
}
