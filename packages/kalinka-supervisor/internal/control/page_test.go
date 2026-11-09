package control

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"testing"

	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/wifi"
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

// fakePowerSaving is a box's Wi-Fi power-saving switch; err is what Set fails with.
type fakePowerSaving struct {
	offered, enabled bool
	err              error
	sets             []bool
}

func (f *fakePowerSaving) Offered() bool { return f.offered }
func (f *fakePowerSaving) Enabled() bool { return f.enabled }
func (f *fakePowerSaving) Set(_ context.Context, on bool) error {
	f.sets = append(f.sets, on)
	if f.err == nil || errors.Is(f.err, wifi.ErrNotApplied) {
		f.enabled = on
	}
	return f.err
}

func powerSavingForm(state string) url.Values {
	return url.Values{"action": {"set_wifi_powersave"}, "server_id": {testID}, "powersave": {state}}
}

// powerSavingHandler serves a box with SSH setup, as every image has, and saving as its Wi-Fi switch.
func powerSavingHandler(t *testing.T, saving *fakePowerSaving) *handler {
	t.Helper()
	h := newTestHandler(t, &fakeCoordinator{}, true).(*handler)
	h.cfg.EnableSSH = func(context.Context, protocol.Secret) error { t.Fatal("the Wi-Fi form reached SSH setup"); return nil }
	h.cfg.WiFiPowerSaving = saving
	return h
}

func TestWiFiPowerSavingFormSwitchesWithoutExtendingTheAPI(t *testing.T) {
	saving := &fakePowerSaving{offered: true}
	h := powerSavingHandler(t, saving)
	page := serve(h, http.MethodGet, "/", "").Body.String()
	if !strings.Contains(page, "Wi-Fi power saving is off") || !strings.Contains(page, `name="powersave" value="on"`) || !strings.Contains(page, `name="server_id" value="`+testID+`"`) {
		t.Fatal("the page does not offer to turn power saving on")
	}
	w := serve(h, http.MethodPost, "/", powerSavingForm("on").Encode(), dashboardOrigin)
	if w.Code != http.StatusSeeOther || w.Header().Get("Location") != "/?powersave=applied#wifi-powersave" || !slices.Equal(saving.sets, []bool{true}) {
		t.Fatalf("turning on = %d to %q after %v", w.Code, w.Header().Get("Location"), saving.sets)
	}
	result := serve(h, http.MethodGet, "/?powersave=applied", "").Body.String()
	for _, want := range []string{"Wi-Fi power saving is on", "Wi-Fi power saving is now on", `name="powersave" value="off"`, `role="status"`} {
		if !strings.Contains(result, want) {
			t.Errorf("the page after turning on lacks %q", want)
		}
	}
	if w := serve(h, http.MethodPost, "/", powerSavingForm("off").Encode(), dashboardOrigin); w.Code != http.StatusSeeOther || !slices.Equal(saving.sets, []bool{true, false}) {
		t.Fatalf("turning off = %d after %v", w.Code, saving.sets)
	}
	if strings.Contains(serve(h, http.MethodGet, "/info", "").Body.String(), "set_wifi_powersave") {
		t.Fatal("dashboard control leaked into protocol actions")
	}
	if w := serve(h, http.MethodPost, "/v1/actions/set_wifi_powersave", `{}`); w.Code != http.StatusNotFound {
		t.Fatal("added a power-saving API endpoint")
	}
	h.cfg.Simulated = true
	if !strings.Contains(serve(h, http.MethodGet, "/", "").Body.String(), "Preview mode: the choice is simulated") {
		t.Fatal("simulation claims to change the box")
	}
}

func TestWiFiPowerSavingFormSaysWhenOnlyTheNextConnectionFollows(t *testing.T) {
	h := powerSavingHandler(t, &fakePowerSaving{offered: true, err: wifi.ErrNotApplied})
	w := serve(h, http.MethodPost, "/", powerSavingForm("on").Encode(), dashboardOrigin)
	if w.Code != http.StatusSeeOther || w.Header().Get("Location") != "/?powersave=saved#wifi-powersave" {
		t.Fatalf("saved only = %d to %q", w.Code, w.Header().Get("Location"))
	}
	result := serve(h, http.MethodGet, "/?powersave=saved", "").Body.String()
	if !strings.Contains(result, "Wi-Fi power saving will be on from the next time this streamer connects") || strings.Contains(result, "is now on") {
		t.Fatal("the page claims the running connection changed")
	}
}

func TestWiFiPowerSavingFormRejectsWhatItCannotDo(t *testing.T) {
	for _, tc := range []struct {
		name    string
		saving  *fakePowerSaving
		form    func(url.Values)
		request func(*http.Request)
		want    int
	}{
		{name: "no wireless interface", saving: &fakePowerSaving{}, want: 400},
		{name: "unknown state", form: func(f url.Values) { f.Set("powersave", "maybe") }, want: 400},
		{name: "missing state", form: func(f url.Values) { f.Del("powersave") }, want: 400},
		{name: "extra field", form: func(f url.Values) { f.Set("password", "x") }, want: 400},
		{name: "duplicate field", form: func(f url.Values) { f.Add("powersave", "off") }, want: 400},
		{name: "wrong identity", form: func(f url.Values) { f.Set("server_id", "another") }, want: 409},
		{name: "hostile origin", request: func(r *http.Request) { r.Header.Set("Origin", "http://evil.example") }, want: 403},
		{name: "JSON body", request: func(r *http.Request) { r.Header.Set("Content-Type", "application/json") }, want: 415},
	} {
		t.Run(tc.name, func(t *testing.T) {
			saving := tc.saving
			if saving == nil {
				saving = &fakePowerSaving{offered: true}
			}
			h := powerSavingHandler(t, saving)
			f := powerSavingForm("on")
			if tc.form != nil {
				tc.form(f)
			}
			edits := []func(*http.Request){dashboardOrigin}
			if tc.request != nil {
				edits = append(edits, tc.request)
			}
			w := serve(h, http.MethodPost, "/", f.Encode(), edits...)
			if w.Code != tc.want || len(saving.sets) != 0 || callsItABox(w.Body.String()) {
				t.Fatalf("status = %d, want %d; switched %v", w.Code, tc.want, saving.sets)
			}
			// A hostile origin is refused in JSON before any form is read; the rest come back as the page.
			if strings.HasPrefix(w.Header().Get("Content-Type"), "text/html") && !strings.Contains(w.Body.String(), `role="alert"`) {
				t.Fatalf("the page explains nothing: %s", w.Body)
			}
		})
	}
	if page := serve(powerSavingHandler(t, &fakePowerSaving{}), http.MethodGet, "/", "").Body.String(); strings.Contains(page, "wifi-powersave") {
		t.Fatal("a wired-only box shows the switch")
	}
}

func TestWiFiPowerSavingFormReportsAFailureWithoutItsCause(t *testing.T) {
	h := powerSavingHandler(t, &fakePowerSaving{offered: true, err: errors.New("disk full")})
	w := serve(h, http.MethodPost, "/", powerSavingForm("on").Encode(), dashboardOrigin)
	if w.Code != http.StatusServiceUnavailable || !strings.Contains(w.Body.String(), "could not be saved") || strings.Contains(w.Body.String(), "disk full") || callsItABox(w.Body.String()) {
		t.Fatalf("failure = %d: %s", w.Code, w.Body)
	}
}
