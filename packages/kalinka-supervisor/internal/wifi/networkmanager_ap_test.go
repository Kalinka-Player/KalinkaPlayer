package wifi

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/godbus/dbus/v5"
	"kalinka/supervisor/internal/protocol"
)

type nmAPFake struct {
	nmFake
	deactivated bool
}

func (b *nmAPFake) Call(ctx context.Context, path dbus.ObjectPath, method string, args ...any) ([]any, error) {
	if method == "org.freedesktop.DBus.Properties.GetAll" {
		if args[0] == nm+".Connection.Active" && b.active == "/activeCandidate" {
			return []any{map[string]dbus.Variant{"Uuid": b.profiles["/candidate"]["connection"]["uuid"]}}, nil
		}
		if args[0] == nm+".IP4Config" {
			return []any{map[string]dbus.Variant{"AddressData": v([]map[string]dbus.Variant{{"address": v(APAddress)}})}}, nil
		}
	}
	if method == nm+".DeactivateConnection" {
		b.active = "/"
		b.deactivated = true
		return nil, nil
	}
	return b.nmFake.Call(ctx, path, method, args...)
}
func TestNMHotspotIsTemporaryAndRecovered(t *testing.T) {
	for _, failure := range []bool{false, true} {
		t.Run(map[bool]string{false: "normal", true: "DNS failure"}[failure], func(t *testing.T) {
			n := NewNetworkManager("wlan0", t.TempDir())
			n.Runtime = t.TempDir()
			n.Poll = time.Millisecond
			b := &nmAPFake{nmFake: nmFake{t: t, state: 100, active: "/", profiles: map[dbus.ObjectPath]settings{"/previous": {"connection": {"uuid": v("previous")}}}}}
			n.Bus = b
			process := &fakeAPProcess{}
			n.StartAPProcess = func(context.Context, string, ...string) (APProcess, error) {
				if failure {
					return nil, protocol.Unavailable
				}
				return process, nil
			}
			if err := n.Recover(context.Background()); err != nil {
				t.Fatal(err)
			}
			err := n.StartAP(context.Background(), "Kalinka-Setup-ABCD")
			if failure {
				if err == nil || !b.deleted || !b.deactivated {
					t.Fatal("failed activation left AP behind", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			s := b.profiles["/candidate"]
			if value[string](s["802-11-wireless"], "mode") != "ap" || value[string](s["ipv4"], "method") != "manual" || value[bool](s["connection"], "autoconnect") {
				t.Fatal("AP profile persistent or routes client traffic")
			}
			if value[string](s["802-11-wireless"], "assigned-mac-address") != "stable" || value[string](s["connection"], "stable-id") != "kalinka-setup-hotspot" {
				t.Fatal("AP serves under the address the box joins networks with")
			}
			if _, err := os.Stat(n.apRecord()); err != nil {
				t.Fatal("missing crash recovery record")
			}
			if err := n.StopAP(context.Background()); err != nil {
				t.Fatal(err)
			}
			if !process.stopped || !b.deleted || !b.deactivated || len(b.profiles) != 1 {
				t.Fatal("cleanup did not preserve saved profile")
			}
		})
	}
}
