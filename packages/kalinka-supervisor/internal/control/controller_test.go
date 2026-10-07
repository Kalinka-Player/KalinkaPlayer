package control

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

type fakeSystemd struct {
	mu              sync.Mutex
	units           map[string]UnitState
	unitErr, actErr error
	checkErr        error
	calls           []string
	block           chan struct{}
}

func (f *fakeSystemd) record(call string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, call)
}
func (f *fakeSystemd) RestartCore(context.Context) error {
	if f.block != nil {
		<-f.block
	}
	f.record("restart")
	return f.actErr
}
func (f *fakeSystemd) Reinstall(context.Context) error {
	f.record("reinstall")
	return f.actErr
}
func (f *fakeSystemd) CheckTarget(_ context.Context, t Target) error {
	f.record("check " + string(t))
	return f.checkErr
}
func (f *fakeSystemd) Shutdown(_ context.Context, t Target) error {
	f.record("shutdown " + string(t))
	return f.actErr
}
func (f *fakeSystemd) Unit(_ context.Context, name string) (UnitState, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.unitErr != nil {
		return UnitState{}, f.unitErr
	}
	if s, ok := f.units[name]; ok {
		return s, nil
	}
	return UnitState{Active: "inactive"}, nil
}

type fakeNetwork bool

func (n fakeNetwork) ChangingNetwork() bool { return bool(n) }

type clock struct{ now time.Time }

func (c *clock) Now() time.Time { return c.now }

func newController(s *fakeSystemd, network NetworkActivity) (*Controller, *clock) {
	c := &clock{time.Unix(1000, 0)}
	return NewController(s, network, ReinstallRecord{}, c.Now), c
}

func code(err error) string {
	var r *Refusal
	if errors.As(err, &r) {
		return r.Code
	}
	if err != nil {
		return "unexpected: " + err.Error()
	}
	return ""
}

func perform(c *Controller, a Action) error {
	op, err := c.Begin(context.Background(), a)
	if err != nil {
		return err
	}
	return op.Run(context.Background())
}

func TestActionsReachSystemd(t *testing.T) {
	for _, tc := range []struct {
		action Action
		want   []string
	}{
		{RestartCore, []string{"restart"}},
		{Reboot, []string{"check reboot.target", "shutdown reboot.target"}},
		{PowerOff, []string{"check poweroff.target", "shutdown poweroff.target"}},
		{Reinstall, []string{"reinstall"}},
	} {
		s := &fakeSystemd{}
		c, _ := newController(s, nil)
		if err := perform(c, tc.action); err != nil {
			t.Fatalf("%s: %v", tc.action, err)
		}
		if len(s.calls) != len(tc.want) {
			t.Fatalf("%s: calls %v", tc.action, s.calls)
		}
		for i := range tc.want {
			if s.calls[i] != tc.want[i] {
				t.Fatalf("%s: calls %v", tc.action, s.calls)
			}
		}
	}
}

func TestUpgradeRefusesEveryAction(t *testing.T) {
	for _, unit := range UpgradeUnits {
		for _, state := range []UnitState{{Active: "activating"}, {Active: "deactivating"}, {Active: "active"}, {Active: "inactive", Queued: true}} {
			for _, a := range Supported {
				s := &fakeSystemd{units: map[string]UnitState{unit: state}}
				c, _ := newController(s, nil)
				if got := code(perform(c, a)); got != CodeUpgrade {
					t.Fatalf("%s %+v %s: %q", unit, state, a, got)
				}
				if len(s.calls) != 0 {
					t.Fatalf("acted during an upgrade: %v", s.calls)
				}
			}
		}
		s := &fakeSystemd{units: map[string]UnitState{unit: {Active: "failed"}}}
		c, _ := newController(s, nil)
		if err := perform(c, Reboot); err != nil {
			t.Fatalf("a failed upgrade blocks reboot: %v", err)
		}
	}
}

func TestCoreStartingDoesNotBlockShutdown(t *testing.T) {
	s := &fakeSystemd{units: map[string]UnitState{CoreUnit: {Active: "activating"}}}
	c, _ := newController(s, nil)
	if err := perform(c, PowerOff); err != nil {
		t.Fatal(err)
	}
}

func TestNetworkChangeBlocksAllButRestart(t *testing.T) {
	for _, a := range []Action{Reboot, PowerOff, Reinstall} {
		c, _ := newController(&fakeSystemd{}, fakeNetwork(true))
		if got := code(perform(c, a)); got != CodeNetworkChange {
			t.Fatalf("%s: %q", a, got)
		}
	}
	c, _ := newController(&fakeSystemd{}, fakeNetwork(true))
	if err := perform(c, RestartCore); err != nil {
		t.Fatal(err)
	}
}

func TestShutdownIsTerminal(t *testing.T) {
	s := &fakeSystemd{}
	c, _ := newController(s, nil)
	if err := perform(c, Reboot); err != nil {
		t.Fatal(err)
	}
	for _, a := range Supported {
		if got := code(perform(c, a)); got != CodeShuttingDown {
			t.Fatalf("%s after reboot: %q", a, got)
		}
	}
	if st, err := c.Status(context.Background()); err != nil || st.Pending != Reboot {
		t.Fatalf("status = %+v, %v", st, err)
	}
}

