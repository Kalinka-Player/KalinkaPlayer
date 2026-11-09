package control

import (
	"bytes"
	"context"
	"embed"
	"errors"
	"html/template"
	"mime"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"kalinka/supervisor/internal/coreconf"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/sshaccess"
	"kalinka/supervisor/internal/wifi"
)

//go:embed web
var web embed.FS

var dashboardPage = template.Must(template.ParseFS(web, "web/index.html"))

// setWiFiPowerSaving is a dashboard form outside the versioned API, like enableSSH.
const setWiFiPowerSaving Action = "set_wifi_powersave"

const (
	sshSetupTimeout = 30 * time.Second
	// powerSavingTimeout covers NetworkManager rereading its configuration, then iw on each interface.
	powerSavingTimeout = 15 * time.Second
)

// WiFiPowerSaving is the box's Wi-Fi power-saving switch; wifi.PowerSaving is the production one.
type WiFiPowerSaving interface {
	Offered() bool
	Enabled() bool
	Set(context.Context, bool) error
}

// pageData renders the control page: the forms it has, what they report and what the last one did.
type pageData struct {
	SSH, Simulated, SSHEnabled bool
	ServerID, Username         string
	// Failed names the form Error came from; "" is an error before any form was recognised.
	Failed Action
	Error  string
	WiFi   *wifiPowerSavingData
}

// wifiPowerSavingData is the row's words: State is the choice in force, Next the
// one its button offers, Result what the last submission did ("applied", "saved"
// for the next activation only, or "").
type wifiPowerSavingData struct {
	State, Next, Result string
}

// wifiPowerSaving is the switch when the box has one to offer.
func (h *handler) wifiPowerSaving() WiFiPowerSaving {
	if h.cfg.WiFiPowerSaving == nil || !h.cfg.WiFiPowerSaving.Offered() {
		return nil
	}
	return h.cfg.WiFiPowerSaving
}

func (h *handler) page(w http.ResponseWriter, r *http.Request, status int, failed Action, message string) string {
	data := pageData{SSH: h.cfg.EnableSSH != nil, Simulated: h.cfg.Simulated, SSHEnabled: r.URL.Query().Get("ssh") == "enabled",
		ServerID: coreconf.ReadID(h.cfg.ServerIDFile), Username: sshaccess.Username, Failed: failed, Error: message}
	if saving := h.wifiPowerSaving(); saving != nil {
		data.WiFi = &wifiPowerSavingData{State: "off", Next: "on"}
		if saving.Enabled() {
			data.WiFi.State, data.WiFi.Next = "on", "off"
		}
		if result := r.URL.Query().Get("powersave"); result == "applied" || result == "saved" {
			data.WiFi.Result = result
		}
	}
	var body bytes.Buffer
	if err := dashboardPage.Execute(&body, data); err != nil {
		return fail(w, "not_found")
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	// Browsers suppress the form's Origin under no-referrer, even on this page's own host.
	w.Header().Set("Referrer-Policy", "same-origin")
	w.Header().Set("Content-Length", strconv.Itoa(body.Len()))
	w.WriteHeader(status)
	if r.Method != http.MethodHead {
		_, _ = w.Write(body.Bytes())
	}
	return ""
}

// form serves the page's own forms. Unlike JSON actions, a browser can send
// one without a CORS preflight, so the dashboard's exact Origin is required.
func (h *handler) form(w http.ResponseWriter, r *http.Request) string {
	saving := h.wifiPowerSaving()
	if h.cfg.EnableSSH == nil && saving == nil {
		return fail(w, "method_not_allowed")
	}
	var failed Action
	reject := func(status int, code, message string) string {
		h.page(w, r, status, failed, message)
		return code
	}
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	if r.Header.Get("Origin") != scheme+"://"+r.Host {
		return reject(http.StatusForbidden, "origin_not_allowed", "Submit the form from this dashboard.")
	}
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/x-www-form-urlencoded" {
		return reject(http.StatusUnsupportedMediaType, "unsupported_media_type", "Submit the form from this dashboard.")
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxBody)
	if r.ParseForm() != nil {
		return reject(http.StatusBadRequest, "invalid_request", "The form could not be read. Try again.")
	}
	var fields []string
	switch action := Action(r.PostForm.Get("action")); {
	case action == enableSSH && h.cfg.EnableSSH != nil:
		failed, fields = action, []string{"action", "server_id", "password", "password_confirm"}
	case action == setWiFiPowerSaving && saving != nil:
		failed, fields = action, []string{"action", "server_id", "powersave"}
	default:
		return reject(http.StatusBadRequest, "invalid_request", "Unknown dashboard action.")
	}
	if !exactly(r.PostForm, fields...) {
		return reject(http.StatusBadRequest, "invalid_request", "The form could not be read. Try again.")
	}
	if r.PostForm.Get("server_id") != coreconf.ReadID(h.cfg.ServerIDFile) {
		return reject(http.StatusConflict, "wrong_server", "The streamer's identity changed. Check its address and try again.")
	}
	if failed == enableSSH {
		return h.enableSSH(w, r, reject)
	}
	return h.setWiFiPowerSaving(w, r, saving, reject)
}

