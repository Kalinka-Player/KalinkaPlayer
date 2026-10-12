package control

import (
	"context"
	"errors"
	"sync"
	"time"

	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/soundcard"
)

// Action names one privileged operation a client may request.
type Action string

const (
	RestartCore  Action = "restart_core"
	Reboot       Action = "reboot"
	PowerOff     Action = "poweroff"
	Reinstall    Action = "reinstall"
	SetSoundcard Action = "set_soundcard"
)

// Supported lists every action this protocol version serves, in the order /info reports them.
var Supported = []Action{RestartCore, Reboot, PowerOff, Reinstall, SetSoundcard}

const enableSSH Action = "enable_ssh"

// helperUnits run the packaged helpers; while one is busy, no other action may start.
var helperUnits = []string{"kalinka-ssh-setup.service", soundcard.ApplyUnit}

// Request is an admitted action's input: the action, and the card set_soundcard makes the box's own.
type Request struct {
	Action    Action
	Soundcard string
}

// SoundcardApplier makes a catalogued sound card the box's own.
type SoundcardApplier interface {
	// Apply reports whether the boot configuration changed, so the card loads only after a restart.
	Apply(ctx context.Context, name string) (bool, error)
}

// SSHSetup enables the dashboard's fixed administrator account and SSH service.
type SSHSetup interface {
	Enable(context.Context, protocol.Secret) error
}

// target returns the shutdown target of an action that ends the supervisor along with the host.
func (a Action) target() (Target, bool) {
	switch a {
	case Reboot:
		return RebootTarget, true
	case PowerOff:
		return PowerOffTarget, true
	}
	return "", false
}

// needsCore reports whether the action acts on Core, which a box running only a renderer does not have.
func (a Action) needsCore() bool { return a == RestartCore || a == Reinstall }

// Refusal codes; each is a fixed, client-facing reason an action did not run.
const (
	CodeBusy          = "busy"
	CodeShuttingDown  = "shutting_down"
	CodeUpgrade       = "upgrade_in_progress"
	CodeNetworkChange = "network_change_in_progress"
	CodeTooSoon       = "too_soon"
	CodeUnavailable   = "unavailable"
	CodeSoundcard     = "soundcard_failed"
)

// Refusal says why an action did not run, and when to retry if that is known.
type Refusal struct {
	Code       string
	RetryAfter time.Duration
}

func (r *Refusal) Error() string { return r.Code }

func refuse(code string) error { return &Refusal{Code: code} }

// NetworkActivity reports whether a network change that a shutdown or reinstall would interrupt is underway.
type NetworkActivity interface {
	ChangingNetwork() bool
}

// Operation is an admitted action. Exactly one Run must follow Begin. Run
// names the shutdown the action ends in, or "" for none; a named one is
// started by exactly one Shutdown, once the reply is on the wire. The
// controller admits nothing else until the last of them returns.
type Operation interface {
	Run(context.Context) (Target, error)
	Shutdown(context.Context) error
}

// Status is a point-in-time view of what the controller looks after.
// Reinstall is "running", "succeeded", "failed" or "idle"; ReinstallFinished
// is when the last one ended, zero if none has.
type Status struct {
	Core              string
	Upgrading         bool
	Pending           Action
	Reinstall         string
	ReinstallFinished time.Time
}

// Controller admits one privileged operation at a time and decides whether
// each may run now. Future operations are serialized through it as well.
// Safe for concurrent use.
type Controller struct {
	systemd         Systemd
	network         NetworkActivity
	reinstalls      ReinstallRecord
	ssh             SSHSetup
	soundcards      SoundcardApplier
	now             func() time.Time
	restartInterval time.Duration

	mu          sync.Mutex
	running     bool
	pending     Action
	lastRestart time.Time
}

// NewController takes network as nil when nearby setup is off.
func NewController(s Systemd, network NetworkActivity, reinstalls ReinstallRecord, ssh SSHSetup, soundcards SoundcardApplier, now func() time.Time) *Controller {
	return &Controller{systemd: s, network: network, reinstalls: reinstalls, ssh: ssh, soundcards: soundcards, now: now, restartInterval: 15 * time.Second}
}

// EnableSSH shares the action lock and admission policy without adding a protocol action.
func (c *Controller) EnableSSH(ctx context.Context, password protocol.Secret) error {
	if err := c.reserve(enableSSH); err != nil {
		return err
	}
	defer func() {
		c.mu.Lock()
		c.running = false
		c.mu.Unlock()
	}()
	if err := c.admit(ctx, Request{Action: enableSSH}); err != nil {
		return err
	}
	if c.ssh == nil {
		return refuse(CodeUnavailable)
	}
	return c.ssh.Enable(ctx, password)
}

// Begin admits r, or returns a *Refusal saying why it may not run now.
func (c *Controller) Begin(ctx context.Context, r Request) (Operation, error) {
	if err := c.reserve(r.Action); err != nil {
		return nil, err
	}
	if err := c.admit(ctx, r); err != nil {
		c.mu.Lock()
		c.running = false
		c.mu.Unlock()
		return nil, err
	}
	return &operation{c: c, req: r}, nil
}