func TestFailedShutdownCanBeRetried(t *testing.T) {
	s := &fakeSystemd{actErr: errors.New("denied")}
	c, _ := newController(s, nil)
	if got := code(perform(c, PowerOff)); got != CodeUnavailable {
		t.Fatalf("failed shutdown = %q", got)
	}
	if st, _ := c.Status(context.Background()); st.Pending != "" {
		t.Fatal("failed shutdown left pending")
	}
	s.actErr = nil
	if err := perform(c, PowerOff); err != nil {
		t.Fatal(err)
	}
}

func TestRestartRateLimit(t *testing.T) {
	c, clk := newController(&fakeSystemd{}, nil)
	if err := perform(c, RestartCore); err != nil {
		t.Fatal(err)
	}
	clk.now = clk.now.Add(5 * time.Second)
	_, err := c.Begin(context.Background(), RestartCore)
	var r *Refusal
	if !errors.As(err, &r) || r.Code != CodeTooSoon || r.RetryAfter != 10*time.Second {
		t.Fatalf("second restart = %v", err)
	}
	clk.now = clk.now.Add(10 * time.Second)
	if err := perform(c, RestartCore); err != nil {
		t.Fatalf("restart after the interval: %v", err)
	}
	if err := perform(c, Reboot); err != nil {
		t.Fatalf("reboot right after a restart: %v", err)
	}
}

func TestFailedRestartCanBeRetriedAtOnce(t *testing.T) {
	s := &fakeSystemd{actErr: errors.New("denied")}
	c, _ := newController(s, nil)
	if got := code(perform(c, RestartCore)); got != CodeUnavailable {
		t.Fatalf("failed restart = %q", got)
	}
	s.actErr = nil
	if err := perform(c, RestartCore); err != nil {
		t.Fatalf("retry after a failed restart: %v", err)
	}
}

func TestOneOperationAtATime(t *testing.T) {
	s := &fakeSystemd{block: make(chan struct{})}
	c, _ := newController(s, nil)
	op, err := c.Begin(context.Background(), RestartCore)
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error)
	go func() { done <- op.Run(context.Background()) }()
	if got := code(perform(c, Reboot)); got != CodeBusy {
		t.Fatalf("second operation = %q", got)
	}
	if _, err := c.Status(context.Background()); err != nil {
		t.Fatalf("status blocked by an operation: %v", err)
	}
	close(s.block)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if err := perform(c, Reboot); err != nil {
		t.Fatalf("controller not released: %v", err)
	}
}

func TestSystemdUnavailable(t *testing.T) {
	c, _ := newController(&fakeSystemd{unitErr: errors.New("no bus")}, nil)
	if got := code(perform(c, RestartCore)); got != CodeUnavailable {
		t.Fatalf("unit lookup failure = %q", got)
	}
	if _, err := c.Status(context.Background()); code(err) != CodeUnavailable {
		t.Fatalf("status = %v", err)
	}
	c, _ = newController(&fakeSystemd{checkErr: errors.New("no target")}, nil)
	if got := code(perform(c, Reboot)); got != CodeUnavailable {
		t.Fatalf("target check failure = %q", got)
	}
	if err := perform(c, RestartCore); err != nil {
		t.Fatalf("refusal left the controller reserved: %v", err)
	}
}

func TestStatus(t *testing.T) {
	s := &fakeSystemd{units: map[string]UnitState{CoreUnit: {Active: "failed"}, UpgradeUnits[1]: {Active: "activating"}}}
	c, _ := newController(s, nil)
	st, err := c.Status(context.Background())
	if err != nil || st != (Status{Core: "failed", Upgrading: true, Reinstall: "idle"}) {
		t.Fatalf("status = %+v, %v", st, err)
	}
}

func TestReinstallStatus(t *testing.T) {
	dir := t.TempDir()
	record := ReinstallRecord{ResultFile: filepath.Join(dir, "reinstall-result")}
	s := &fakeSystemd{}
	c := NewController(s, nil, record, time.Now)
	status := func() Status {
		t.Helper()
		st, err := c.Status(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		return st
	}
	if st := status(); st.Reinstall != "idle" || !st.ReinstallFinished.IsZero() {
		t.Fatalf("before any reinstall: %+v", st)
	}
	s.units = map[string]UnitState{ReinstallUnit: {Active: "activating"}}
	if st := status(); st.Reinstall != "running" || !st.Upgrading {
		t.Fatalf("while reinstalling: %+v", st)
	}
	s.units = nil
	for result, want := range map[string]string{"success\n": "succeeded", "exit-code\n": "failed", "timeout\n": "failed"} {
		if err := os.WriteFile(record.ResultFile, []byte(result), 0600); err != nil {
			t.Fatal(err)
		}
		if st := status(); st.Reinstall != want || st.ReinstallFinished.IsZero() {
			t.Fatalf("after %q: %+v", result, st)
		}
	}
}

func TestReinstallIsNotTerminal(t *testing.T) {
	s := &fakeSystemd{}
	c, _ := newController(s, nil)
	if err := perform(c, Reinstall); err != nil {
		t.Fatal(err)
	}
	if st, _ := c.Status(context.Background()); st.Pending != "" {
		t.Fatalf("a reinstall is pending like a shutdown: %+v", st)
	}
	s.units = map[string]UnitState{ReinstallUnit: {Active: "activating"}}
	if got := code(perform(c, RestartCore)); got != CodeUpgrade {
		t.Fatalf("restart while reinstalling = %q", got)
	}
}
