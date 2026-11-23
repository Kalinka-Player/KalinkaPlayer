package control

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/netip"
	"net/url"
	"regexp"
	"slices"
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

func TestLogNotesCallTheMachineAStreamer(t *testing.T) {
	h := logsHandler(t, &fakeJournal{})
	h.cfg.Journal = func(_ context.Context, stdout io.Writer, args ...string) error {
		switch {
		case slices.Contains(args, "--unit=kalinka.service"):
			_, err := pipe(stdout, bytes.Repeat([]byte("2026-11-23T09:00:00+0000 kalinka kernel: entry\n"), logCap/40))
			return err
		case slices.Contains(args, "--unit=kalinka-supervisor.service"):
			return context.DeadlineExceeded
		case slices.Contains(args, "_TRANSPORT=kernel"):
			return errors.New("exit status 1")
		}
		return nil
	}
	_, files := unzip(t, serve(h, http.MethodGet, "/v1/logs", "").Body.Bytes())
	notes := 0
	for name, text := range files {
		for _, line := range strings.Split(text, "\n") {
			if !strings.HasPrefix(line, "-- ") {
				continue
			}
			notes++
			if callsItABox(line) {
				t.Errorf("%s says box: %s", name, line)
			}
		}
	}
	// A line naming each file's units, then: cut, stopped, failed, and no entries twice.
	if notes != len(logSources)+5 {
		t.Fatalf("%d notes, want %d", notes, len(logSources)+5)
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
		h.cfg.EnableSSH = func(context.Context, protocol.Secret, netip.Addr) error { return errors.New("no sshd") }
		f := sshForm()
		form(f)
		w := serve(h, http.MethodPost, "/", f.Encode(), dashboardOrigin)
		page := w.Body.String()
		if w.Code < 400 || !strings.Contains(page, `role="alert"`) || callsItABox(page) {
			t.Errorf("%s = %d: %s", name, w.Code, page)
		}
	}
}
