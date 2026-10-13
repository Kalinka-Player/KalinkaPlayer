package control

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"time"

	"kalinka/supervisor/internal/coreconf"
)

// PowerOffTimer follows Core's inactivity clock and applies the normal shutdown guards.
// A missing, unreachable or older Core never authorizes a shutdown.
type PowerOffTimer struct {
	coord    Coordinator
	core     func() coreconf.Config
	identity func() string
	resolve  func(string) (string, error)
	client   *http.Client
}

func NewPowerOffTimer(coord Coordinator, core func() coreconf.Config, identity func() string) *PowerOffTimer {
	return &PowerOffTimer{
		coord: coord, core: core, identity: identity, resolve: InterfaceIPv4,
		client: &http.Client{
			Timeout:       3 * time.Second,
			Transport:     &http.Transport{Proxy: nil},
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}
}

// Run consults a fresh Core decision every five seconds until ctx ends.
func (t *PowerOffTimer) Run(ctx context.Context) {
	defer t.client.CloseIdleConnections()
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			t.check(ctx)
		}
	}
}

func (t *PowerOffTimer) due(ctx context.Context) bool {
	id := t.identity()
	if id == "" {
		return false
	}
	cfg := t.core()
	host := "127.0.0.1"
	if cfg.Interface != coreconf.AllInterfaces {
		var err error
		host, err = t.resolve(cfg.Interface)
		if err != nil || net.ParseIP(host) == nil {
			return false
		}
	}
	url := "http://" + net.JoinHostPort(host, strconv.Itoa(cfg.Port)) + "/server/power-off-timer"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return false
	}
	res, err := t.client.Do(req)
	if err != nil {
		return false
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return false
	}
	var decision struct {
		ServerID string `json:"server_id"`
		Due      bool   `json:"due"`
	}
	decoder := json.NewDecoder(io.LimitReader(res.Body, 1024))
	return decoder.Decode(&decision) == nil && decoder.Decode(&struct{}{}) == io.EOF && decision.ServerID == id && decision.Due
}

func (t *PowerOffTimer) check(ctx context.Context) {
	if !t.due(ctx) {
		return
	}
	acting, cancel := context.WithTimeout(ctx, 2*phaseTimeout)
	defer cancel()
	op, err := t.coord.Begin(acting, Request{Action: PowerOff, ConfirmPowerOff: t.due})
	if err != nil {
		return
	}
	if _, err = op.Run(acting); err == nil {
		err = op.Shutdown(acting)
	}
	if err != nil {
		slog.Warn("Inactivity power-off failed; a fresh Core decision is required to retry")
	} else {
		slog.Info("Inactivity power-off accepted")
	}
}
