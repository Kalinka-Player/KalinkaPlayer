package control

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"testing"

	"kalinka/supervisor/internal/protocol"
)

func sshForm() url.Values {
	return url.Values{
		"action": {"enable_ssh"}, "server_id": {testID},
		"password": {"test-only-password"}, "password_confirm": {"test-only-password"},
	}
}

func dashboardOrigin(r *http.Request) {
	r.Header.Set("Origin", "http://kalinka.local:8001")
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
}

func TestSSHFormEnablesAccessWithoutExtendingTheAPI(t *testing.T) {
	h := newTestHandler(t, &fakeCoordinator{}, true).(*handler)
	called := false
	h.cfg.EnableSSH = func(ctx context.Context, password protocol.Secret) error {
		called = true
		if password.Reveal() != sshForm().Get("password") {
			t.Fatal("password changed in transit")
		}
		if _, ok := ctx.Deadline(); !ok || ctx.Err() != nil {
			t.Fatal("setup does not have its own live deadline")
		}
		return nil
	}
	page := serve(h, http.MethodGet, "/", "")
	if !strings.Contains(page.Body.String(), `name="server_id" value="`+testID+`"`) || !strings.Contains(page.Header().Get("Content-Security-Policy"), "form-action 'self'") {
		t.Fatal("form lacks the box identity or permission to submit")
	}
	if page.Header().Get("Referrer-Policy") != "same-origin" {
		t.Fatal("browser cannot send the form's Origin")
	}
	w := serve(h, http.MethodPost, "/", sshForm().Encode(), dashboardOrigin, func(r *http.Request) {
		ctx, cancel := context.WithCancel(r.Context())
		cancel()
		*r = *r.WithContext(ctx)
	})
	if !called || w.Code != http.StatusSeeOther || w.Header().Get("Location") != "/?ssh=enabled#ssh" {
		t.Fatalf("setup = %d, called %v, location %q", w.Code, called, w.Header().Get("Location"))
	}
	if strings.Contains(serve(h, http.MethodGet, "/info", "").Body.String(), "enable_ssh") {
		t.Fatal("dashboard control leaked into protocol actions")
	}
	if w := serve(h, http.MethodPost, "/v1/actions/enable_ssh", `{}`); w.Code != http.StatusNotFound {
		t.Fatal("added an SSH API endpoint")
	}
	result := serve(h, http.MethodGet, "/?ssh=enabled", "").Body.String()
	if !strings.Contains(result, "SSH is enabled") || !strings.Contains(result, `data-username="kalinka-admin"`) {
		t.Fatal("result lacks connection instructions")
	}
	h.cfg.Simulated = true
	if result := serve(h, http.MethodGet, "/?ssh=enabled", "").Body.String(); !strings.Contains(result, "SSH setup simulated successfully") || strings.Contains(result, "SSH is enabled") {
		t.Fatal("simulation claims to have changed the box")
	}
}

func TestSSHFormRejectsInvalidOrCrossOriginRequests(t *testing.T) {
	for _, tc := range []struct {
		name    string
		form    func(url.Values)
		request func(*http.Request)
		want    int
	}{
		{name: "missing origin", request: func(r *http.Request) { r.Header.Del("Origin") }, want: 403},
		{name: "null origin", request: func(r *http.Request) { r.Header.Set("Origin", "null") }, want: 403},
		{name: "hostile origin", request: func(r *http.Request) { r.Header.Set("Origin", "http://evil.example") }, want: 403},
		{name: "Core origin", request: func(r *http.Request) { r.Header.Set("Origin", "http://kalinka.local:8000") }, want: 403},
		{name: "remote peer", request: func(r *http.Request) { r.RemoteAddr = "8.8.8.8:4321" }, want: 403},
		{name: "public hostname", request: func(r *http.Request) { r.Host = "evil.example" }, want: 403},
		{name: "JSON body", request: func(r *http.Request) { r.Header.Set("Content-Type", "application/json") }, want: 415},
		{name: "wrong identity", form: func(f url.Values) { f.Set("server_id", "another-box") }, want: 409},
		{name: "unknown action", form: func(f url.Values) { f.Set("action", "reboot") }, want: 400},
		{name: "extra field", form: func(f url.Values) { f.Set("username", "root") }, want: 400},
		{name: "missing field", form: func(f url.Values) { f.Del("password_confirm") }, want: 400},
		{name: "duplicate field", form: func(f url.Values) { f.Add("password", "another-password") }, want: 400},
		{name: "short password", form: func(f url.Values) { f.Set("password", "short") }, want: 400},
		{name: "mismatch", form: func(f url.Values) { f.Set("password_confirm", "another-password") }, want: 400},
		{name: "account injection", form: func(f url.Values) { f.Set("password", "test-password\nroot:hijack") }, want: 400},
		{name: "oversized", form: func(f url.Values) { f.Set("password", strings.Repeat("x", maxBody)) }, want: 400},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newTestHandler(t, &fakeCoordinator{}, true).(*handler)
			h.cfg.EnableSSH = func(context.Context, protocol.Secret) error { t.Fatal("invalid request reached setup"); return nil }
			f := sshForm()
			if tc.form != nil {
				tc.form(f)
			}
			edits := []func(*http.Request){dashboardOrigin}
			if tc.request != nil {
				edits = append(edits, tc.request)
			}
			w := serve(h, http.MethodPost, "/", f.Encode(), edits...)
			if w.Code != tc.want {
				t.Fatalf("status = %d, want %d", w.Code, tc.want)
			}
			if strings.Contains(w.Body.String(), f.Get("password")) {
				t.Fatal("response leaked password")
			}
		})
	}
}

func TestSSHFormErrorsAndAuditNeverExposeCredentials(t *testing.T) {
	var logs bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
	defer slog.SetDefault(previous)
	for _, tc := range []struct {
		err     error
		want    int
		message string
	}{
		{errors.New(sshForm().Get("password")), 503, "SSH setup failed"},
		{refuse(CodeUpgrade), 409, "An upgrade is installing packages"},
		{refuse(CodeBusy), 409, "Another action is in progress"},
	} {
		h := newTestHandler(t, &fakeCoordinator{}, true).(*handler)
		h.cfg.EnableSSH = func(context.Context, protocol.Secret) error { return tc.err }
		w := serve(h, http.MethodPost, "/", sshForm().Encode(), dashboardOrigin)
		if w.Code != tc.want || !strings.Contains(w.Body.String(), tc.message) {
			t.Fatalf("status %d or message missing", w.Code)
		}
		if strings.Contains(w.Body.String()+logs.String(), sshForm().Get("password")) {
			t.Fatal("password leaked")
		}
	}
	if !strings.Contains(logs.String(), "outcome=ssh_setup_failed") {
		t.Fatal("missing audit outcome")
	}
}
