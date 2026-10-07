package dbusx

import (
	"context"
	"errors"
	"fmt"
	"testing"

	"github.com/godbus/dbus/v5"
)

func TestErrorName(t *testing.T) {
	remote := dbus.Error{Name: "org.freedesktop.systemd1.NoSuchUnit", Body: []any{"Unit x not loaded."}}
	for _, tc := range []struct {
		name string
		err  error
		want string
	}{
		{"remote reply", remote, remote.Name},
		{"wrapped", fmt.Errorf("call: %w", remote), remote.Name},
		{"pointer", dbus.NewError("org.example.Failed", nil), "org.example.Failed"},
		{"local failure", context.DeadlineExceeded, ""},
		{"none", nil, ""},
		{"plain", errors.New("closed"), ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := ErrorName(tc.err); got != tc.want {
				t.Fatalf("ErrorName = %q, want %q", got, tc.want)
			}
		})
	}
}
