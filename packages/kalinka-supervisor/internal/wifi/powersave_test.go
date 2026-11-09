package wifi

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/godbus/dbus/v5"
	"kalinka/supervisor/internal/dbusx"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/rfkill/rfkilltest"
)

type reloadFake struct {
	reloads []uint32
	err     error
	closed  bool
}

func (b *reloadFake) Close() error { b.closed = true; return nil }
func (b *reloadFake) Call(_ context.Context, path dbus.ObjectPath, method string, args ...any) ([]any, error) {
	if path != nmRoot || method != nm+".Reload" {
		return nil, errors.New("unexpected call " + method)
	}
	b.reloads = append(b.reloads, args[0].(uint32))
	return nil, b.err
}

// stagedPowerSaving is a box with two wireless interfaces, a wired one and a
// NetworkManager that answers reloads over bus.
func stagedPowerSaving(t *testing.T, commands *[][]string, runErr error) (*PowerSaving, *reloadFake) {
	t.Helper()
	root := t.TempDir()
	rfkilltest.LinkWiFi(t, root, "wlan0", "phy0")
	rfkilltest.LinkWiFi(t, root, "wlan1", "phy1")
	for _, dir := range []string{"sys/class/net/eth0", filepath.Dir(PowerSavingConf)} {
		if err := os.MkdirAll(filepath.Join(root, dir), 0755); err != nil {
			t.Fatal(err)
		}
	}
	bus := &reloadFake{}
	p := NewPowerSaving(root, false)
	p.Run = recordCommands(commands, runErr)
	p.Dial = func() (dbusx.Bus, error) { return bus, nil }
	return p, bus
}

func savedChoice(t *testing.T, p *PowerSaving) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(p.Root, PowerSavingConf))
	if err != nil {
		return ""
	}
	return string(b)
}

func iwCommands(iface, state string) [][]string {
	return [][]string{{"iw", "dev", iface, "set", "power_save", state}}
}

func TestPowerSavingIsOffUntilTheBoxChooses(t *testing.T) {
	var commands [][]string
	p, bus := stagedPowerSaving(t, &commands, nil)
	if !p.Offered() || p.Enabled() {
		t.Fatal("a box with Wi-Fi starts on the package default, off")
	}
	if err := p.Set(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	if conf := savedChoice(t, p); !strings.HasSuffix(conf, "\n[connection]\nwifi.powersave=3\n") || !strings.HasPrefix(conf, "# ") {
		t.Fatalf("choice saved for NetworkManager as %q", conf)
	}
	if !slices.Equal(bus.reloads, []uint32{1}) || !bus.closed {
		t.Fatalf("NetworkManager reread its configuration with flags %v, connection closed %v", bus.reloads, bus.closed)
	}
	if want := append(iwCommands("wlan0", "on"), iwCommands("wlan1", "on")...); !reflect.DeepEqual(commands, want) {
		t.Fatalf("running connections switched with %v", commands)
	}
	if !p.Enabled() {
		t.Fatal("the choice does not read back")
	}
	commands = nil
	if err := p.Set(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	if p.Enabled() || !strings.HasSuffix(savedChoice(t, p), "wifi.powersave=2\n") || !reflect.DeepEqual(commands, append(iwCommands("wlan0", "off"), iwCommands("wlan1", "off")...)) {
		t.Fatalf("turning off saved %q and ran %v", savedChoice(t, p), commands)
	}
}

func TestPowerSavingReadsAChoiceMadeByHand(t *testing.T) {
	for conf, want := range map[string]bool{
		"[connection]\nwifi.powersave=3\n":      true,
		"[connection]\n wifi.powersave = 3 \n":  true,
		"[connection]\nwifi.powersave=2\n":      false,
		"[connection]\nwifi.powersave=0\n":      false,
		"[connection]\nwifi.powersave=enable\n": false,
		"[connection]\n":                        false,
		"not a keyfile":                         false,
	} {
		p := NewPowerSaving(t.TempDir(), false)
		path := filepath.Join(p.Root, PowerSavingConf)
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(conf), 0644); err != nil {
			t.Fatal(err)
		}
		if p.Enabled() != want {
			t.Errorf("%q read as on=%v", conf, !want)
		}
	}
}

func TestPowerSavingSaysWhenOnlyTheNextActivationFollows(t *testing.T) {
	t.Run("NetworkManager does not answer", func(t *testing.T) {
		var commands [][]string
		p, bus := stagedPowerSaving(t, &commands, nil)
		bus.err = errors.New("not running")
		if err := p.Set(context.Background(), true); err != ErrNotApplied {
			t.Fatalf("Set = %v", err)
		}
		if !p.Enabled() || len(commands) != 0 {
			t.Fatalf("saved %v, ran %v before NetworkManager knew the choice", p.Enabled(), commands)
		}
	})
	t.Run("the radio refuses", func(t *testing.T) {
		var commands [][]string
		p, bus := stagedPowerSaving(t, &commands, protocol.Unavailable)
		if err := p.Set(context.Background(), true); err != ErrNotApplied {
			t.Fatalf("Set = %v", err)
		}
		if !p.Enabled() || len(bus.reloads) != 1 || len(commands) != 1 {
			t.Fatalf("saved %v, reloads %v, ran %v", p.Enabled(), bus.reloads, commands)
		}
	})
	t.Run("the choice cannot be saved", func(t *testing.T) {
		var commands [][]string
		p, bus := stagedPowerSaving(t, &commands, nil)
		if err := os.RemoveAll(filepath.Join(p.Root, "etc")); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(p.Root, "etc"), nil, 0644); err != nil {
			t.Fatal(err)
		}
		if err := p.Set(context.Background(), true); err != protocol.Storage {
			t.Fatalf("Set = %v", err)
		}
		if p.Enabled() || len(bus.reloads) != 0 || len(commands) != 0 {
			t.Fatal("an unsaved choice was applied")
		}
	})
}

func TestPowerSavingIsOfferedOnlyWithWiFi(t *testing.T) {
	p := NewPowerSaving(t.TempDir(), false)
	if err := os.MkdirAll(filepath.Join(p.Root, "sys/class/net/eth0"), 0755); err != nil {
		t.Fatal(err)
	}
	if p.Offered() {
		t.Fatal("offered on a wired-only box")
	}
	rfkilltest.LinkWiFi(t, p.Root, "wlan0", "phy0")
	if !p.Offered() {
		t.Fatal("not offered with a wireless interface")
	}
	if !NewPowerSaving(t.TempDir(), true).Offered() {
		t.Fatal("a simulated run shows every control")
	}
}

func TestPowerSavingSimulatedChangesNothing(t *testing.T) {
	p := NewPowerSaving(t.TempDir(), true)
	p.Run = func(context.Context, time.Duration, string, ...string) (string, error) {
		t.Fatal("a simulated run ran iw")
		return "", nil
	}
	p.Dial = func() (dbusx.Bus, error) {
		t.Fatal("a simulated run reached NetworkManager")
		return nil, nil
	}
	if p.Enabled() {
		t.Fatal("simulation starts on")
	}
	if err := p.Set(context.Background(), true); err != nil || !p.Enabled() {
		t.Fatalf("simulated choice = %v, on %v", err, p.Enabled())
	}
	if _, err := os.Stat(filepath.Join(p.Root, PowerSavingConf)); !os.IsNotExist(err) {
		t.Fatal("a simulated run wrote the choice")
	}
}
