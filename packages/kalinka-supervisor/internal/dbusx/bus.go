// Package dbusx narrows the system bus to method calls on one service so callers can be faked.
package dbusx

import (
	"context"
	"errors"
	"time"

	"github.com/godbus/dbus/v5"
)

// Bus calls methods on one D-Bus service. Errors are returned unmapped, so a
// caller can tell D-Bus error names apart with ErrorName. Not safe for use
// after Close.
type Bus interface {
	Call(context.Context, dbus.ObjectPath, string, ...any) ([]any, error)
	Close() error
}

type systemBus struct {
	conn *dbus.Conn
	dest string
}

// System opens a private system-bus connection for calls to dest; the caller owns and closes it.
func System(dest string) (Bus, error) {
	conn, err := dbus.ConnectSystemBus()
	if err != nil {
		return nil, err
	}
	return &systemBus{conn, dest}, nil
}

func (b *systemBus) Call(ctx context.Context, path dbus.ObjectPath, method string, args ...any) ([]any, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	c := b.conn.Object(b.dest, path).CallWithContext(ctx, method, 0, args...)
	return c.Body, c.Err
}
func (b *systemBus) Close() error { return b.conn.Close() }

// ErrorName returns the D-Bus error name a remote call failed with, or "" for any other failure.
func ErrorName(err error) string {
	var value dbus.Error
	if errors.As(err, &value) {
		return value.Name
	}
	var pointer *dbus.Error
	if errors.As(err, &pointer) {
		return pointer.Name
	}
	return ""
}
