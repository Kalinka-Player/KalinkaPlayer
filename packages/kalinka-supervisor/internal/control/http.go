package control

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"math"
	"mime"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"kalinka/supervisor/internal/coreconf"
	"kalinka/supervisor/internal/protocol"
)

const (
	// Protocol is the newest control protocol served, MinProtocol the oldest still served.
	Protocol    = 1
	MinProtocol = 1
	maxBody     = 1024
	// Each phase gets its own deadline, together under the server's write timeout.
	phaseTimeout = 5 * time.Second
	accepted     = "accepted"
)

var outcomes = map[string]struct {
	status  int
	message string
}{
	"invalid_request":        {http.StatusBadRequest, "The request body must be a JSON object with only server_id"},
	"not_local":              {http.StatusForbidden, "Only the local network may use this API"},
	"host_not_allowed":       {http.StatusForbidden, "Address this box by its IP address or local name"},
	"origin_not_allowed":     {http.StatusForbidden, "Only pages served by this box may use this API"},
	"not_found":              {http.StatusNotFound, "No such resource"},
	"method_not_allowed":     {http.StatusMethodNotAllowed, "Method not allowed"},
	"wrong_server":           {http.StatusConflict, "server_id does not name this box"},
	"unsupported_media_type": {http.StatusUnsupportedMediaType, "The request body must be application/json"},
	CodeBusy:                 {http.StatusConflict, "Another action is in progress"},
	CodeShuttingDown:         {http.StatusConflict, "The box is already restarting or powering off"},
	CodeUpgrade:              {http.StatusConflict, "An upgrade is installing packages; try again when it finishes"},
	CodeNetworkChange:        {http.StatusConflict, "Nearby setup is changing the network; try again when it finishes"},
	CodeTooSoon:              {http.StatusTooManyRequests, "Core was restarted moments ago"},
	CodeUnavailable:          {http.StatusServiceUnavailable, "systemd did not carry out the request"},
	"log_unreadable":         {http.StatusInternalServerError, "The reinstall log could not be read"},
}

// pagePolicy keeps the page to its own files and out of other sites' frames, where a click could be hijacked.
const pagePolicy = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; " +
	"connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"

// Coordinator admits and runs privileged operations; Controller is the production one.
type Coordinator interface {
	Begin(context.Context, Action) (Operation, error)
	Status(context.Context) (Status, error)
}

// HandlerConfig is what the HTTP layer reports and checks besides the actions themselves.
// Dashboard returns a JSON-encodable snapshot of the box.
type HandlerConfig struct {
	Version      string
	ServerIDFile string
	CorePort     func() int
	Setup        func() string
	Dashboard    func(context.Context) any
	Reinstalls   ReinstallRecord
	EnableSSH    func(context.Context, protocol.Secret) error
	Simulated    bool
}

type handler struct {
	coord Coordinator
	cfg   HandlerConfig
}

type route struct {
	method  string
	respond func(http.ResponseWriter, *http.Request) string
}

// NewHandler serves the control page and its API to the trusted LAN. Safe for concurrent use.
func NewHandler(c Coordinator, cfg HandlerConfig) http.Handler {
	return &handler{c, cfg}
}

func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	outcome := h.serve(w, r)
	if r.Method == http.MethodPost {
		slog.Info("Supervisor action", "path", r.URL.Path, "remote", r.RemoteAddr, "outcome", outcome)
	}
}

func (h *handler) serve(w http.ResponseWriter, r *http.Request) string {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Vary", "Origin")
	w.Header().Set("Content-Security-Policy", pagePolicy)
	w.Header().Set("X-Frame-Options", "DENY")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Referrer-Policy", "no-referrer")
	if !remoteAllowed(r.RemoteAddr) {
		return fail(w, "not_local")
	}
	if !hostAllowed(r.Host) {
		return fail(w, "host_not_allowed")
	}
	if origin := r.Header.Get("Origin"); origin != "" {
		if !originAllowed(origin, r.Host, h.cfg.CorePort()) {
			return fail(w, "origin_not_allowed")
		}
		w.Header().Set("Access-Control-Allow-Origin", origin)
	}
	if r.URL.Path == "/" {
		w.Header().Set("Allow", "GET, HEAD, POST, OPTIONS")
		switch r.Method {
		case http.MethodGet, http.MethodHead:
			return h.page(w, r, http.StatusOK, "")
		case http.MethodPost:
			return h.enableSSH(w, r)
		case http.MethodOptions:
			w.WriteHeader(http.StatusNoContent)
			return ""
		default:
			return fail(w, "method_not_allowed")
		}
	}
	rt, ok := h.route(r.URL.Path)
	if !ok {
		return fail(w, "not_found")
	}
	w.Header().Set("Allow", rt.method+", OPTIONS")
	switch {
	case r.Method == http.MethodOptions:
		w.Header().Set("Access-Control-Allow-Methods", rt.method)
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		w.Header().Set("Access-Control-Max-Age", "600")
		w.WriteHeader(http.StatusNoContent)
		return ""
	case r.Method == rt.method, r.Method == http.MethodHead && rt.method == http.MethodGet:
		return rt.respond(w, r)
	}
	return fail(w, "method_not_allowed")
}

