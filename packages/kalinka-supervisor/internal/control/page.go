package control

import (
	"bytes"
	"context"
	"embed"
	"errors"
	"html/template"
	"mime"
	"net/http"
	"strconv"
	"time"

	"kalinka/supervisor/internal/coreconf"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/sshaccess"
)

//go:embed web
var web embed.FS

var dashboardPage = template.Must(template.ParseFS(web, "web/index.html"))

const sshSetupTimeout = 30 * time.Second

func (h *handler) page(w http.ResponseWriter, r *http.Request, status int, message string) string {
	var body bytes.Buffer
	err := dashboardPage.Execute(&body, struct {
		SSH, Simulated, SSHEnabled bool
		ServerID, Username, Error  string
	}{h.cfg.EnableSSH != nil, h.cfg.Simulated, r.URL.Query().Get("ssh") == "enabled",
		coreconf.ServerID(h.cfg.ServerIDFile), sshaccess.Username, message})
	if err != nil {
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

func (h *handler) enableSSH(w http.ResponseWriter, r *http.Request) string {
	reject := func(status int, code, message string) string {
		h.page(w, r, status, message)
		return code
	}
	if h.cfg.EnableSSH == nil {
		return fail(w, "method_not_allowed")
	}
	// Unlike JSON actions, a browser can send this form without a CORS preflight.
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	if r.Header.Get("Origin") != scheme+"://"+r.Host {
		return reject(http.StatusForbidden, "origin_not_allowed", "Submit SSH setup from this dashboard.")
	}
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/x-www-form-urlencoded" {
		return reject(http.StatusUnsupportedMediaType, "unsupported_media_type", "Submit SSH setup using the form below.")
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxBody)
	if r.ParseForm() != nil || len(r.PostForm) != 4 {
		return reject(http.StatusBadRequest, "invalid_request", "The SSH setup form could not be read. Try again.")
	}
	for _, name := range []string{"action", "server_id", "password", "password_confirm"} {
		if len(r.PostForm[name]) != 1 {
			return reject(http.StatusBadRequest, "invalid_request", "The SSH setup form could not be read. Try again.")
		}
	}
	if r.PostForm.Get("action") != string(enableSSH) {
		return reject(http.StatusBadRequest, "invalid_request", "Unknown dashboard action.")
	}
	if r.PostForm.Get("server_id") != coreconf.ServerID(h.cfg.ServerIDFile) {
		return reject(http.StatusConflict, "wrong_server", "The box's identity changed. Check its address and try again.")
	}
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
