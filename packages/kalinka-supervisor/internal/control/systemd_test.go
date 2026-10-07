package control

import (
	"context"
	"errors"
	"fmt"
	"os"
	"reflect"
	"testing"

	"github.com/godbus/dbus/v5"
	"kalinka/supervisor/internal/dbusx"
)

type busCall struct {
	path   dbus.ObjectPath
	method string
	args   []any
}

type fakeBus struct {
	calls *[]busCall
	reply func(busCall) ([]any, error)
}

func (b fakeBus) Call(_ context.Context, path dbus.ObjectPath, method string, args ...any) ([]any, error) {
	c := busCall{path, method, args}
	*b.calls = append(*b.calls, c)
	return b.reply(c)
}
func (b fakeBus) Close() error { return nil }

func systemdWith(reply func(busCall) ([]any, error)) (*SystemdBus, *[]busCall) {
	calls := &[]busCall{}
	return &SystemdBus{dial: func() (dbusx.Bus, error) { return fakeBus{calls, reply}, nil }}, calls
}

func methods(calls []busCall) []string {
	var names []string
	for _, c := range calls {
		names = append(names, fmt.Sprint(c.method, c.args))
	}
	return names
}

func succeed(busCall) ([]any, error) { return nil, nil }

var noSuch = dbus.Error{Name: noSuchUnit, Body: []any{"Unit not loaded."}}

func TestRestartCoreClearsFailureFirst(t *testing.T) {
	for _, tc := range []struct {
		name     string
		resetErr error
		want     []string
		fails    bool
	}{
		{"failed or running Core", nil, []string{managerIface + ".ResetFailedUnit[kalinka.service]", managerIface + ".RestartUnit[kalinka.service replace]"}, false},
		{"unit not loaded yet", noSuch, []string{managerIface + ".ResetFailedUnit[kalinka.service]", managerIface + ".RestartUnit[kalinka.service replace]"}, false},
		{"access denied", dbus.Error{Name: "org.freedesktop.DBus.Error.AccessDenied"}, []string{managerIface + ".ResetFailedUnit[kalinka.service]"}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s, calls := systemdWith(func(c busCall) ([]any, error) {
				if c.method == managerIface+".ResetFailedUnit" {
					return nil, tc.resetErr
				}
				return nil, nil
			})
			if err := s.RestartCore(context.Background()); (err != nil) != tc.fails {
				t.Fatalf("RestartCore = %v", err)
			}
			if got := methods(*calls); !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("calls = %v", got)
			}
		})
	}
}

func TestReinstallQueuesItsUnit(t *testing.T) {
	s, calls := systemdWith(succeed)
	if err := s.Reinstall(context.Background()); err != nil {
		t.Fatal(err)
	}
	want := []string{fmt.Sprint(managerIface+".StartUnit", []any{"kalinka-reinstall.service", "replace"})}
	if got := methods(*calls); !reflect.DeepEqual(got, want) {
		t.Fatalf("calls = %v", got)
	}
}

func TestShutdownIsIrreversible(t *testing.T) {
	for _, target := range []Target{RebootTarget, PowerOffTarget} {
		s, calls := systemdWith(succeed)
		if err := s.CheckTarget(context.Background(), target); err != nil {
			t.Fatal(err)
		}
		if err := s.Shutdown(context.Background(), target); err != nil {
			t.Fatal(err)
		}
		want := []string{
			fmt.Sprint(managerIface+".LoadUnit", []any{string(target)}),
			fmt.Sprint(managerIface+".StartUnit", []any{string(target), "replace-irreversibly"}),
		}
		if got := methods(*calls); !reflect.DeepEqual(got, want) {
			t.Fatalf("calls = %v", got)
		}
	}
}

func TestUnitState(t *testing.T) {
	loaded := func(active string, job uint32) func(busCall) ([]any, error) {
		return func(c busCall) ([]any, error) {
			switch {
			case c.method == managerIface+".GetUnit":
				return []any{dbus.ObjectPath("/unit")}, nil
			case c.path == "/unit" && c.args[1] == "ActiveState":
				return []any{dbus.MakeVariant(active)}, nil
			case c.path == "/unit" && c.args[1] == "Job":
				return []any{dbus.MakeVariant([]any{job, dbus.ObjectPath("/job")})}, nil
			}
			return nil, errors.New("unexpected call")
		}
	}
	for _, tc := range []struct {
		name  string
		reply func(busCall) ([]any, error)
		want  UnitState
		busy  bool
	}{
		{"not loaded", func(busCall) ([]any, error) { return nil, noSuch }, UnitState{Active: "inactive"}, false},
		{"running oneshot", loaded("activating", 0), UnitState{Active: "activating"}, true},
		{"stopping", loaded("deactivating", 0), UnitState{Active: "deactivating"}, true},
		{"start queued", loaded("inactive", 42), UnitState{Active: "inactive", Queued: true}, true},
		{"finished", loaded("inactive", 0), UnitState{Active: "inactive"}, false},
		{"failed", loaded("failed", 0), UnitState{Active: "failed"}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s, _ := systemdWith(tc.reply)
			got, err := s.Unit(context.Background(), "kalinka-upgrade.service")
			if err != nil || got != tc.want || got.Busy() != tc.busy {
				t.Fatalf("Unit = %+v, %v (busy %v)", got, err, got.Busy())
			}
		})
	}
	s, _ := systemdWith(func(busCall) ([]any, error) { return nil, dbus.Error{Name: "org.freedesktop.DBus.Error.Timeout"} })
	if _, err := s.Unit(context.Background(), CoreUnit); err == nil {
		t.Fatal("bus failure reported as a state")
	}
	malformed := loaded("active", 0)
	s, _ = systemdWith(func(c busCall) ([]any, error) {
		if c.path == "/unit" && c.args[1] == "Job" {
			return []any{dbus.MakeVariant("no job")}, nil
		}
		return malformed(c)
	})
	if _, err := s.Unit(context.Background(), CoreUnit); err == nil {
		t.Fatal("an unreadable job reported as none")
	}
}

// Read-only against the host's systemd; it never starts, stops or restarts anything.
func TestHostSystemdReadOnly(t *testing.T) {
	if os.Getenv("KALINKA_TEST_HOST_SYSTEMD") != "1" {
		t.Skip("set KALINKA_TEST_HOST_SYSTEMD=1 to read unit state from the host's systemd")
	}
	s := NewSystemdBus()
	ctx := context.Background()
	if state, err := s.Unit(ctx, "dbus.service"); err != nil || state.Active != "active" {
		t.Fatalf("dbus.service = %+v, %v", state, err)
	}
	if state, err := s.Unit(ctx, "kalinka-test-missing.service"); err != nil || state != (UnitState{Active: "inactive"}) {
		t.Fatalf("missing unit = %+v, %v", state, err)
	}
	if err := s.CheckTarget(ctx, PowerOffTarget); err != nil {
		t.Fatalf("poweroff.target: %v", err)
	}
}

func TestDialFailure(t *testing.T) {
	s := &SystemdBus{dial: func() (dbusx.Bus, error) { return nil, errors.New("no bus") }}
	if s.RestartCore(context.Background()) == nil || s.Shutdown(context.Background(), RebootTarget) == nil {
		t.Fatal("dial failure ignored")
	}
}
