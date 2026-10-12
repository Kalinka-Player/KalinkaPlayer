package control

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/soundcard"
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
func (f *fakeSystemd) RemovePlugin(context.Context) error {
	f.record("remove plugin")
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

type fakeSetup struct {
	changing, available bool
	err                 error
	opened              []time.Duration
}

func (s *fakeSetup) ChangingNetwork() bool { return s.changing }
func (s *fakeSetup) Available() bool       { return s.available }
func (s *fakeSetup) OpenSetup(d time.Duration) error {
	s.opened = append(s.opened, d)
	return s.err
}

type clock struct{ now time.Time }

func (c *clock) Now() time.Time { return c.now }

type fakeApplier struct {
	reboot  bool
	err     error
	applied []string
}

func (f *fakeApplier) Apply(_ context.Context, name string) (bool, error) {
	f.applied = append(f.applied, name)
	return f.reboot, f.err
}

// stateDir holds a directory per controller the tests build, for its removal request file.
var stateDir string

func TestMain(m *testing.M) {
	var err error
	if stateDir, err = os.MkdirTemp("", "control-test"); err != nil {
		panic(err)
	}
	code := m.Run()
	os.RemoveAll(stateDir)
	os.Exit(code)
}

// newController builds a box on which every package is installed.
func newController(s *fakeSystemd, setup NearbySetup) (*Controller, *clock) {
	c := &clock{time.Unix(1000, 0)}
	dir, err := os.MkdirTemp(stateDir, "box")
	if err != nil {
		panic(err)
	}
	return NewController(Config{
		Systemd: s, Setup: setup, Removals: NewPluginRemovals(dir),
		Installed: func(string) bool { return true }, Soundcards: &fakeApplier{}, Now: c.Now,
	}), c
}

// withPlugin makes pkg the only package installed on c's box.
func withPlugin(c *Controller, pkg string) {
	c.installed = func(name string) bool { return name == pkg }
}

type sshSetupFunc func(context.Context, protocol.Secret) error

func (f sshSetupFunc) Enable(ctx context.Context, password protocol.Secret) error {
	return f(ctx, password)
}

func TestSSHSetupSharesActionPolicy(t *testing.T) {
	for _, tc := range []struct {
		name, unit, want string
		setup            NearbySetup
		pending          Action
	}{
		{name: "available"},
		{name: "installing", unit: ReinstallUnit, want: CodeUpgrade},
		{name: "previous helper still running", unit: "kalinka-ssh-setup.service", want: CodeBusy},
		{name: "sound card applied at boot", unit: "kalinka-soundcard.service", want: CodeBusy},
		{name: "sound card being applied", unit: "kalinka-soundcard-apply.service", want: CodeBusy},
		{name: "changing network", setup: &fakeSetup{changing: true}, want: CodeNetworkChange},
		{name: "shutting down", pending: Reboot, want: CodeShuttingDown},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := &fakeSystemd{units: map[string]UnitState{tc.unit: {Active: "active"}}}
			c, _ := newController(s, tc.setup)
			c.pending = tc.pending
			called := false
			c.ssh = sshSetupFunc(func(context.Context, protocol.Secret) error { called = true; return nil })
			if err := c.EnableSSH(context.Background(), protocol.NewSecret("test-only-password")); code(err) != tc.want {
				t.Fatalf("refusal = %s, want %s", code(err), tc.want)
			}
			if called != (tc.want == "") {
				t.Fatalf("setup called = %v", called)
			}
			if c.running {
				t.Fatal("setup did not release the action lock")
			}
		})
	}
}

