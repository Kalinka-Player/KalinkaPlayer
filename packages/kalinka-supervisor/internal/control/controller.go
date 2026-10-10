package control

import (
	"context"
	"errors"
	"sync"
	"time"

	"kalinka/supervisor/internal/protocol"
)

// Action names one privileged operation a client may request.
type Action string

const (
	RestartCore Action = "restart_core"
	Reboot      Action = "reboot"
	PowerOff    Action = "poweroff"
	Reinstall   Action = "reinstall"
	WifiSetup   Action = "wifi_setup"
)

// Supported lists every action this protocol version serves, in the order /info reports them.
var Supported = []Action{RestartCore, Reboot, PowerOff, Reinstall, WifiSetup}

// setupWindow is how long WifiSetup keeps nearby setup open.
const setupWindow = 10 * time.Minute

const enableSSH Action = "enable_ssh"

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

// Refusal codes; each is a fixed, client-facing reason an action did not run.
const (
	CodeBusy             = "busy"
	CodeShuttingDown     = "shutting_down"
	CodeUpgrade          = "upgrade_in_progress"
	CodeNetworkChange    = "network_change_in_progress"
	CodeTooSoon          = "too_soon"
	CodeUnavailable      = "unavailable"
	CodeSetupUnavailable = "setup_unavailable"
	CodeSetupInUse       = "setup_in_use"
)

// Refusal says why an action did not run, and when to retry if that is known.
type Refusal struct {
	Code       string
	RetryAfter time.Duration
}

func (r *Refusal) Error() string { return r.Code }

func refuse(code string) error { return &Refusal{Code: code} }

// NearbySetup is the part of nearby BLE setup that actions coordinate with.
type NearbySetup interface {
	// ChangingNetwork reports whether a network change that a shutdown or reinstall would interrupt is underway.
	ChangingNetwork() bool
	// Available reports whether OpenSetup can reach setup now.
	Available() bool
	// OpenSetup makes setup available for d even while the box is online. It
	// returns protocol.Busy while a phone is using setup, and
	// protocol.Unavailable while Available is false.
	OpenSetup(d time.Duration) error
}

// Operation is an admitted action. Exactly one Run must follow Begin; the
// controller admits nothing else until it returns.
type Operation interface {
	Run(context.Context) error
}

// Status is a point-in-time view of what the controller looks after.
// Reinstall is "running", "succeeded", "failed" or "idle"; ReinstallFinished
// is when the last one ended, zero if none has. WifiSetup is whether that
// action can reach nearby setup now.
type Status struct {
	Core              string
	Upgrading         bool
	Pending           Action
	Reinstall         string
	ReinstallFinished time.Time
	WifiSetup         bool
}

// Controller admits one privileged operation at a time and decides whether
// each may run now. Future operations are serialized through it as well.
// Safe for concurrent use.
type Controller struct {
	systemd         Systemd
	setup           NearbySetup
	reinstalls      ReinstallRecord
	ssh             SSHSetup
	now             func() time.Time
	restartInterval time.Duration

	mu          sync.Mutex
	running     bool
	pending     Action
	lastRestart time.Time
}

// NewController takes setup as nil when nearby setup is off.
func NewController(s Systemd, setup NearbySetup, reinstalls ReinstallRecord, ssh SSHSetup, now func() time.Time) *Controller {
	return &Controller{systemd: s, setup: setup, reinstalls: reinstalls, ssh: ssh, now: now, restartInterval: 15 * time.Second}
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
	if err := c.admit(ctx, enableSSH); err != nil {
		return err
	}
	if c.ssh == nil {
		return refuse(CodeUnavailable)
	}
	return c.ssh.Enable(ctx, password)
}

// Begin admits a, or returns a *Refusal saying why it may not run now.
func (c *Controller) Begin(ctx context.Context, a Action) (Operation, error) {
	if err := c.reserve(a); err != nil {
		return nil, err
	}
	if err := c.admit(ctx, a); err != nil {
		c.mu.Lock()
		c.running = false
		c.mu.Unlock()
		return nil, err
	}
	return &operation{c, a}, nil
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

func (c *Controller) admit(ctx context.Context, a Action) error {
	setup, err := c.systemd.Unit(ctx, "kalinka-ssh-setup.service")
	if err != nil {
		return refuse(CodeUnavailable)
	}
	if setup.Busy() {
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
	if a == WifiSetup && c.setup == nil {
		return refuse(CodeSetupUnavailable)
	}
	// Core being mid-start blocks nothing: a wedged bootstrap is a main reason to reboot or reinstall.
	if c.setup != nil && c.setup.ChangingNetwork() {
		return refuse(CodeNetworkChange)
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
	action Action
}

func (o *operation) Run(ctx context.Context) error {
	c := o.c
	err := o.perform(ctx)
	c.mu.Lock()
	defer c.mu.Unlock()
	c.running = false
	if err != nil {
		c.pending = ""
		return err
	}
	if o.action == RestartCore {
		c.lastRestart = c.now()
	}
	return nil
}

// perform carries out the action, or returns a *Refusal saying why it did not happen.
func (o *operation) perform(ctx context.Context) error {
	c := o.c
	if o.action == WifiSetup {
		err := c.setup.OpenSetup(setupWindow)
		switch {
		case err == nil:
			return nil
		case errors.Is(err, protocol.Busy):
			return refuse(CodeSetupInUse)
		}
		return refuse(CodeSetupUnavailable)
	}
	var err error
	if target, terminal := o.action.target(); terminal {
		err = c.systemd.Shutdown(ctx, target)
	} else if o.action == Reinstall {
		err = c.systemd.Reinstall(ctx)
	} else {
		err = c.systemd.RestartCore(ctx)
	}
	if err != nil {
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
	s := Status{Core: core.Active, Upgrading: installing != "", Reinstall: "running", WifiSetup: c.setup != nil && c.setup.Available()}
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