// exactly reports whether form has names and nothing else, each once.
func exactly(form url.Values, names ...string) bool {
	if len(form) != len(names) {
		return false
	}
	for _, name := range names {
		if len(form[name]) != 1 {
			return false
		}
	}
	return true
}

func (h *handler) enableSSH(w http.ResponseWriter, r *http.Request, reject func(int, string, string) string) string {
	password := r.PostForm.Get("password")
	if !sshaccess.ValidPassword(password) {
		return reject(http.StatusBadRequest, "invalid_password", "Use 12–128 characters: letters, numbers, spaces or symbols from an English keyboard.")
	}
	if password != r.PostForm.Get("password_confirm") {
		return reject(http.StatusBadRequest, "password_mismatch", "The passwords do not match. Try again.")
	}
	secret := protocol.NewSecret(password)
	defer secret.Clear()
	r.PostForm = nil
	r.Form = nil
	ctx, cancel := context.WithTimeout(context.Background(), sshSetupTimeout)
	defer cancel()
	if err := h.cfg.EnableSSH(ctx, secret); err != nil {
		var refusal *Refusal
		if errors.As(err, &refusal) {
			outcome := outcomes[refusal.Code]
			return reject(outcome.status, refusal.Code, outcome.message)
		}
		return reject(http.StatusServiceUnavailable, "ssh_setup_failed", "SSH setup failed. Check that an SSH server is installed and try again.")
	}
	http.Redirect(w, r, "/?ssh=enabled#ssh", http.StatusSeeOther)
	return "ssh_enabled"
}

func (h *handler) setWiFiPowerSaving(w http.ResponseWriter, r *http.Request, saving WiFiPowerSaving, reject func(int, string, string) string) string {
	var on bool
	switch r.PostForm.Get("powersave") {
	case "on":
		on = true
	case "off":
	default:
		return reject(http.StatusBadRequest, "invalid_request", "The form could not be read. Try again.")
	}
	ctx, cancel := context.WithTimeout(context.Background(), powerSavingTimeout)
	defer cancel()
	err := saving.Set(ctx, on)
	result := "applied"
	if errors.Is(err, wifi.ErrNotApplied) {
		result, err = "saved", nil
	}
	if err != nil {
		return reject(http.StatusServiceUnavailable, "powersave_failed", "The Wi-Fi power-saving choice could not be saved. The supervisor's journal says why.")
	}
	http.Redirect(w, r, "/?powersave="+result+"#wifi-powersave", http.StatusSeeOther)
	return "powersave_" + result
}

// asset is one file of the control page, built into the binary so the page
// works with Core, the network and the disk in any state.
type asset struct {
	file, contentType string
}

var assets = map[string]asset{
	"/app.js":    {"web/app.js", "text/javascript; charset=utf-8"},
	"/style.css": {"web/style.css", "text/css; charset=utf-8"},
	"/logo.svg":  {"web/logo.svg", "image/svg+xml"},
	"/icon.svg":  {"web/icon.svg", "image/svg+xml"},
}

func (a asset) serve(w http.ResponseWriter) string {
	b, err := web.ReadFile(a.file)
	if err != nil {
		return fail(w, "not_found")
	}
	w.Header().Set("Content-Type", a.contentType)
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	_, _ = w.Write(b)
	return ""
}