func TestSSHSetupExcludesOtherActionsAndReleasesLockOnFailure(t *testing.T) {
	c, _ := newController(&fakeSystemd{}, nil)
	c.ssh = sshSetupFunc(func(context.Context, protocol.Secret) error {
		for _, action := range Supported {
			if _, err := c.Begin(context.Background(), request(action)); code(err) != CodeBusy {
				t.Fatalf("%s was not excluded", action)
			}
		}
		if err := c.EnableSSH(context.Background(), protocol.NewSecret("another-password")); code(err) != CodeBusy {
			t.Fatal("parallel SSH setup was admitted")
		}
		return errors.New("setup failed")
	})
	if c.EnableSSH(context.Background(), protocol.NewSecret("test-only-password")) == nil {
		t.Fatal("failure was lost")
	}
	if err := perform(c, RestartCore); err != nil {
		t.Fatalf("setup did not release the action lock: %v", err)
	}
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

// request asks for a, naming a card or a package for the actions that take one.
func request(a Action) Request {
	r := Request{Action: a, Soundcard: "hifiberry-digi"}
	if a == RemovePlugin {
		r.Package = "kalinka-plugin-qobuz"
	}
	return r
}

// perform carries a out as the HTTP layer does: Run, then the shutdown it names.
func perform(c *Controller, a Action) error {
	op, err := c.Begin(context.Background(), request(a))
	if err != nil {
		return err
	}
	target, err := op.Run(context.Background())
	if err != nil || target == "" {
		return err
	}
	return op.Shutdown(context.Background())
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
		{RemovePlugin, []string{"remove plugin"}},
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
				if len(s.calls) != 0 || len(c.soundcards.(*fakeApplier).applied) != 0 {
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
	for _, a := range []Action{Reboot, PowerOff, Reinstall, SetSoundcard, WifiSetup} {
		setup := &fakeSetup{changing: true}
		c, _ := newController(&fakeSystemd{}, setup)
		if got := code(perform(c, a)); got != CodeNetworkChange || len(setup.opened) != 0 {
			t.Fatalf("%s: %q, setup opened %v", a, got, setup.opened)
		}
	}
	for _, a := range []Action{RestartCore, RemovePlugin} {
		c, _ := newController(&fakeSystemd{}, &fakeSetup{changing: true})
		if err := perform(c, a); err != nil {
			t.Fatalf("%s: %v", a, err)
		}
	}
}

func TestWifiSetupOpensNearbySetup(t *testing.T) {
	s := &fakeSystemd{}
	setup := &fakeSetup{}
	c, _ := newController(s, setup)
	if err := perform(c, WifiSetup); err != nil {
		t.Fatal(err)
	}
	if len(setup.opened) != 1 || setup.opened[0] != 10*time.Minute {
		t.Fatalf("setup opened for %v", setup.opened)
	}
	if len(s.calls) != 0 {
		t.Fatalf("opening setup reached systemd: %v", s.calls)
	}
	if st, _ := c.Status(context.Background()); st.Pending != "" {
		t.Fatalf("opening setup is pending like a shutdown: %+v", st)
	}
	if err := perform(c, WifiSetup); err != nil {
		t.Fatalf("setup cannot be opened again: %v", err)
	}
}

func TestWifiSetupRefusals(t *testing.T) {
	c, _ := newController(&fakeSystemd{}, nil)
	if got := code(perform(c, WifiSetup)); got != CodeSetupUnavailable {
		t.Fatalf("setup off: %q", got)
	}
	for _, tc := range []struct {
		name  string
		setup *fakeSetup
		units map[string]UnitState
		want  string
		asked int
	}{
		{"setup not running", &fakeSetup{err: protocol.Unavailable}, nil, CodeSetupUnavailable, 1},
		{"a phone using setup", &fakeSetup{err: protocol.Busy}, nil, CodeSetupInUse, 1},
		{"network change", &fakeSetup{changing: true}, nil, CodeNetworkChange, 0},
		{"upgrade", &fakeSetup{}, map[string]UnitState{UpgradeUnits[0]: {Active: "activating"}}, CodeUpgrade, 0},
	} {
		s := &fakeSystemd{units: tc.units}
		c, _ := newController(s, tc.setup)
		if got := code(perform(c, WifiSetup)); got != tc.want {
			t.Fatalf("%s: %q", tc.name, got)
		}
		if len(s.calls) != 0 || len(tc.setup.opened) != tc.asked {
			t.Fatalf("%s: systemd calls %v, setup opened %v", tc.name, s.calls, tc.setup.opened)
		}
		s.units = nil
		if err := perform(c, RestartCore); err != nil {
			t.Fatalf("%s: refusal left the controller reserved: %v", tc.name, err)
		}
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
	_, err := c.Begin(context.Background(), request(RestartCore))
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
	op, err := c.Begin(context.Background(), request(RestartCore))
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error)
	go func() {
		_, err := op.Run(context.Background())
		done <- err
	}()
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
	s := &fakeSystemd{units: map[string]UnitState{CoreUnit: {Active: "failed"}, "kalinka-upgrade.service": {Active: "activating"}}}
	c, _ := newController(s, nil)
	st, err := c.Status(context.Background())
	if err != nil || st != (Status{Core: "failed", Upgrading: true, Reinstall: Outcome{State: "idle"}, PluginRemoval: Outcome{State: "idle"}}) {
		t.Fatalf("status = %+v, %v", st, err)
	}
}

func TestStatusSaysWhetherSetupCanOpen(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup NearbySetup
		want  bool
	}{
		{"setup off", nil, false},
		{"radio not up", &fakeSetup{}, false},
		{"radio up", &fakeSetup{available: true}, true},
	} {
		c, _ := newController(&fakeSystemd{}, tc.setup)
		if st, err := c.Status(context.Background()); err != nil || st.WifiSetup != tc.want {
			t.Fatalf("%s: status = %+v, %v", tc.name, st, err)
		}
	}
}

func TestReinstallStatus(t *testing.T) {
	dir := t.TempDir()
	record := Record{ResultFile: filepath.Join(dir, "reinstall-result")}
	s := &fakeSystemd{}
	c := NewController(Config{Systemd: s, Reinstalls: record, Now: time.Now})
	status := func() Status {
		t.Helper()
		st, err := c.Status(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		return st
	}
	if st := status(); st.Reinstall.State != "idle" || !st.Reinstall.Finished.IsZero() {
		t.Fatalf("before any reinstall: %+v", st)
	}
	s.units = map[string]UnitState{ReinstallUnit: {Active: "activating"}}
	if st := status(); st.Reinstall.State != "running" || !st.Upgrading || st.PluginRemoval.State != "idle" {
		t.Fatalf("while reinstalling: %+v", st)
	}
	s.units = nil
	for result, want := range map[string]string{"success\n": "succeeded", "exit-code\n": "failed", "timeout\n": "failed"} {
		if err := os.WriteFile(record.ResultFile, []byte(result), 0600); err != nil {
			t.Fatal(err)
		}
		if st := status(); st.Reinstall.State != want || st.Reinstall.Finished.IsZero() {
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

func TestBootSoundcardHoldsActionsOffWhileItRuns(t *testing.T) {
	waiting := &fakeSystemd{units: map[string]UnitState{"kalinka-soundcard.service": {Active: "inactive", Queued: true}}}
	c, _ := newController(waiting, nil)
	if got := code(perform(c, SetSoundcard)); got != CodeBusy {
		t.Fatalf("a change of card raced the hook waiting for the network: %q", got)
	}
	if err := perform(c, Reboot); err != nil {
		t.Fatalf("a hook still waiting for the network held off a reboot: %v", err)
	}
	running := &fakeSystemd{units: map[string]UnitState{"kalinka-soundcard.service": {Active: "activating"}}}
	c, _ = newController(running, nil)
	for _, a := range Supported {
		if got := code(perform(c, a)); got != CodeBusy {
			t.Fatalf("%s while the hook runs: %q", a, got)
		}
	}
}

func TestSoundcardWithoutRestart(t *testing.T) {
	s := &fakeSystemd{}
	c, _ := newController(s, nil)
	op, err := c.Begin(context.Background(), Request{Action: SetSoundcard, Soundcard: "i2s-dac"})
	if err != nil {
		t.Fatal(err)
	}
	if target, err := op.Run(context.Background()); target != "" || err != nil {
		t.Fatalf("Run = %q, %v", target, err)
	}
	if applied := c.soundcards.(*fakeApplier).applied; len(applied) != 1 || applied[0] != "i2s-dac" {
		t.Fatalf("applied %v", applied)
	}
	if len(s.calls) != 1 || s.calls[0] != "check reboot.target" {
		t.Fatalf("calls %v", s.calls)
	}
	if st, _ := c.Status(context.Background()); st.Pending != "" {
		t.Fatalf("a card that needs no restart left %q pending", st.Pending)
	}
	if err := perform(c, RestartCore); err != nil {
		t.Fatalf("the controller was not released: %v", err)
	}
}

func TestSoundcardRestartFlowsThroughPending(t *testing.T) {
	s := &fakeSystemd{}
	c, _ := newController(s, nil)
	c.soundcards = &fakeApplier{reboot: true}
	op, err := c.Begin(context.Background(), request(SetSoundcard))
	if err != nil {
		t.Fatal(err)
	}
	if target, err := op.Run(context.Background()); target != RebootTarget || err != nil {
		t.Fatalf("Run = %q, %v", target, err)
	}
	if st, _ := c.Status(context.Background()); st.Pending != Reboot {
		t.Fatalf("pending = %q", st.Pending)
	}
	for _, a := range Supported {
		if got := code(perform(c, a)); got != CodeShuttingDown {
			t.Fatalf("%s while the box restarts for its card: %q", a, got)
		}
	}
	if err := op.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if want := []string{"check reboot.target", "shutdown reboot.target"}; len(s.calls) != 2 || s.calls[0] != want[0] || s.calls[1] != want[1] {
		t.Fatalf("calls %v", s.calls)
	}
}

func TestSoundcardRestartRefusedBySystemd(t *testing.T) {
	s := &fakeSystemd{}
	c, _ := newController(s, nil)
	c.soundcards = &fakeApplier{reboot: true}
	s.actErr = errors.New("denied")
	if got := code(perform(c, SetSoundcard)); got != CodeUnavailable {
		t.Fatalf("refused restart = %q", got)
	}
	if st, _ := c.Status(context.Background()); st.Pending != "" {
		t.Fatal("a refused restart stayed pending")
	}
	s.actErr = nil
	if err := perform(c, Reboot); err != nil {
		t.Fatalf("the restart cannot be retried: %v", err)
	}
}

func TestSoundcardChangeUnderWayIsBusy(t *testing.T) {
	c, _ := newController(&fakeSystemd{}, nil)
	c.soundcards = &fakeApplier{err: soundcard.ErrBusy}
	if got := code(perform(c, SetSoundcard)); got != CodeBusy {
		t.Fatalf("apply while the helper is held = %q", got)
	}
	if err := perform(c, RestartCore); err != nil {
		t.Fatalf("the controller was not released: %v", err)
	}
}

func TestSoundcardFailureReleasesTheController(t *testing.T) {
	c, _ := newController(&fakeSystemd{}, nil)
	c.soundcards = &fakeApplier{reboot: true, err: errors.New("helper failed")}
	if got := code(perform(c, SetSoundcard)); got != CodeSoundcard {
		t.Fatalf("failed apply = %q", got)
	}
	if st, _ := c.Status(context.Background()); st.Pending != "" {
		t.Fatal("a failed apply left a restart pending")
	}
	if err := perform(c, RestartCore); err != nil {
		t.Fatalf("the controller was not released: %v", err)
	}
}

func TestSoundcardNeedsARestartToBePossible(t *testing.T) {
	applier := &fakeApplier{}
	c, _ := newController(&fakeSystemd{checkErr: errors.New("no target")}, nil)
	c.soundcards = applier
	if got := code(perform(c, SetSoundcard)); got != CodeUnavailable || len(applier.applied) != 0 {
		t.Fatalf("applied %v without a way to restart: %q", applier.applied, got)
	}
	c, _ = newController(&fakeSystemd{}, nil)
	c.soundcards = nil
	if got := code(perform(c, SetSoundcard)); got != CodeUnavailable {
		t.Fatalf("without an applier = %q", got)
	}
}

func TestPluginRemovalWritesTheRequestBeforeStartingTheUnit(t *testing.T) {
	s := &fakeSystemd{}
	c, _ := newController(s, nil)
	withPlugin(c, "kalinka-plugin-qobuz")
	if err := perform(c, RemovePlugin); err != nil {
		t.Fatal(err)
	}
	if c.removals.Package() != "kalinka-plugin-qobuz" || len(s.calls) != 1 || s.calls[0] != "remove plugin" {
		t.Fatalf("request %q, calls %v", c.removals.Package(), s.calls)
	}
	st, _ := c.Status(context.Background())
	if st.RemovedPackage != "kalinka-plugin-qobuz" || st.Pending != "" {
		t.Fatalf("status = %+v", st)
	}
	s.units = map[string]UnitState{PluginRemovalUnit: {Active: "activating"}}
	st, _ = c.Status(context.Background())
	if st.PluginRemoval.State != "running" || !st.Upgrading || st.Reinstall.State != "idle" {
		t.Fatalf("while removing: %+v", st)
	}
	if got := code(perform(c, RestartCore)); got != CodeUpgrade {
		t.Fatalf("restart while removing = %q", got)
	}
	s.units = nil
	if err := os.WriteFile(c.removals.ResultFile, []byte("exit-code\n"), 0600); err != nil {
		t.Fatal(err)
	}
	st, _ = c.Status(context.Background())
	if st.PluginRemoval.State != "failed" || st.PluginRemoval.Finished.IsZero() || st.RemovedPackage != "kalinka-plugin-qobuz" {
		t.Fatalf("after failure: %+v", st)
	}
}

func TestPluginRemovalRefusesWhatIsNotAnInstalledPlugin(t *testing.T) {
	for _, tc := range []struct{ pkg, want string }{
		{"kalinka-plugin-spotify", CodeNotInstalled},
		{"kalinka-plugin-sdk", CodeInvalidPackage},
		{"kalinka-server", CodeInvalidPackage},
		{"", CodeInvalidPackage},
	} {
		s := &fakeSystemd{}
		c, _ := newController(s, nil)
		withPlugin(c, "kalinka-plugin-qobuz")
		c.installed = func(name string) bool {
			return name == "kalinka-plugin-qobuz" || name == "kalinka-plugin-sdk" || name == "kalinka-server"
		}
		_, err := c.Begin(context.Background(), Request{Action: RemovePlugin, Package: tc.pkg})
		if code(err) != tc.want {
			t.Errorf("%q = %q, want %q", tc.pkg, code(err), tc.want)
		}
		if len(s.calls) != 0 || c.removals.Package() != "" || c.running {
			t.Errorf("%q: acted or stayed reserved", tc.pkg)
		}
	}
	c, _ := newController(&fakeSystemd{}, nil)
	c.installed = nil
	if got := code(perform(c, RemovePlugin)); got != CodeNotInstalled {
		t.Fatalf("without a package database = %q", got)
	}
}

func TestFailedPluginRemovalStartCanBeRetried(t *testing.T) {
	s := &fakeSystemd{actErr: errors.New("denied")}
	c, _ := newController(s, nil)
	withPlugin(c, "kalinka-plugin-qobuz")
	if got := code(perform(c, RemovePlugin)); got != CodeUnavailable {
		t.Fatalf("failed start = %q", got)
	}
	s.actErr = nil
	if err := perform(c, RemovePlugin); err != nil {
		t.Fatalf("retry: %v", err)
	}
}
