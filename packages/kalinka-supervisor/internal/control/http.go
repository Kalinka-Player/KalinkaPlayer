package control

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"math"
	"mime"
	"net"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"kalinka/supervisor/internal/coreconf"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/soundcard"
	"kalinka/supervisor/internal/timezone"
)

const (
	// Protocol is the newest control protocol served, MinProtocol the oldest still served.
	Protocol    = 1
	MinProtocol = 1
	maxBody     = 1024
	// Each phase gets its own deadline, together under the server's write timeout.
	phaseTimeout = 5 * time.Second
	// soundcardTimeout bounds the helper, which systemd stops after 80 seconds.
	soundcardTimeout = 90 * time.Second
	// timezoneTimeout covers D-Bus starting timedated on a Pi Zero, then the display's restart.
	timezoneTimeout = 30 * time.Second
	accepted        = "accepted"
)

var outcomes = map[string]struct {
	status  int
	message string
}{
	"invalid_request":        {http.StatusBadRequest, "The request body must be a JSON object with only the streamer's identity and, for set_soundcard, a sound card name, for remove_plugin, a package name, or, for set_timezone, a time zone name"},
	"not_local":              {http.StatusForbidden, "Only the local network may use this API"},
	"host_not_allowed":       {http.StatusForbidden, "Address this streamer by its IP address or local name"},
	"origin_not_allowed":     {http.StatusForbidden, "Only pages served by this streamer may use this API"},
	"not_found":              {http.StatusNotFound, "No such resource"},
	"method_not_allowed":     {http.StatusMethodNotAllowed, "Method not allowed"},
	"wrong_server":           {http.StatusConflict, "server_id or renderer_id does not name this streamer"},
	"unknown_soundcard":      {http.StatusUnprocessableEntity, "This streamer cannot be set up for that sound card; /v1/soundcard lists the ones it can"},
	"unknown_timezone":       {http.StatusBadRequest, "This streamer's tzdata has no such time zone; /v1/timezones lists the ones it offers"},
	"unsupported_media_type": {http.StatusUnsupportedMediaType, "The request body must be application/json"},
	CodeBusy:                 {http.StatusConflict, "Another action is in progress"},
	CodeShuttingDown:         {http.StatusConflict, "The streamer is already restarting or powering off"},
	CodeUpgrade:              {http.StatusConflict, "An upgrade is installing packages; try again when it finishes"},
	CodeNetworkChange:        {http.StatusConflict, "Nearby setup is changing the network; try again when it finishes"},
	CodeTooSoon:              {http.StatusTooManyRequests, "Core was restarted moments ago"},
	CodeUnavailable:          {http.StatusServiceUnavailable, "systemd did not carry out the request"},
	CodeSoundcard:            {http.StatusInternalServerError, "The sound card could not be set up; the supervisor's journal says why"},
	CodeInvalidPackage:       {http.StatusBadRequest, "The package is not a Kalinka plugin this supervisor may remove"},
	CodeNotInstalled:         {http.StatusConflict, "No such plugin package is installed"},
	"log_unreadable":         {http.StatusInternalServerError, "The log could not be read"},
}

// pagePolicy keeps the page to its own files and out of other sites' frames, where a click could be hijacked.
const pagePolicy = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; " +
	"connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"

// Coordinator admits and runs privileged operations; Controller is the production one.
type Coordinator interface {
	Begin(context.Context, Request) (Operation, error)
	Status(context.Context) (Status, error)
}

// Soundcards is what the API knows about the box's sound card and which
// cards it may be asked for; soundcard.Detector is the production one.
type Soundcards interface {
	Supported() bool
	Offers(name string) bool
	Report() soundcard.Report
}

// Timezones is what the API knows about the box's time zone and which zones
// it may be set to; timezone.Zones is the production one.
type Timezones interface {
	Supported() bool
	Resolve(name string) (string, bool)
	Current() *string
	Offered() []string
}

