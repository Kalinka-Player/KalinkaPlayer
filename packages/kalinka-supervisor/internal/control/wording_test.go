package control

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"testing"

	"kalinka/supervisor/internal/protocol"
)

// People read "streamer"; "box" may stay only inside CSS names such as box-sizing and border-box.
var (
	cssBoxName = regexp.MustCompile(`\b(box-(sizing|shadow)|(border|content|padding)-box)\b`)
	boxWord    = regexp.MustCompile(`(?i)\bbox(es)?\b`)
)

func callsItABox(text string) bool {
	return boxWord.MatchString(cssBoxName.ReplaceAllString(text, ""))
}

func TestPagesCallTheMachineAStreamer(t *testing.T) {
	if callsItABox("* { box-sizing: border-box; box-shadow: none }") || !callsItABox("Restart the Box") || !callsItABox("a set-top-box") {
		t.Fatal("the check cannot tell CSS names from text")
	}
	for _, file := range []string{"web/index.html", "web/app.js"} {
		b, err := web.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		for i, line := range strings.Split(string(b), "\n") {
			if callsItABox(line) {
				t.Errorf("%s:%d says box: %s", file, i+1, strings.TrimSpace(line))
			}
		}
	}
	for code, o := range outcomes {
		if callsItABox(o.message) {
			t.Errorf("the %s message says box: %s", code, o.message)
		}
	}
	h := NewSetupHandler(nil, func() int { return 8000 }, t.TempDir())
	activation := serve(h, http.MethodGet, "/activate", "", func(r *http.Request) { r.Host = "192.168.4.1" })
	if activation.Code != http.StatusOK || !strings.Contains(activation.Body.String(), "streamer") || callsItABox(activation.Body.String()) {
		t.Errorf("the activation page = %d: %s", activation.Code, activation.Body.String())
	}
}

func TestDashboardErrorsCallTheMachineAStreamer(t *testing.T) {
	for name, form := range map[string]func(url.Values){
		"unreadable":     func(f url.Values) { f.Del("password_confirm") },
		"unknown action": func(f url.Values) { f.Set("action", "reboot") },
		"wrong identity": func(f url.Values) { f.Set("server_id", "another") },
		"short password": func(f url.Values) { f.Set("password", "short") },
		"mismatch":       func(f url.Values) { f.Set("password_confirm", "another-password") },
		"setup failed":   func(url.Values) {},
	} {
		h := newTestHandler(t, &fakeCoordinator{}, true).(*handler)
		h.cfg.EnableSSH = func(context.Context, protocol.Secret) error { return errors.New("no sshd") }
		f := sshForm()
		form(f)
		w := serve(h, http.MethodPost, "/", f.Encode(), dashboardOrigin)
		page := w.Body.String()
		if w.Code < 400 || !strings.Contains(page, `role="alert"`) || callsItABox(page) {
			t.Errorf("%s = %d: %s", name, w.Code, page)
		}
	}
}