func (h *handler) route(path string) (route, bool) {
	switch {
	case path == "/info":
		return route{http.MethodGet, h.info}, true
	case path == "/v1/status":
		return route{http.MethodGet, h.status}, true
	case path == "/v1/dashboard":
		return route{http.MethodGet, h.dashboard}, true
	case path == "/v1/reinstall/log":
		return route{http.MethodGet, h.reinstallLog}, true
	case strings.HasPrefix(path, "/v1/actions/"):
		a := Action(strings.TrimPrefix(path, "/v1/actions/"))
		if !slices.Contains(Supported, a) {
			return route{}, false
		}
		return route{http.MethodPost, func(w http.ResponseWriter, r *http.Request) string { return h.act(w, r, a) }}, true
	}
	if a, ok := assets[path]; ok {
		return route{http.MethodGet, func(w http.ResponseWriter, _ *http.Request) string { return a.serve(w) }}, true
	}
	return route{}, false
}

func (h *handler) info(w http.ResponseWriter, _ *http.Request) string {
	var id *string
	if s := coreconf.ServerID(h.cfg.ServerIDFile); s != "" {
		id = &s
	}
	reply(w, http.StatusOK, map[string]any{
		"name": "kalinka-supervisor", "version": h.cfg.Version,
		"protocol": Protocol, "min_protocol": MinProtocol,
		"server_id": id, "actions": Supported,
	}, false)
	return ""
}

func (h *handler) status(w http.ResponseWriter, r *http.Request) string {
	s, err := h.coord.Status(r.Context())
	if err != nil {
		return refusal(w, err)
	}
	var pending *Action
	if s.Pending != "" {
		pending = &s.Pending
	}
	var finished *time.Time
	if !s.ReinstallFinished.IsZero() {
		finished = &s.ReinstallFinished
	}
	reply(w, http.StatusOK, map[string]any{
		"core": s.Core, "core_port": h.cfg.CorePort(), "upgrading": s.Upgrading, "setup": h.cfg.Setup(), "pending": pending,
		"reinstall": map[string]any{"state": s.Reinstall, "finished_at": finished},
	}, false)
	return ""
}

func (h *handler) dashboard(w http.ResponseWriter, r *http.Request) string {
	reply(w, http.StatusOK, h.cfg.Dashboard(r.Context()), false)
	return ""
}

func (h *handler) reinstallLog(w http.ResponseWriter, _ *http.Request) string {
	b, err := h.cfg.Reinstalls.Tail(64 << 10)
	if err != nil {
		return fail(w, "log_unreadable")
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	_, _ = w.Write(b)
	return ""
}

func (h *handler) act(w http.ResponseWriter, r *http.Request, a Action) string {
	if media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type")); err != nil || media != "application/json" {
		return fail(w, "unsupported_media_type")
	}
	var body struct {
		ServerID *string `json:"server_id"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxBody))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&body) != nil || decoder.Decode(&struct{}{}) != io.EOF {
		return fail(w, "invalid_request")
	}
	want := coreconf.ServerID(h.cfg.ServerIDFile)
	if (body.ServerID == nil) != (want == "") || body.ServerID != nil && *body.ServerID != want {
		return fail(w, "wrong_server")
	}
	// Not the request's context: a client hanging up must not abandon an admitted operation midway.
	admitting, stopAdmitting := context.WithTimeout(context.Background(), phaseTimeout)
	defer stopAdmitting()
	op, err := h.coord.Begin(admitting, a)
	if err != nil {
		return refusal(w, err)
	}
	running, stopRunning := context.WithTimeout(context.Background(), phaseTimeout)
	defer stopRunning()
	// A reinstall only queues its unit here; the work runs, and reports, from there.
	if _, terminal := a.target(); terminal {
		// The reply must be on the wire before shutdown starts taking the network down.
		reply(w, http.StatusAccepted, map[string]Action{"action": a}, true)
		if op.Run(running) != nil {
			return "failed"
		}
		return accepted
	}
	if err = op.Run(running); err != nil {
		return refusal(w, err)
	}
	reply(w, http.StatusAccepted, map[string]Action{"action": a}, false)
	return accepted
}

func reply(w http.ResponseWriter, status int, v any, final bool) {
	b, _ := json.Marshal(v)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	if final {
		w.Header().Set("Connection", "close")
	}
	w.WriteHeader(status)
	_, _ = w.Write(b)
	if final {
		_ = http.NewResponseController(w).Flush()
	}
}

func fail(w http.ResponseWriter, code string) string {
	o := outcomes[code]
	reply(w, o.status, map[string]any{"detail": map[string]string{"code": code, "message": o.message}}, false)
	return code
}

func refusal(w http.ResponseWriter, err error) string {
	var r *Refusal
	if !errors.As(err, &r) {
		r = &Refusal{Code: CodeUnavailable}
	}
	if r.RetryAfter > 0 {
		w.Header().Set("Retry-After", strconv.Itoa(int(math.Ceil(r.RetryAfter.Seconds()))))
	}
	return fail(w, r.Code)
}