// HandlerConfig is what the HTTP layer reports and checks besides the actions themselves.
// Dashboard returns a JSON-encodable snapshot of the box.
type HandlerConfig struct {
	Version        string
	ServerIDFile   string
	RendererIDFile string
	// CoreInstalled is false on a box that runs only a renderer.
	CoreInstalled func() bool
	Soundcards    Soundcards
	Timezones     Timezones
	CorePort      func() int
	Setup         func() string
	Dashboard     func(context.Context) any
	Reinstalls    Record
	Removals      PluginRemovals
	EnableSSH     func(context.Context, protocol.Secret) error
	Simulated     bool
	NetworkSetup  NetworkSetup
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
	if r.URL.Path == "/v1/setup" {
		switch r.Method {
		case http.MethodGet:
			return h.setupStatus(w, r)
		case http.MethodPost:
			return h.setupCommand(w, r)
		case http.MethodOptions:
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type, X-Kalinka-Setup-Client")
			w.WriteHeader(http.StatusNoContent)
			return ""
		default:
			return fail(w, "method_not_allowed")
		}
	}
	if r.URL.Path == "/setup" && r.Method == http.MethodGet {
		http.Redirect(w, r, "http://"+net.JoinHostPort(hostname(r.Host), strconv.Itoa(h.cfg.CorePort()))+"/?setup=network", http.StatusSeeOther)
		return ""
	}
	if h.coord == nil {
		return fail(w, "not_found")
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
		return route{http.MethodGet, func(w http.ResponseWriter, _ *http.Request) string { return serveLog(w, h.cfg.Reinstalls) }}, true
	case path == "/v1/plugin-removal/log":
		return route{http.MethodGet, func(w http.ResponseWriter, _ *http.Request) string { return serveLog(w, h.cfg.Removals.Record) }}, true
	case path == "/v1/soundcard" && h.cfg.Soundcards != nil:
		return route{http.MethodGet, h.soundcard}, true
	case path == "/v1/timezones" && slices.Contains(h.actions(), SetTimezone):
		return route{http.MethodGet, h.timezones}, true
	case strings.HasPrefix(path, "/v1/actions/"):
		a := Action(strings.TrimPrefix(path, "/v1/actions/"))
		if !slices.Contains(h.actions(), a) {
			return route{}, false
		}
		return route{http.MethodPost, func(w http.ResponseWriter, r *http.Request) string { return h.act(w, r, a) }}, true
	}
	if a, ok := assets[path]; ok {
		return route{http.MethodGet, func(w http.ResponseWriter, _ *http.Request) string { return a.serve(w) }}, true
	}
	return route{}, false
}

// actions is what this box can do: one without Core has no Core to restart
// or reinstall, only a Raspberry Pi has a sound card to choose, and only a
// box with tzdata and timedated can change its time zone.
func (h *handler) actions() []Action {
	return slices.DeleteFunc(slices.Clone(Supported), func(a Action) bool {
		switch a {
		case SetSoundcard:
			return h.cfg.Soundcards == nil || !h.cfg.Soundcards.Supported()
		case SetTimezone:
			return h.cfg.Timezones == nil || !h.cfg.Timezones.Supported()
		}
		return a.needsCore() && h.cfg.CoreInstalled != nil && !h.cfg.CoreInstalled()
	})
}

func (h *handler) info(w http.ResponseWriter, _ *http.Request) string {
	reply(w, http.StatusOK, map[string]any{
		"name": "kalinka-supervisor", "version": h.cfg.Version,
		"protocol": Protocol, "min_protocol": MinProtocol,
		"server_id":      nullable(coreconf.ReadID(h.cfg.ServerIDFile)),
		"renderer_id":    nullable(coreconf.ReadID(h.cfg.RendererIDFile)),
		"actions":        h.actions(),
		"poweroff_timer": true,
		"network_setup":  h.cfg.NetworkSetup != nil,
	}, false)
	return ""
}

