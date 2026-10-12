package control

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"kalinka/supervisor/internal/coreconf"
)

func timerServer(t *testing.T, c Coordinator, handler http.HandlerFunc) *PowerOffTimer {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	_, port, _ := net.SplitHostPort(server.Listener.Addr().String())
	n, _ := strconv.Atoi(port)
	timer := NewPowerOffTimer(c, func() coreconf.Config {
		return coreconf.Config{Interface: coreconf.AllInterfaces, Port: n}
	}, func() string { return "box-1" })
	t.Cleanup(timer.client.CloseIdleConnections)
	return timer
}

func TestPowerOffTimerRequiresFreshMatchingDecision(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		status     int
	}{
		{"disabled", `{"server_id":"box-1","due":false}`, 200},
		{"wrong box", `{"server_id":"box-2","due":true}`, 200},
		{"missing identity", `{"due":true}`, 200},
		{"missing due", `{"server_id":"box-1"}`, 200},
		{"invalid due", `{"server_id":"box-1","due":"true"}`, 200},
		{"malformed", `{"server_id":"box-1","due":true`, 200},
		{"trailing data", `{"server_id":"box-1","due":true}{}`, 200},
		{"too large", `{"padding":"` + strings.Repeat("x", 1024) + `","server_id":"box-1","due":true}`, 200},
		{"old core", `{}`, 404},
		{"core failing", `{"server_id":"box-1","due":true}`, 500},
		{"redirect", `{"server_id":"box-1","due":true}`, 302},
	} {
		t.Run(tc.name, func(t *testing.T) {
			systemd := &fakeSystemd{}
			controller, _ := newController(systemd, nil)
			timer := timerServer(t, controller, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Location", "/redirected")
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			})
			timer.check(context.Background())
			if len(systemd.calls) != 0 {
				t.Fatalf("unexpected actions: %v", systemd.calls)
			}
		})
	}
}

func TestPowerOffTimerUsesControllerAndNeverRepeatsAcceptedShutdown(t *testing.T) {
	systemd := &fakeSystemd{}
	controller, _ := newController(systemd, nil)
	timer := timerServer(t, controller, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/server/power-off-timer" || r.Method != http.MethodGet {
			t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"server_id":"box-1","due":true}`))
	})
	timer.check(context.Background())
	timer.check(context.Background())
	if got := strings.Join(systemd.calls, ","); got != "check poweroff.target,shutdown poweroff.target" {
		t.Fatalf("actions: %s", got)
	}
}

func TestPowerOffTimerRespectsBusyGuardsAndRechecksBeforeRetry(t *testing.T) {
	for _, guard := range []string{"upgrade", "reinstall", "network", "helper"} {
		t.Run(guard, func(t *testing.T) {
			systemd := &fakeSystemd{units: map[string]UnitState{}}
			setup := &fakeSetup{changing: guard == "network"}
			switch guard {
			case "upgrade":
				systemd.units[UpgradeUnits[0]] = UnitState{Active: "active"}
			case "reinstall":
				systemd.units[ReinstallUnit] = UnitState{Active: "activating"}
			case "helper":
				systemd.units[helperUnits[0]] = UnitState{Active: "activating"}
			}
			controller, _ := newController(systemd, setup)
			due := true
			timer := timerServer(t, controller, func(w http.ResponseWriter, r *http.Request) {
				_, _ = w.Write([]byte(`{"server_id":"box-1","due":` + strconv.FormatBool(due) + `}`))
			})
			timer.check(context.Background())
			if len(systemd.calls) != 0 {
				t.Fatalf("shutdown during %s: %v", guard, systemd.calls)
			}
			systemd.units = map[string]UnitState{}
			controller.setup = nil
			due = false
			timer.check(context.Background())
			if len(systemd.calls) != 0 {
				t.Fatal("reused an old due decision")
			}
			due = true
			timer.check(context.Background())
			if len(systemd.calls) != 2 {
				t.Fatalf("did not retry: %v", systemd.calls)
			}
		})
	}
}

func TestPowerOffTimerFailedShutdownCanRetry(t *testing.T) {
	systemd := &fakeSystemd{actErr: errors.New("unavailable")}
	controller, _ := newController(systemd, nil)
	timer := timerServer(t, controller, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"server_id":"box-1","due":true}`))
	})
	timer.check(context.Background())
	systemd.actErr = nil
	timer.check(context.Background())
	if len(systemd.calls) != 4 {
		t.Fatalf("did not retry failed shutdown: %v", systemd.calls)
	}
}

func TestPowerOffTimerRechecksAfterAdmission(t *testing.T) {
	systemd := &fakeSystemd{}
	controller, _ := newController(systemd, nil)
	reads := 0
	timer := timerServer(t, controller, func(w http.ResponseWriter, r *http.Request) {
		reads++
		_, _ = w.Write([]byte(`{"server_id":"box-1","due":` + strconv.FormatBool(reads == 1) + `}`))
	})
	timer.check(context.Background())
	if reads != 2 || strings.Join(systemd.calls, ",") != "check poweroff.target" {
		t.Fatalf("did not recheck before shutdown: reads=%d, calls=%v", reads, systemd.calls)
	}
	// A cancelled automatic shutdown must not reserve the box indefinitely.
	op, err := controller.Begin(context.Background(), Request{Action: RestartCore})
	if err != nil {
		t.Fatalf("cancelled timer blocked another action: %v", err)
	}
	if _, err := op.Run(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestPowerOffTimerFollowsCoreInterfaceAndPort(t *testing.T) {
	timer := timerServer(t, nil, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"server_id":"box-1","due":true}`))
	})
	cfg := timer.core()
	cfg.Interface = "eth0"
	timer.core = func() coreconf.Config { return cfg }
	timer.resolve = func(iface string) (string, error) {
		if iface != "eth0" {
			t.Fatalf("interface: %s", iface)
		}
		return "127.0.0.1", nil
	}
	if !timer.due(context.Background()) {
		t.Fatal("did not follow Core's interface")
	}
	timer.resolve = func(string) (string, error) { return "", errors.New("no address") }
	if timer.due(context.Background()) {
		t.Fatal("missing interface authorized shutdown")
	}
	timer.identity = func() string { return "" }
	if timer.due(context.Background()) {
		t.Fatal("missing local identity authorized shutdown")
	}
}

func TestPowerOffTimerCancellationAndTimeout(t *testing.T) {
	timer := timerServer(t, nil, func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	})
	timer.client.Timeout = 10 * time.Millisecond
	if timer.due(context.Background()) {
		t.Fatal("timeout authorized shutdown")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	timer.Run(ctx)
}
