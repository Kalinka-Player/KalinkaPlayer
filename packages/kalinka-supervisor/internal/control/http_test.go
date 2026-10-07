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
	"strings"
	"testing"
	"time"
)

const testID = "ee4d496c-3f6c-4388-a02e-f6a2f17829cd"

type fakeCoordinator struct {
	begun  []Action
	refuse error
	run    func(context.Context) error
	status Status
}

type runFunc func(context.Context) error

func (f runFunc) Run(ctx context.Context) error { return f(ctx) }

func (f *fakeCoordinator) Begin(_ context.Context, a Action) (Operation, error) {
	f.begun = append(f.begun, a)
	if f.refuse != nil {
		return nil, f.refuse
	}
	if f.run != nil {
		return runFunc(f.run), nil
	}
	return runFunc(func(context.Context) error { return nil }), nil
}
func (f *fakeCoordinator) Status(context.Context) (Status, error) {
	if f.refuse != nil {
		return Status{}, f.refuse
	}
	return f.status, nil
}

func newTestHandler(t *testing.T, coord Coordinator, identity bool) http.Handler {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "server_id")
	if identity {
		if err := os.WriteFile(path, []byte(testID+"\n"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	return NewHandler(coord, HandlerConfig{
		Version: "1.2.3", ServerIDFile: path,
		CorePort:   func() int { return 8000 },
		Setup:      func() string { return "waiting" },
		Dashboard:  func(context.Context) any { return map[string]string{"host": "kalinka"} },
		Reinstalls: ReinstallRecord{LogFile: filepath.Join(dir, "reinstall.log")},
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
	for _, identity := range []bool{true, false} {
		w := serve(newTestHandler(t, &fakeCoordinator{}, identity), http.MethodGet, "/info", "")
		var info map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &info); err != nil || w.Code != http.StatusOK {
			t.Fatalf("info: %d %s", w.Code, w.Body)
		}
		want := map[string]any{
			"name": "kalinka-supervisor", "version": "1.2.3", "protocol": 1.0, "min_protocol": 1.0,
			"actions": []any{"restart_core", "reboot", "poweroff", "reinstall"}, "server_id": nil,
		}
		if identity {
			want["server_id"] = testID
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
	coord := &fakeCoordinator{status: Status{Core: "failed", Upgrading: true, Pending: PowerOff, Reinstall: "failed", ReinstallFinished: finished}}
	w := serve(newTestHandler(t, coord, true), http.MethodGet, "/v1/status", "")
	want := `{"core":"failed","core_port":8000,"pending":"poweroff","reinstall":{"finished_at":"2026-10-07T09:30:00Z","state":"failed"},"setup":"waiting","upgrading":true}`
	if got := strings.TrimSpace(w.Body.String()); got != want {
		t.Fatalf("status = %s", got)
	}
	coord.status = Status{Core: "active"}
	if got := serve(newTestHandler(t, coord, true), http.MethodGet, "/v1/status", "").Body.String(); !strings.Contains(got, `"pending":null`) {
		t.Fatalf("status = %s", got)
	}
	coord.refuse = refuse(CodeUnavailable)
	if code, c := detail(t, serve(newTestHandler(t, coord, true), http.MethodGet, "/v1/status", "")); code != 503 || c != CodeUnavailable {
		t.Fatalf("status failure = %d %s", code, c)
	}
}

func TestActionRequests(t *testing.T) {
	withType := func(v string) func(*http.Request) { return func(r *http.Request) { r.Header.Set("Content-Type", v) } }
	idBody := `{"server_id":"` + testID + `"}`
	for _, tc := range []struct {
		name     string
		identity bool
		path     string
		body     string
		edit     []func(*http.Request)
		status   int
		code     string
	}{
		{"accepted", true, "/v1/actions/restart_core", idBody, nil, 202, ""},
		{"charset parameter", true, "/v1/actions/reboot", idBody, []func(*http.Request){withType("application/json; charset=utf-8")}, 202, ""},
		{"no identity yet", false, "/v1/actions/poweroff", `{}`, nil, 202, ""},
		{"identity missing from body", true, "/v1/actions/reboot", `{}`, nil, 409, "wrong_server"},
		{"other box", true, "/v1/actions/reboot", `{"server_id":"other"}`, nil, 409, "wrong_server"},
		{"identity where none exists", false, "/v1/actions/reboot", idBody, nil, 409, "wrong_server"},
		{"no content type", true, "/v1/actions/reboot", idBody, []func(*http.Request){withType("")}, 415, "unsupported_media_type"},
		{"simple-request smuggling", true, "/v1/actions/reboot", idBody, []func(*http.Request){withType("text/plain; x=application/json")}, 415, "unsupported_media_type"},
		{"unknown field", true, "/v1/actions/reboot", `{"server_id":"` + testID + `","force":true}`, nil, 400, "invalid_request"},
		{"trailing data", true, "/v1/actions/reboot", idBody + `{}`, nil, 400, "invalid_request"},
		{"not an object", true, "/v1/actions/reboot", `[]`, nil, 400, "invalid_request"},
		{"oversized", true, "/v1/actions/reboot", `{"server_id":"` + strings.Repeat("a", 2048) + `"}`, nil, 400, "invalid_request"},
		{"unknown action", true, "/v1/actions/format_disk", idBody, nil, 404, "not_found"},
		{"unknown path", true, "/v2/actions/reboot", idBody, nil, 404, "not_found"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			coord := &fakeCoordinator{}
			w := serve(newTestHandler(t, coord, tc.identity), http.MethodPost, tc.path, tc.body, tc.edit...)
			if status, code := detail(t, w); status != tc.status || code != tc.code {
				t.Fatalf("got %d %q: %s", status, code, w.Body)
			}
			if (tc.status == 202) != (len(coord.begun) == 1) {
				t.Fatalf("coordinator asked %v", coord.begun)
			}
		})
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
	} {
		w := serve(newTestHandler(t, &fakeCoordinator{refuse: tc.refusal}, true), http.MethodPost, "/v1/actions/restart_core", `{"server_id":"`+testID+`"}`)
		if status, code := detail(t, w); status != tc.status || code != tc.refusal.Error() || w.Header().Get("Retry-After") != tc.retryAfter {
			t.Fatalf("%v: %d %s retry %q", tc.refusal, status, code, w.Header().Get("Retry-After"))
		}
	}
	failing := &fakeCoordinator{run: func(context.Context) error { return refuse(CodeUnavailable) }}
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
	release := make(chan struct{})
	ran := make(chan struct{})
	coord := &fakeCoordinator{run: func(context.Context) error {
		<-release
		close(ran)
		return nil
	}}
	server := httptest.NewServer(newTestHandler(t, coord, true))
	defer server.Close()
	replied := make(chan string, 1)
	go func() {
		resp, err := http.Post(server.URL+"/v1/actions/poweroff", "application/json", strings.NewReader(`{"server_id":"`+testID+`"}`))
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
		if got != `202 Accepted {"action":"poweroff"}` {
			t.Fatalf("reply = %s", got)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("reply held back until the shutdown ran")
	}
	close(release)
	<-ran
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
		Reinstalls: ReinstallRecord{LogFile: log},
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
	if w.Code != 202 || len(coord.begun) != 1 || coord.begun[0] != Reinstall {
		t.Fatalf("the supervisor's own page was refused: %d %s", w.Code, w.Body)
	}
}