func nullable(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

func (h *handler) soundcard(w http.ResponseWriter, _ *http.Request) string {
	reply(w, http.StatusOK, h.cfg.Soundcards.Report(), false)
	return ""
}

func (h *handler) timezones(w http.ResponseWriter, _ *http.Request) string {
	reply(w, http.StatusOK, map[string]any{"timezones": h.cfg.Timezones.Offered()}, false)
	return ""
}

// currentTimezone is the zone the system itself names, so a change made outside the supervisor shows too.
func (h *handler) currentTimezone() *string {
	if h.cfg.Timezones == nil {
		return nil
	}
	return h.cfg.Timezones.Current()
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
	removal := outcomeJSON(s.PluginRemoval)
	removal["package"] = nullable(s.RemovedPackage)
	reply(w, http.StatusOK, map[string]any{
		"core": s.Core, "core_port": h.cfg.CorePort(), "upgrading": s.Upgrading, "setup": h.cfg.Setup(), "pending": pending,
		"reinstall": outcomeJSON(s.Reinstall), "plugin_removal": removal, "timezone": h.currentTimezone(),
	}, false)
	return ""
}

func outcomeJSON(o Outcome) map[string]any {
	var finished *time.Time
	if !o.Finished.IsZero() {
		finished = &o.Finished
	}
	return map[string]any{"state": o.State, "finished_at": finished}
}

func (h *handler) dashboard(w http.ResponseWriter, r *http.Request) string {
	reply(w, http.StatusOK, h.cfg.Dashboard(r.Context()), false)
	return ""
}

func serveLog(w http.ResponseWriter, r Record) string {
	b, err := r.Tail(64 << 10)
	if err != nil {
		return fail(w, "log_unreadable")
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	_, _ = w.Write(b)
	return ""
}

// identity names the box an action is meant for, so a stale address cannot act on another one.
type identity struct {
	ServerID   *string `json:"server_id"`
	RendererID *string `json:"renderer_id"`
}

type soundcardBody struct {
	identity
	Soundcard *string `json:"soundcard"`
}

type packageBody struct {
	identity
	Package *string `json:"package"`
}

type timezoneBody struct {
	identity
	Timezone *string `json:"timezone"`
}

// identified reports whether id names this box: by its server where it has
// one, otherwise by its renderer. A renderer named alongside a server must
// be this box's too.
func (h *handler) identified(id identity) bool {
	server, renderer := coreconf.ReadID(h.cfg.ServerIDFile), coreconf.ReadID(h.cfg.RendererIDFile)
	matches := func(sent *string, want string) bool { return sent == nil && want == "" || sent != nil && *sent == want }
	switch {
	case server != "":
		return matches(id.ServerID, server) && (id.RendererID == nil || *id.RendererID == renderer)
	case renderer != "":
		return id.ServerID == nil && matches(id.RendererID, renderer)
	}
	return id.ServerID == nil && id.RendererID == nil
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxBody))
	decoder.DisallowUnknownFields()
	return decoder.Decode(v) == nil && decoder.Decode(&struct{}{}) == io.EOF
}

func (h *handler) act(w http.ResponseWriter, r *http.Request, a Action) string {
	if media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type")); err != nil || media != "application/json" {
		return fail(w, "unsupported_media_type")
	}
	req := Request{Action: a}
	var id identity
	if a == SetSoundcard {
		var body soundcardBody
		if !decode(w, r, &body) || body.Soundcard == nil || !soundcard.ValidName(*body.Soundcard) {
			return fail(w, "invalid_request")
		}
		id, req.Soundcard = body.identity, *body.Soundcard
	} else if a == RemovePlugin {
		var body packageBody
		if !decode(w, r, &body) || body.Package == nil {
			return fail(w, "invalid_request")
		}
		id, req.Package = body.identity, *body.Package
	} else if a == SetTimezone {
		var body timezoneBody
		if !decode(w, r, &body) || body.Timezone == nil || !timezone.ValidName(*body.Timezone) {
			return fail(w, "invalid_request")
		}
		id, req.Timezone = body.identity, *body.Timezone
	} else if !decode(w, r, &id) {
		return fail(w, "invalid_request")
	}
	if !h.identified(id) {
		return fail(w, "wrong_server")
	}
	if a == SetSoundcard && !h.cfg.Soundcards.Offers(req.Soundcard) {
		return fail(w, "unknown_soundcard")
	}
	if a == SetTimezone {
		zone, ok := h.cfg.Timezones.Resolve(req.Timezone)
		if !ok {
			return fail(w, "unknown_timezone")
		}
		req.Timezone = zone
	}
	// Not the request's context: a client hanging up must not abandon an admitted operation midway.
	admitting, stopAdmitting := context.WithTimeout(context.Background(), phaseTimeout)
	defer stopAdmitting()
	op, err := h.coord.Begin(admitting, req)
	if err != nil {
		return refusal(w, err)
	}
	limit := phaseTimeout
	switch a {
	case SetSoundcard:
		limit = soundcardTimeout
	case SetTimezone:
		limit = timezoneTimeout
	}
	running, stopRunning := context.WithTimeout(context.Background(), limit)
	defer stopRunning()
	// A reinstall or removal only queues its unit here; the work runs, and reports, from there.
	target, err := op.Run(running)
	if err != nil {
		return refusal(w, err)
	}
	answer := map[string]any{"action": a}
	if a == SetSoundcard {
		answer["reboot"] = target != ""
	}
	if a == RemovePlugin {
		answer["package"] = req.Package
	}
	if a == SetTimezone {
		answer["timezone"] = req.Timezone
	}
	if target == "" {
		reply(w, http.StatusAccepted, answer, false)
		return accepted
	}
	// The reply must be on the wire before shutdown starts taking the network down.
	reply(w, http.StatusAccepted, answer, true)
	shutting, stopShutting := context.WithTimeout(context.Background(), phaseTimeout)
	defer stopShutting()
	if op.Shutdown(shutting) != nil {
		return "failed"
	}
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
