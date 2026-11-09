package control

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"
)

func scheduledTimer(t *testing.T, systemd *fakeSystemd) (*PowerOffTimer, *time.Time) {
	t.Helper()
	controller, _ := newController(systemd, nil)
	timer := timerServer(t, controller, func(w http.ResponseWriter, r *http.Request) {
		// An idle Core would shut down now; a sleep timer must take precedence.
		_, _ = w.Write([]byte(`{"server_id":"box-1","due":true}`))
	})
	now := time.Now()
	timer.now = func() time.Time { return now }
	return timer, &now
}

func TestScheduledPowerOffExpiresWithoutCoreAndRunsOnce(t *testing.T) {
	systemd := &fakeSystemd{}
	timer, now := scheduledTimer(t, systemd)
	ctx := context.Background()
	status, err := timer.change(ctx, "box-1", 30)
	if err != nil || status.State != "scheduled" || *status.RemainingSeconds != 1800 {
		t.Fatalf("schedule: %+v %v", status, err)
	}
	timer.check(ctx)
	if len(systemd.calls) != 0 {
		t.Fatal("powered off before deadline")
	}
	*now = now.Add(29*time.Minute + time.Second)
	status, _ = timer.Status(ctx)
	if *status.RemainingSeconds != 59 {
		t.Fatalf("countdown: %+v", status)
	}
	// The scheduled path must not query Core, which may have restarted or failed.
	timer.core = nil
	*now = now.Add(59 * time.Second)
	timer.check(ctx)
	status, _ = timer.Status(ctx)
	if status.State != "powering_off" || status.RemainingSeconds != nil {
		t.Fatalf("shutdown status: %+v", status)
	}
	if _, err := timer.change(ctx, "box-1", 0); err == nil {
		t.Fatal("cancellation claimed success after shutdown admission")
	}
	if _, err := timer.change(ctx, "box-1", 60); err == nil {
		t.Fatal("scheduled while shutting down")
	}
	if got := strings.Join(systemd.calls, ","); got != "check poweroff.target,shutdown poweroff.target" {
		t.Fatalf("actions: %s", got)
	}
}

func TestScheduledPowerOffReplaceCancelAndIdentity(t *testing.T) {
	systemd := &fakeSystemd{}
	timer, now := scheduledTimer(t, systemd)
	ctx := context.Background()
	_, _ = timer.change(ctx, "box-1", 30)
	*now = now.Add(20 * time.Minute)
	_, _ = timer.change(ctx, "box-1", 90)
	*now = now.Add(10 * time.Minute)
	timer.check(ctx)
	status, _ := timer.Status(ctx)
	if *status.RemainingSeconds != 80*60 || len(systemd.calls) != 0 {
		t.Fatalf("replacement did not retire original deadline: %+v", status)
	}
	if _, err := timer.change(ctx, "other-box", 0); err == nil {
		t.Fatal("wrong owner cancelled timer")
	}
	status, err := timer.change(ctx, "box-1", 0)
	if err != nil || status.State != "on" || status.RemainingSeconds != nil || !timer.deadline.IsZero() {
		t.Fatalf("cancel: %+v %v", status, err)
	}
	// Idempotent cancellation.
	if _, err := timer.change(ctx, "box-1", 0); err != nil {
		t.Fatal(err)
	}
	_, _ = timer.change(ctx, "box-1", 30)
	timer.identity = func() string { return "box-2" }
	*now = now.Add(time.Hour)
	timer.check(ctx)
	if len(systemd.calls) != 0 || !timer.deadline.IsZero() {
		t.Fatal("timer survived a change of server identity")
	}
}

func TestScheduledPowerOffRetriesGuardsAndFailedShutdown(t *testing.T) {
	ctx := context.Background()
	systemd := &fakeSystemd{units: map[string]UnitState{UpgradeUnits[0]: {Active: "active"}}}
	timer, now := scheduledTimer(t, systemd)
	_, _ = timer.change(ctx, "box-1", 30)
	*now = now.Add(30 * time.Minute)
	timer.check(ctx)
	if len(systemd.calls) != 0 {
		t.Fatal("powered off during upgrade")
	}
	status, _ := timer.Status(ctx)
	if status.State != "scheduled" || *status.RemainingSeconds != 0 {
		t.Fatalf("lost due timer: %+v", status)
	}
	systemd.units = nil
	systemd.actErr = errors.New("unavailable")
	timer.check(ctx)
	if timer.deadline.IsZero() {
		t.Fatal("lost failed timer")
	}
	systemd.actErr = nil
	timer.check(ctx)
	if len(systemd.calls) != 4 || !timer.deadline.IsZero() {
		t.Fatalf("retry: %v", systemd.calls)
	}
}

