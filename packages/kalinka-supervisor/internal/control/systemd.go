// Package control serves the supervisor's privileged actions to the trusted LAN over HTTP.
package control

import (
	"context"
	"errors"
	"log/slog"

	"github.com/godbus/dbus/v5"
	"kalinka/supervisor/internal/dbusx"
)

const (
	CoreUnit     = "kalinka.service"
	systemd1     = "org.freedesktop.systemd1"
	manager      = dbus.ObjectPath("/org/freedesktop/systemd1")
	managerIface = systemd1 + ".Manager"
	unitIface    = systemd1 + ".Unit"
	noSuchUnit   = systemd1 + ".NoSuchUnit"
)

var errReply = errors.New("unexpected systemd reply")

// ReinstallUnit runs the published installer in reinstall mode; the supervisor package ships it.
const ReinstallUnit = "kalinka-reinstall.service"

// UpgradeUnits install packages; restarting or shutting down under them can leave a broken installation.
var UpgradeUnits = []string{ReinstallUnit, "kalinka-upgrade.service", "kalinka-renderer-upgrade.service", "kalinka-restart.service"}

// Target is a systemd shutdown target.
type Target string

const (
	RebootTarget   Target = "reboot.target"
	PowerOffTarget Target = "poweroff.target"
)

// UnitState is what the controller needs to know about one unit.
type UnitState struct {
	Active string
	Queued bool
}

// Busy reports whether the unit is running, stopping, or has a job waiting to start it.
func (u UnitState) Busy() bool {
	return u.Queued || u.Active != "inactive" && u.Active != "failed"
}

// Systemd is the fixed set of unit operations the supervisor performs.
// Implementations are safe for concurrent use.
type Systemd interface {
	RestartCore(context.Context) error
	// Reinstall queues ReinstallUnit and returns without waiting for it to finish.
	Reinstall(context.Context) error
	// CheckTarget confirms systemd answers and knows the target, without starting it.
	CheckTarget(context.Context, Target) error
	Shutdown(context.Context, Target) error
	Unit(ctx context.Context, name string) (UnitState, error)
}

// SystemdBus drives systemd's manager over a fresh system-bus connection per
// operation, so a restarted bus daemon never leaves it holding a dead one.
type SystemdBus struct {
	dial func() (dbusx.Bus, error)
}

func NewSystemdBus() *SystemdBus {
	return &SystemdBus{dial: func() (dbusx.Bus, error) { return dbusx.System(systemd1) }}
}

func (s *SystemdBus) with(use func(dbusx.Bus) error) error {
	bus, err := s.dial()
	if err != nil {
		return err
	}
	defer bus.Close()
	return use(bus)
}

// RestartCore clears a failed state first, so a Core that hit its start limit can still be restarted.
func (s *SystemdBus) RestartCore(ctx context.Context) error {
	return s.with(func(bus dbusx.Bus) error {
		if _, err := bus.Call(ctx, manager, managerIface+".ResetFailedUnit", CoreUnit); err != nil && dbusx.ErrorName(err) != noSuchUnit {
			return err
		}
		_, err := bus.Call(ctx, manager, managerIface+".RestartUnit", CoreUnit, "replace")
		return err
	})
}

func (s *SystemdBus) CheckTarget(ctx context.Context, t Target) error {
	return s.with(func(bus dbusx.Bus) error {
		_, err := bus.Call(ctx, manager, managerIface+".LoadUnit", string(t))
		return err
	})
}

func (s *SystemdBus) Reinstall(ctx context.Context) error {
	return s.with(func(bus dbusx.Bus) error {
		_, err := bus.Call(ctx, manager, managerIface+".StartUnit", ReinstallUnit, "replace")
		return err
	})
}

func (s *SystemdBus) Shutdown(ctx context.Context, t Target) error {
	return s.with(func(bus dbusx.Bus) error {
		_, err := bus.Call(ctx, manager, managerIface+".StartUnit", string(t), "replace-irreversibly")
		return err
	})
}

// Unit reports a unit systemd has not loaded, or does not know, as inactive.
func (s *SystemdBus) Unit(ctx context.Context, name string) (UnitState, error) {
	state := UnitState{Active: "inactive"}
	err := s.with(func(bus dbusx.Bus) error {
		body, err := bus.Call(ctx, manager, managerIface+".GetUnit", name)
		if dbusx.ErrorName(err) == noSuchUnit {
			return nil
		}
		if err != nil {
			return err
		}
		var path dbus.ObjectPath
		if dbus.Store(body, &path) != nil {
			return errReply
		}
		active, err := property(ctx, bus, path, "ActiveState")
		if err != nil {
			return err
		}
		job, err := property(ctx, bus, path, "Job")
		if err != nil {
			return err
		}
		var ok bool
		if state.Active, ok = active.Value().(string); !ok {
			return errReply
		}
		fields, ok := job.Value().([]any)
		if !ok || len(fields) != 2 {
			return errReply
		}
		id, ok := fields[0].(uint32)
		if !ok {
			return errReply
		}
		state.Queued = id != 0
		return nil
	})
	return state, err
}

func property(ctx context.Context, bus dbusx.Bus, path dbus.ObjectPath, name string) (dbus.Variant, error) {
	var v dbus.Variant
	body, err := bus.Call(ctx, path, "org.freedesktop.DBus.Properties.Get", unitIface, name)
	if err != nil {
		return v, err
	}
	if dbus.Store(body, &v) != nil {
		return v, errReply
	}
	return v, nil
}

// Simulated logs each operation instead of performing it, for test runs and unprivileged development.
type Simulated struct{}

func (Simulated) RestartCore(context.Context) error {
	slog.Info("Simulated Core restart")
	return nil
}
func (Simulated) Reinstall(context.Context) error {
	slog.Info("Simulated reinstall")
	return nil
}
func (Simulated) CheckTarget(context.Context, Target) error { return nil }
func (Simulated) Shutdown(_ context.Context, t Target) error {
	slog.Info("Simulated shutdown", "target", t)
	return nil
}
func (Simulated) Unit(_ context.Context, name string) (UnitState, error) {
	if name == CoreUnit {
		return UnitState{Active: "active"}, nil
	}
	return UnitState{Active: "inactive"}, nil
}
