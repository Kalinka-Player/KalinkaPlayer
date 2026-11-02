package gatt

import (
	"context"
	"errors"
	"github.com/godbus/dbus/v5"
	"kalinka/supervisor/internal/machine"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/wifi"
	"net"
	"strings"
	"testing"
)

func TestReadRequiresBlueZAndSnapshotsPages(t *testing.T) {
	m := machine.New(context.Background(), &wifi.DryRun{}, machine.Config{Test: true})
	defer m.Close()
	_, _ = m.Tick(context.Background())
	b := &Bluez{owner: ":1.42", machine: m}
	data := []byte("original page")
	c := &characteristic{bluez: b, snapshot: true, get: func() []byte { return data }, reads: map[string][]byte{}}
	opts := map[string]dbus.Variant{"device": dbus.MakeVariant(dbus.ObjectPath("/phone"))}
	if _, err := c.ReadValue(":1.99", opts); err == nil {
		t.Fatal("unprivileged D-Bus caller accepted")
	}
	if got, err := c.ReadValue(":1.42", opts); err != nil || string(got) != "original page" {
		t.Fatal("initial read")
	}
	data = []byte("changed page")
	opts["offset"] = dbus.MakeVariant(uint16(5))
	if got, err := c.ReadValue(":1.42", opts); err != nil || string(got) != "nal page" {
		t.Fatal("long read changed mid-page")
	}
	opts["offset"] = dbus.MakeVariant(uint16(500))
	if _, err := c.ReadValue(":1.42", opts); err == nil {
		t.Fatal("bad offset")
	}
	props := c.props()
	flags := props["Flags"].Value().([]string)
	if len(flags) != 2 || flags[1] != "encrypt-read" {
		t.Fatal("unencrypted read allowed")
	}
	if len(props["Value"].Value().([]byte)) != 1 {
		t.Fatal("property leaks payload")
	}
	cmd := &command{b}
	if err := cmd.WriteValue(":1.99", []byte{3, 1}, opts); err == nil {
		t.Fatal("command spoofing accepted")
	}
	a := &agent{b}
	if err := a.AuthorizeService(":1.42", "/phone", "unrelated-audio-service"); err == nil {
		t.Fatal("agent claimed audio")
	}
}

func TestEventsSayWhyBluetoothSetupStopped(t *testing.T) {
	m := machine.New(context.Background(), &wifi.DryRun{}, machine.Config{Test: true})
	defer m.Close()
	owner := func(next string) *dbus.Signal {
		return &dbus.Signal{Name: "org.freedesktop.DBus.NameOwnerChanged", Body: []any{"org.bluez", ":1.42", next}}
	}
	for _, tc := range []struct {
		name, want string
		end        func(*Bluez)
	}{
		{"bluetoothd restarted", "bluetoothd restarted", func(b *Bluez) { b.signals <- owner(":1.77") }},
		{"bluetoothd stopped", "bluetoothd stopped", func(b *Bluez) { b.signals <- owner("") }},
		{"bus closed", "lost the system bus", func(b *Bluez) { _ = b.conn.Close() }},
		{"signals closed", "lost the system bus", func(b *Bluez) { close(b.signals) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			local, remote := net.Pipe()
			defer remote.Close()
			conn, err := dbus.NewConn(local)
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			b := &Bluez{conn: conn, owner: ":1.42", machine: m, status: &characteristic{}, signals: make(chan *dbus.Signal, 2)}
			b.signals <- owner(":1.42")
			tc.end(b)
			if err := b.Events(context.Background()); !errors.Is(err, protocol.Unavailable) || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("Events = %v, want a reason containing %q", err, tc.want)
			}
		})
	}
}