func (c *Controller) reserve(a Action) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	switch {
	case c.pending != "":
		return refuse(CodeShuttingDown)
	case c.running:
		return refuse(CodeBusy)
	}
	if a == RestartCore && !c.lastRestart.IsZero() {
		if wait := c.lastRestart.Add(c.restartInterval).Sub(c.now()); wait > 0 {
			return &Refusal{Code: CodeTooSoon, RetryAfter: wait}
		}
	}
	c.running = true
	return nil
}

func (c *Controller) admit(ctx context.Context, r Request) error {
	a := r.Action
	for _, unit := range helperUnits {
		state, err := c.systemd.Unit(ctx, unit)
		if err != nil {
			return refuse(CodeUnavailable)
		}
		if state.Busy() {
			return refuse(CodeBusy)
		}
	}
	// The boot hook waits for the network at every boot. Only its run holds actions off; its wait holds off
	// just a change of card, which would race it for the helper.
	boot, err := c.systemd.Unit(ctx, soundcard.BootUnit)
	if err != nil {
		return refuse(CodeUnavailable)
	}
	if boot.Running() || r.Action == SetSoundcard && boot.Busy() {
		return refuse(CodeBusy)
	}
	installing, err := c.installing(ctx)
	if err != nil {
		return err
	}
	if installing != "" {
		return refuse(CodeUpgrade)
	}
	if a == RestartCore {
		return nil
	}
	// Core being mid-start blocks nothing: a wedged bootstrap is a main reason to reboot or reinstall.
	if c.network != nil && c.network.ChangingNetwork() {
		return refuse(CodeNetworkChange)
	}
	if a == SetSoundcard {
		if c.soundcards == nil {
			return refuse(CodeUnavailable)
		}
		// Checked first: a card applied without the restart it needs would stay silent.
		if c.systemd.CheckTarget(ctx, RebootTarget) != nil {
			return refuse(CodeUnavailable)
		}
		return nil
	}
	target, terminal := a.target()
	if !terminal {
		return nil
	}
	if c.systemd.CheckTarget(ctx, target) != nil {
		return refuse(CodeUnavailable)
	}
	c.mu.Lock()
	c.pending = a
	c.mu.Unlock()
	return nil
}

type operation struct {
	c      *Controller
	req    Request
	target Target
}

func (o *operation) Run(ctx context.Context) (Target, error) {
	c := o.c
	switch o.req.Action {
	case Reboot, PowerOff:
		o.target, _ = o.req.Action.target()
		return o.target, nil
	case SetSoundcard:
		return o.setSoundcard(ctx)
	case Reinstall:
		return "", o.finish(c.systemd.Reinstall(ctx))
	}
	err := c.systemd.RestartCore(ctx)
	if err == nil {
		c.mu.Lock()
		c.lastRestart = c.now()
		c.mu.Unlock()
	}
	return "", o.finish(err)
}

func (o *operation) setSoundcard(ctx context.Context) (Target, error) {
	c := o.c
	reboot, err := c.soundcards.Apply(ctx, o.req.Soundcard)
	if errors.Is(err, soundcard.ErrBusy) {
		o.finish(nil)
		return "", refuse(CodeBusy)
	}
	if err != nil {
		o.finish(nil)
		return "", refuse(CodeSoundcard)
	}
	if !reboot {
		return "", o.finish(nil)
	}
	c.mu.Lock()
	c.pending = Reboot
	c.mu.Unlock()
	o.target = RebootTarget
	return o.target, nil
}

func (o *operation) Shutdown(ctx context.Context) error {
	return o.finish(o.c.systemd.Shutdown(ctx, o.target))
}

// finish releases the controller; a failure also abandons any pending shutdown, so the action can be retried.
func (o *operation) finish(err error) error {
	c := o.c
	c.mu.Lock()
	defer c.mu.Unlock()
	c.running = false
	if err != nil {
		c.pending = ""
		return refuse(CodeUnavailable)
	}
	return nil
}

// installing names the first of UpgradeUnits that is busy, or "" when none is.
func (c *Controller) installing(ctx context.Context) (string, error) {
	for _, unit := range UpgradeUnits {
		state, err := c.systemd.Unit(ctx, unit)
		if err != nil {
			return "", refuse(CodeUnavailable)
		}
		if state.Busy() {
			return unit, nil
		}
	}
	return "", nil
}

// Status never waits for an operation in progress.
func (c *Controller) Status(ctx context.Context) (Status, error) {
	core, err := c.systemd.Unit(ctx, CoreUnit)
	if err != nil {
		return Status{}, refuse(CodeUnavailable)
	}
	installing, err := c.installing(ctx)
	if err != nil {
		return Status{}, err
	}
	s := Status{Core: core.Active, Upgrading: installing != "", Reinstall: "running"}
	if installing != ReinstallUnit {
		s.Reinstall, s.ReinstallFinished = c.reinstalls.Outcome()
		if s.Reinstall == "" {
			s.Reinstall = "idle"
		}
	}
	c.mu.Lock()
	s.Pending = c.pending
	c.mu.Unlock()
	return s, nil
}