func TestCancelledPowerOffNeverFires(t *testing.T) {
	systemd := &fakeSystemd{}
	controller, _ := newController(systemd, nil)
	timer := timerServer(t, controller, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"server_id":"box-1","due":false}`))
	})
	now := time.Now()
	timer.now = func() time.Time { return now }
	ctx := context.Background()
	_, _ = timer.change(ctx, "box-1", 30)
	_, _ = timer.change(ctx, "box-1", 0)
	now = now.Add(time.Hour)
	timer.check(ctx)
	if len(systemd.calls) != 0 {
		t.Fatalf("cancelled shutdown fired: %v", systemd.calls)
	}
}

func TestCancellationRacingShutdownCannotReportSuccess(t *testing.T) {
	entered, release := make(chan struct{}), make(chan struct{})
	coord := &fakeCoordinator{}
	coord.shutdown = func(context.Context) error {
		coord.status.Pending = PowerOff
		close(entered)
		<-release
		return nil
	}
	timer := NewPowerOffTimer(coord, nil, func() string { return "box-1" })
	now := time.Now()
	timer.now = func() time.Time { return now }
	_, _ = timer.change(context.Background(), "box-1", 30)
	now = now.Add(time.Hour)
	checked := make(chan struct{})
	go func() {
		timer.check(context.Background())
		close(checked)
	}()
	<-entered
	cancelled := make(chan error, 1)
	go func() {
		_, err := timer.change(context.Background(), "box-1", 0)
		cancelled <- err
	}()
	close(release)
	if err := <-cancelled; err == nil {
		t.Fatal("cancellation reported success while shutdown was committing")
	}
	<-checked
}

func TestPowerOffScheduleAPI(t *testing.T) {
	coord := &fakeCoordinator{}
	h := newTestHandler(t, coord, true).(*handler)
	timer := NewPowerOffTimer(coord, nil, func() string { return testID })
	h.cfg.PowerOffTimer = timer
	w := serve(h, http.MethodGet, "/info", "")
	if !strings.Contains(w.Body.String(), `"poweroff_schedule":true`) {
		t.Fatal(w.Body)
	}
	for _, minutes := range []string{"30", "60", "90"} {
		w = serve(h, http.MethodPost, "/v1/actions/schedule_poweroff", `{"server_id":"`+testID+`","minutes":`+minutes+`}`)
		if w.Code != http.StatusOK || len(coord.begun) != 0 {
			t.Fatalf("schedule: %d %s (ran %v)", w.Code, w.Body, coord.begun)
		}
	}
	w = serve(h, http.MethodGet, "/v1/power-off", "")
	var status PowerStatus
	if err := json.Unmarshal(w.Body.Bytes(), &status); err != nil || w.Code != 200 || status.State != "scheduled" || *status.RemainingSeconds != 5400 {
		t.Fatalf("status: %d %s %v", w.Code, w.Body, err)
	}
	if w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("countdown may be cached")
	}
	for _, body := range []string{
		`{}`, `null`, `{"server_id":"` + testID + `"}`,
		`{"server_id":"` + testID + `","minutes":0}`,
		`{"server_id":"` + testID + `","minutes":-30}`,
		`{"server_id":"` + testID + `","minutes":31}`,
		`{"server_id":"` + testID + `","minutes":30.5}`,
		`{"server_id":"` + testID + `","minutes":"30"}`,
		`{"server_id":"` + testID + `","minutes":30,"extra":true}`,
	} {
		w = serve(h, http.MethodPost, "/v1/actions/schedule_poweroff", body)
		if w.Code != 400 {
			t.Fatalf("accepted invalid schedule %s: %d %s", body, w.Code, w.Body)
		}
	}
	for _, action := range []string{"schedule_poweroff", "cancel_poweroff"} {
		body := `{"server_id":"another"}`
		if action == "schedule_poweroff" {
			body = `{"server_id":"another","minutes":30}`
		}
		w = serve(h, http.MethodPost, "/v1/actions/"+action, body)
		if w.Code != 409 {
			t.Fatalf("identity not checked: %d %s", w.Code, w.Body)
		}
	}
	w = serve(h, http.MethodPost, "/v1/actions/cancel_poweroff", `{"server_id":"`+testID+`"}`)
	if w.Code != 200 || !timer.deadline.IsZero() {
		t.Fatalf("cancel: %d %s", w.Code, w.Body)
	}
	coord.status.Pending = PowerOff
	w = serve(h, http.MethodGet, "/v1/power-off", "")
	if !strings.Contains(w.Body.String(), `"state":"powering_off"`) {
		t.Fatal(w.Body)
	}
	w = serve(h, http.MethodPost, "/v1/actions/cancel_poweroff", `{"server_id":"`+testID+`"}`)
	if w.Code != 409 {
		t.Fatalf("cancel during shutdown: %d %s", w.Code, w.Body)
	}
}
