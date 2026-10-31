package wifi

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/godbus/dbus/v5"
	"kalinka/supervisor/internal/dbusx"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/rfkill/rfkilltest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

const testDevice = dbus.ObjectPath("/device")

type nmFake struct {
	t                            *testing.T
	profiles                     map[dbus.ObjectPath]settings
	state                        uint32
	reason                       uint32
	active                       dbus.ObjectPath
	states                       []uint32
	committed, deleted, restored bool
	failSave                     bool
	cancel                       context.CancelFunc
	rollbackRemaining            time.Duration
	commands                     *[][]string
	countryBeforeJoin            bool
	wirelessEnabled              bool
	wirelessSets                 int
	refuseWireless               bool
}

func (b *nmFake) Close() error { return nil }
func (b *nmFake) Call(ctx context.Context, path dbus.ObjectPath, method string, args ...any) ([]any, error) {
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	switch method {
	case nm + ".GetDeviceByIpIface":
		return []any{testDevice}, nil
	case "org.freedesktop.DBus.Properties.GetAll":
		switch args[0] {
		case nmDevice:
			if b.active == "/activeCandidate" && len(b.states) > 0 {
				b.state = b.states[0]
				b.states = b.states[1:]
			}
			return []any{map[string]dbus.Variant{"DeviceType": v(uint32(2)), "Managed": v(true), "State": v(b.state), "StateReason": v([]any{b.state, b.reason}), "ActiveConnection": v(b.active), "Ip4Config": v(dbus.ObjectPath("/ipv4"))}}, nil
		case nm + ".Connection.Active":
			return []any{map[string]dbus.Variant{"Uuid": v("previous")}}, nil
		case nm + ".IP4Config":
			return []any{map[string]dbus.Variant{"AddressData": v([]map[string]dbus.Variant{{"address": v("192.0.2.5")}})}}, nil
		case nmWireless:
			return []any{map[string]dbus.Variant{"LastScan": v(int64(0))}}, nil
		}
	case "org.freedesktop.DBus.Properties.Get":
		if path == nmRoot && args[0] == nm && args[1] == "WirelessEnabled" {
			return []any{v(b.wirelessEnabled)}, nil
		}
	case "org.freedesktop.DBus.Properties.Set":
		if path == nmRoot && args[0] == nm && args[1] == "WirelessEnabled" {
			if b.refuseWireless {
				return nil, dbus.NewError("org.freedesktop.NetworkManager.PermissionDenied", []any{"Not authorized to enable/disable Wi-Fi"})
			}
			b.wirelessSets++
			b.wirelessEnabled = args[2].(dbus.Variant).Value().(bool)
			return nil, nil
		}
	case nmWireless + ".RequestScan":
		return nil, errors.New("scan refused")
	case nmWireless + ".GetAllAccessPoints":
		return []any{[]dbus.ObjectPath{}}, nil
	case nm + ".Settings.AddConnection2":
		b.countryBeforeJoin = b.commands != nil && slices.ContainsFunc(*b.commands, func(c []string) bool { return slices.Equal(c, []string{"iw", "reg", "set", "GB"}) })
		if args[1] != uint32(2) {
			b.t.Fatal("candidate persisted before DHCP")
		}
		s := args[0].(settings)
		if value[bool](s["connection"], "autoconnect") {
			b.t.Fatal("candidate autoconnect enabled early")
		}
		b.profiles["/candidate"] = s
		return []any{dbus.ObjectPath("/candidate"), map[string]dbus.Variant{}}, nil
	case nm + ".Settings.ListConnections":
		var paths []dbus.ObjectPath
		for p := range b.profiles {
			paths = append(paths, p)
		}
		return []any{paths}, nil
	case nmConnection + ".GetSettings":
		return []any{b.profiles[path]}, nil
	case nm + ".ActivateConnection":
		if args[0] == dbus.ObjectPath("/previous") {
			b.restored = true
			b.active = "/activePrevious"
		} else {
			b.active = "/activeCandidate"
			if b.cancel != nil {
				b.cancel()
			}
		}
		return []any{b.active}, nil
	case nmConnection + ".Update2":
		if b.failSave {
			return nil, errors.New("error containing a hypothetical password")
		}
		if b.state != 100 || args[1] != uint32(1) || !value[bool](args[0].(settings)["connection"], "autoconnect") {
			b.t.Fatal("invalid commit")
		}
		b.committed = true
		return []any{map[string]dbus.Variant{}}, nil
	case nmConnection + ".Delete":
		deadline, _ := ctx.Deadline()
		b.rollbackRemaining = time.Until(deadline)
		if path != "/candidate" {
			b.t.Fatal("deleted pre-existing profile")
		}
		delete(b.profiles, path)
		b.deleted = true
		return nil, nil
	}
	b.t.Fatalf("unexpected NM call %s %s", path, method)
	return nil, protocol.Unavailable
}
func TestNMStagingAndRollback(t *testing.T) {
	for _, scenario := range []string{"success_after_need_auth", "wrong_password", "save_failure", "cancel"} {
		t.Run(scenario, func(t *testing.T) {
			n := NewNetworkManager("wlan0", t.TempDir())
			n.Poll = time.Millisecond
			n.Timeout = time.Second
			n.RegulatoryConf = filepath.Join(t.TempDir(), "kalinka-regdom.conf")
			var commands [][]string
			n.Run = recordCommands(&commands, nil)
			b := &nmFake{t: t, state: 100, active: "/activePrevious", profiles: map[dbus.ObjectPath]settings{"/previous": {"connection": {"uuid": v("previous")}}}, states: []uint32{60, 70, 100}, commands: &commands}
			if scenario == "wrong_password" {
				b.states = []uint32{120}
				b.reason = 7
			}
			b.failSave = scenario == "save_failure"
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if scenario == "cancel" {
				b.cancel = cancel
			}
			n.Bus = b
			if err := n.Recover(ctx); err != nil {
				t.Fatal(err)
			}
			var stages []byte
			cmd := protocol.Command{SSID: "home", Country: "GB", Password: protocol.NewSecret("dummy-passphrase")}
			defer cmd.Password.Clear()
			address, err := n.Join(ctx, cmd, func(s byte) { stages = append(stages, s) })
			if strings.HasPrefix(scenario, "success") {
				if err != nil || address != "192.0.2.5" || !b.committed || b.deleted || b.restored {
					t.Fatalf("success failed %s %v", address, err)
				}
				if len(stages) < 3 {
					t.Fatal("missing progress")
				}
				if !b.countryBeforeJoin {
					t.Fatal("country not applied before the join")
				}
				if conf, err := os.ReadFile(n.RegulatoryConf); err != nil || string(conf) != "options cfg80211 ieee80211_regdom=GB\n" {
					t.Fatalf("country not kept across restarts: %q %v", conf, err)
				}
			} else {
				if err == nil || b.committed || !b.deleted || !b.restored {
					t.Fatalf("rollback failed %v", err)
				}
				if b.rollbackRemaining <= 50*time.Second || b.rollbackRemaining > RollbackTimeout {
					t.Fatal("rollback must use the shared 55-second budget independently of join cancellation")
				}
				if scenario == "wrong_password" && err != protocol.WrongPassword {
					t.Fatal("reason lost")
				}
				if strings.Contains(err.Error(), "hypothetical") {
					t.Fatal("backend error leaked")
				}
				if _, err := os.Stat(n.RegulatoryConf); !os.IsNotExist(err) {
					t.Fatal("a failed join kept its country")
				}
			}
			if _, err := os.Stat(n.Journal); !os.IsNotExist(err) {
				t.Fatal("journal remained")
			}
		})
	}
}
func recordCommands(commands *[][]string, err error) Runner {
	return func(_ context.Context, _ time.Duration, name string, args ...string) (string, error) {
		*commands = append(*commands, append([]string{name}, args...))
		return "", err
	}
}
func TestNMJoinsWhenTheRadioRefusesTheCountry(t *testing.T) {
	n := NewNetworkManager("wlan0", t.TempDir())
	n.Poll = time.Millisecond
	n.Timeout = time.Second
	n.RegulatoryConf = filepath.Join(t.TempDir(), "kalinka-regdom.conf")
	var commands [][]string
	n.Run = recordCommands(&commands, protocol.Unavailable)
	n.Bus = &nmFake{t: t, state: 100, active: "/activePrevious", profiles: map[dbus.ObjectPath]settings{"/previous": {"connection": {"uuid": v("previous")}}}, states: []uint32{100}}
	if err := n.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	cmd := protocol.Command{SSID: "home", Country: "GB", Password: protocol.NewSecret("dummy-passphrase")}
	defer cmd.Password.Clear()
	if _, err := n.Join(context.Background(), cmd, func(byte) {}); err != nil {
		t.Fatal(err)
	}
	if conf, err := os.ReadFile(n.RegulatoryConf); err != nil || !strings.Contains(string(conf), "ieee80211_regdom=GB") {
		t.Fatalf("country not kept for the next start: %q %v", conf, err)
	}
}
func TestNMKeepsAJoinWhoseCountryCannotBeKept(t *testing.T) {
	n := NewNetworkManager("wlan0", t.TempDir())
	n.Poll = time.Millisecond
	n.Timeout = time.Second
	blocker := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(blocker, nil, 0600); err != nil {
		t.Fatal(err)
	}
	n.RegulatoryConf = filepath.Join(blocker, "kalinka-regdom.conf")
	var commands [][]string
	n.Run = recordCommands(&commands, nil)
	n.Bus = &nmFake{t: t, state: 100, active: "/activePrevious", profiles: map[dbus.ObjectPath]settings{"/previous": {"connection": {"uuid": v("previous")}}}, states: []uint32{100}}
	if err := n.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	cmd := protocol.Command{SSID: "home", Country: "GB", Password: protocol.NewSecret("dummy-passphrase")}
	defer cmd.Password.Clear()
	if _, err := n.Join(context.Background(), cmd, func(byte) {}); err != nil {
		t.Fatalf("a country file that cannot be written failed the join: %v", err)
	}
	if _, err := os.Stat(n.Journal); !os.IsNotExist(err) {
		t.Fatalf("the join was left to roll back: %v", err)
	}
}
func TestNMScanAppliesCountry(t *testing.T) {
	n := NewNetworkManager("wlan0", t.TempDir())
	n.Poll = time.Millisecond
	var commands [][]string
	n.Run = recordCommands(&commands, nil)
	n.Bus = &nmFake{t: t, state: 100, active: "/activePrevious", profiles: map[dbus.ObjectPath]settings{}}
	if err := n.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := n.Scan(context.Background(), "gb"); err != protocol.Invalid {
		t.Fatalf("malformed country accepted: %v", err)
	}
	if _, err := n.Scan(context.Background(), "GB"); err != nil {
		t.Fatal(err)
	}
	if !slices.EqualFunc(commands, [][]string{{"iw", "reg", "set", "GB"}}, slices.Equal) {
		t.Fatalf("commands %v", commands)
	}
}

