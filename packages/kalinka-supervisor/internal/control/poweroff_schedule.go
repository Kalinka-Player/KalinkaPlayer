package control

import (
	"context"
	"log/slog"
	"math"
	"mime"
	"net/http"
	"time"
)

// PowerStatus describes the host while its supervisor is still reachable.
// A nil countdown means no explicit sleep timer is running.
type PowerStatus struct {
	ServerID         string `json:"server_id"`
	State            string `json:"state"`
	RemainingSeconds *int   `json:"remaining_seconds"`
}

func (t *PowerOffTimer) statusLocked(ctx context.Context) (PowerStatus, error) {
	s, err := t.coord.Status(ctx)
	if err != nil {
		return PowerStatus{}, err
	}
	result := PowerStatus{ServerID: t.identity(), State: "on"}
	switch s.Pending {
	case PowerOff:
		result.State = "powering_off"
	case Reboot, SetSoundcard:
		result.State = "restarting"
	default:
		if !t.deadline.IsZero() && t.owner == result.ServerID {
			result.State = "scheduled"
			seconds := max(0, int(math.Ceil(t.deadline.Sub(t.now()).Seconds())))
			result.RemainingSeconds = &seconds
		}
	}
	return result, nil
}

func (t *PowerOffTimer) Status(ctx context.Context) (PowerStatus, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.statusLocked(ctx)
}

// change replaces or cancels the timer without changing persisted inactivity
// settings. The deadline uses Go's monotonic clock and survives Core restarts.
func (t *PowerOffTimer) change(ctx context.Context, owner string, minutes int) (PowerStatus, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if owner == "" || owner != t.identity() {
		return PowerStatus{}, refuse("wrong_server")
	}
	status, err := t.statusLocked(ctx)
	if err != nil {
		return PowerStatus{}, err
	}
	if status.State == "powering_off" || status.State == "restarting" {
		return PowerStatus{}, refuse(CodeShuttingDown)
	}
	if minutes == 0 {
		t.deadline, t.owner = time.Time{}, ""
		status.State, status.RemainingSeconds = "on", nil
	} else {
		t.deadline = t.now().Add(time.Duration(minutes) * time.Minute)
		t.owner = status.ServerID
		seconds := minutes * 60
		status.State, status.RemainingSeconds = "scheduled", &seconds
	}
	return status, nil
}

// Called with mu held, so changing/cancelling cannot race an admitted shutdown.
func (t *PowerOffTimer) powerOffScheduled(ctx context.Context) {
	acting, cancel := context.WithTimeout(ctx, 2*phaseTimeout)
	defer cancel()
	op, err := t.coord.Begin(acting, Request{Action: PowerOff, ConfirmPowerOff: func(context.Context) bool {
		return t.owner == t.identity() && !t.now().Before(t.deadline)
	}})
	if err != nil {
		return // Keep the due timer; retry after busy/update guards clear.
	}
	if _, err = op.Run(acting); err == nil {
		err = op.Shutdown(acting)
	}
	if err != nil {
		slog.Warn("Scheduled power-off failed; will retry")
	} else {
		t.deadline, t.owner = time.Time{}, ""
		slog.Info("Scheduled power-off accepted")
	}
}

func (h *handler) powerStatus(w http.ResponseWriter, r *http.Request) string {
	status, err := h.cfg.PowerOffTimer.Status(r.Context())
	if err != nil {
		return refusal(w, err)
	}
	reply(w, http.StatusOK, status, false)
	return ""
}

func (h *handler) schedulePowerOff(w http.ResponseWriter, r *http.Request, cancel bool) string {
	if media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type")); err != nil || media != "application/json" {
		return fail(w, "unsupported_media_type")
	}
	var id identity
	minutes := 0
	if cancel {
		if !decode(w, r, &id) {
			return fail(w, "invalid_request")
		}
	} else {
		var body struct {
			identity
			Minutes *int `json:"minutes"`
		}
		if !decode(w, r, &body) || body.Minutes == nil || (*body.Minutes != 30 && *body.Minutes != 60 && *body.Minutes != 90) {
			return fail(w, "invalid_request")
		}
		id, minutes = body.identity, *body.Minutes
	}
	if !h.identified(id) || id.ServerID == nil || *id.ServerID == "" {
		return fail(w, "wrong_server")
	}
	status, err := h.cfg.PowerOffTimer.change(r.Context(), *id.ServerID, minutes)
	if err != nil {
		return refusal(w, err)
	}
	reply(w, http.StatusOK, status, false)
	return accepted
}
