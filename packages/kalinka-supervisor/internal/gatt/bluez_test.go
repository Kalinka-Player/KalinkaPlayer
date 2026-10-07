package gatt

import (
	"context"
	"github.com/godbus/dbus/v5"
	"kalinka/supervisor/internal/machine"
	"kalinka/supervisor/internal/wifi"
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