// The empty list a phone sees when NetworkManager refuses the scan, as it does with Wi-Fi switched off.
func TestNMScanRefusedGivesNoNetworks(t *testing.T) {
	n := NewNetworkManager("wlan0", t.TempDir())
	n.Poll = time.Millisecond
	n.Bus = &nmFake{t: t, state: 100, active: "/activePrevious", profiles: map[dbus.ObjectPath]settings{}}
	if err := n.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	networks, err := n.Scan(context.Background(), "")
	if err != nil || networks == nil || len(networks) != 0 {
		t.Fatalf("networks %v, error %v", networks, err)
	}
}
func TestNMEnableRadio(t *testing.T) {
	for _, tc := range []struct {
		name    string
		enabled bool
		sets    int
	}{{"switched off", false, 1}, {"already on", true, 0}} {
		t.Run(tc.name, func(t *testing.T) {
			n := NewNetworkManager("wlan0", t.TempDir())
			n.Root = rfkilltest.Stage(t, rfkilltest.Radio{Type: "wlan", Name: "phy0", Soft: true}, rfkilltest.Radio{Type: "bluetooth", Name: "hci0", Soft: true})
			b := &nmFake{t: t, state: 100, active: "/activePrevious", profiles: map[dbus.ObjectPath]settings{}, wirelessEnabled: tc.enabled}
			n.Bus = b
			if err := n.Recover(context.Background()); err != nil {
				t.Fatal(err)
			}
			for range 2 {
				if err := n.EnableRadio(context.Background()); err != nil {
					t.Fatal(err)
				}
			}
			if b.wirelessSets != tc.sets || !b.wirelessEnabled {
				t.Fatalf("%d sets, WirelessEnabled %v", b.wirelessSets, b.wirelessEnabled)
			}
			if rfkilltest.Soft(t, n.Root, "rfkill0") {
				t.Fatal("kernel Wi-Fi block left")
			}
			if !rfkilltest.Soft(t, n.Root, "rfkill1") {
				t.Fatal("Bluetooth cleared by the Wi-Fi switch")
			}
		})
	}
}
func TestNMEnableRadioRefused(t *testing.T) {
	n := NewNetworkManager("wlan0", t.TempDir())
	n.Root = rfkilltest.Stage(t, rfkilltest.Radio{Type: "wlan", Name: "phy0", Soft: true})
	n.Bus = &nmFake{t: t, state: 100, active: "/activePrevious", profiles: map[dbus.ObjectPath]settings{}, refuseWireless: true}
	if err := n.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	err := n.EnableRadio(context.Background())
	if dbusx.ErrorName(err) != "org.freedesktop.NetworkManager.PermissionDenied" {
		t.Fatalf("refusal lost its reason: %v", err)
	}
	if !rfkilltest.Soft(t, n.Root, "rfkill0") {
		t.Fatal("kernel block cleared behind NetworkManager's back")
	}
}
func TestNMKeepsHostCountry(t *testing.T) {
	n := NewNetworkManager("wlan0", t.TempDir())
	n.Poll = time.Millisecond
	n.Timeout = time.Second
	n.RegulatoryConf = ""
	var commands [][]string
	n.Run = recordCommands(&commands, nil)
	n.Bus = &nmFake{t: t, state: 100, active: "/activePrevious", profiles: map[dbus.ObjectPath]settings{"/previous": {"connection": {"uuid": v("previous")}}}, states: []uint32{100}}
	if err := n.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := n.Scan(context.Background(), "GB"); err != nil {
		t.Fatal(err)
	}
	cmd := protocol.Command{SSID: "home", Country: "GB", Password: protocol.NewSecret("dummy-passphrase")}
	defer cmd.Password.Clear()
	if _, err := n.Join(context.Background(), cmd, func(byte) {}); err != nil {
		t.Fatal(err)
	}
	if len(commands) != 0 {
		t.Fatalf("host country changed: %v", commands)
	}
}
func TestNMRecoveryJournalHasNoCredentials(t *testing.T) {
	n := NewNetworkManager("wlan0", t.TempDir())
	b := &nmFake{t: t, state: 100, active: "/activeCandidate", profiles: map[dbus.ObjectPath]settings{"/previous": {"connection": {"uuid": v("previous")}}, "/candidate": {"connection": {"uuid": v("candidate")}}}}
	n.Bus = b
	if err := writeJSON(n.Journal, nmRecord{"candidate", "previous"}); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(n.Journal)
	var fields map[string]any
	_ = json.Unmarshal(raw, &fields)
	if len(fields) != 2 {
		t.Fatal("journal includes more than profile identities")
	}
	if err := n.Recover(context.Background()); err != nil || !b.deleted || !b.restored {
		t.Fatalf("recovery failed %v", err)
	}
}

// Opt-in read-only smoke test against the host's real NetworkManager.
func TestNMHostReadOnly(t *testing.T) {
	if os.Getenv("KALINKA_TEST_HOST_NM") != "1" {
		t.Skip("opt-in real system bus")
	}
	n := NewNetworkManager("", t.TempDir())
	defer n.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := n.Recover(ctx); err != nil {
		t.Fatal(err)
	}
	p, err := n.props(ctx, n.device, nmDevice)
	if err != nil {
		t.Fatal(err)
	}
	var reason struct{ State, Reason uint32 }
	if err := p["StateReason"].Store(&reason); err != nil {
		t.Fatal(err)
	}
	if _, err := n.deviceAddress(ctx); err != nil {
		t.Fatal(err)
	}
	networks, err := n.Scan(ctx, "GB")
	if err != nil {
		t.Fatal(err)
	}
	if len(networks) == 0 {
		t.Log("No visible access points; scan transport checked")
	}
}
