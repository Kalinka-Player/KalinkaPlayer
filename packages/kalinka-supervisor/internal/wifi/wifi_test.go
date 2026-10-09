package wifi

import (
	"context"
	"errors"
	"kalinka/supervisor/internal/protocol"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestTransactionCrashRecovery(t *testing.T) {
	root := t.TempDir()
	old := filepath.Join(root, "old")
	created := filepath.Join(root, "created")
	_ = os.WriteFile(old, []byte("original"), 0640)
	tx := Transaction{filepath.Join(root, "journal"), []string{old, created}}
	if err := tx.Begin(); err != nil {
		t.Fatal(err)
	}
	if err := tx.Begin(); err != protocol.Busy {
		t.Fatal("overwrote pending rollback")
	}
	_ = os.WriteFile(old, []byte("candidate"), 0600)
	_ = os.WriteFile(created, []byte("candidate"), 0600)
	restarted := Transaction{tx.Dir, tx.Paths}
	if err := restarted.Restore(); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(old)
	info, _ := os.Stat(old)
	if string(b) != "original" || info.Mode().Perm() != 0640 {
		t.Fatal("original not restored")
	}
	if _, err := os.Stat(created); !os.IsNotExist(err) {
		t.Fatal("new file remained")
	}
	if restarted.Pending() {
		t.Fatal("journal remained")
	}
}
func TestDietPiConfig(t *testing.T) {
	ssid := `Alice's $(touch /tmp/never) wifi`
	db, err := updateDatabase("aWIFI_SSID[0]='saved'\naWIFI_KEY[0]='old'\n", ssid, "derived-key")
	if err != nil {
		t.Fatal(err)
	}
	db2, err := updateDatabase(db, ssid, "new-key")
	if err != nil || strings.Count(db2, "aWIFI_SSID[") != 2 || !strings.Contains(db2, "aWIFI_KEY[0]='old'") {
		t.Fatal("database update changed another slot")
	}
	var full strings.Builder
	for i := range 5 {
		full.WriteString("aWIFI_SSID[" + string(rune('0'+i)) + "]='saved" + string(rune('0'+i)) + "'\n")
	}
	if _, err := updateDatabase(full.String(), "new", "key"); err != protocol.Storage {
		t.Fatal("evicted saved network")
	}
	original := "ctrl_interface=/run/wpa_supplicant\nnetwork={\n ssid=\"saved\"\n}\nnetwork={\n ssid=\"disabled\"\n disabled=2\n}\n"
	staged, states, err := stageNetworks(original)
	if err != nil || strings.Join(states, ",") != "0,2" || strings.Count(staged, "disabled=1") != 2 {
		t.Fatal("staging")
	}
	if _, _, err := stageNetworks("network={ssid=\"unknown syntax\"}\n"); err != protocol.Storage {
		t.Fatal("unknown syntax accepted")
	}
	for _, value := range []string{"'unterminated", "'' second"} {
		if _, err := shellWord(value); err == nil {
			t.Errorf("unsafe shell word accepted %q", value)
		}
	}
}
func TestParseScanSecurityAndDedup(t *testing.T) {
	scan := "BSS aa\n\tsignal: -61.00 dBm\n\tSSID: Caf\\xc3\\xa9\n\tRSN:\n\t\t * Authentication suites: PSK SAE\nBSS bb\n\tsignal: -42.00 dBm\n\tSSID: Caf\\xc3\\xa9\n\tRSN:\n\t\t * Authentication suites: PSK\nBSS cc\n\tsignal: -70.00 dBm\n\tSSID: WPA3\n\tRSN:\n\t\t * Authentication suites: SAE\nBSS dd\n\tsignal: -80.00 dBm\n\tSSID: public\n"
	got := ParseScan(scan)
	if len(got) != 3 || got[0].SSID != "Café" || got[0].Signal != -42 || got[0].Security != "wpa2" || got[1].Security != "unsupported" || got[2].Security != "open" {
		t.Fatalf("unexpected scan: %+v", got)
	}
}

type controlFake struct {
	failSave, wrongKey bool
	commands           []string
}

func (c *controlFake) Close() error { return nil }
func (c *controlFake) Events(context.Context) ([]string, error) {
	if c.wrongKey {
		return []string{"CTRL-EVENT-SSID-TEMP-DISABLED reason=WRONG_KEY"}, nil
	}
	return nil, nil
}
func (c *controlFake) Request(_ context.Context, s string) (string, error) {
	c.commands = append(c.commands, s)
	switch s {
	case "PING":
		return "PONG", nil
	case "LIST_NETWORKS":
		return "network id / ssid / bssid / flags\n0\tsaved\tany\t[DISABLED]", nil
	case "GET_NETWORK 0 ssid":
		return `"saved"`, nil
	case "GET_NETWORK 1 ssid":
		return `"candidate"`, nil
	case "ADD_NETWORK":
		return "1", nil
	case "STATUS":
		return "wpa_state=COMPLETED\nid=1", nil
	case "SAVE_CONFIG":
		if c.failSave {
			return "", protocol.Unavailable
		}
	}
	return "OK", nil
}
func TestDietPiJoinAndRollback(t *testing.T) {
	for _, scenario := range []string{"success", "wrong_password", "save_failure", "cancel"} {
		t.Run(scenario, func(t *testing.T) {
			root := t.TempDir()
			d, err := NewDietPi("wlan0", root, filepath.Join(root, "state"), filepath.Join(root, "run"))
			if err != nil {
				t.Fatal(err)
			}
			files := map[string]string{d.Tool: "tool", d.Conf: "update_config=1\nnetwork={\n ssid=\"saved\"\n}\n", d.Database: "aWIFI_SSID[0]='saved'\n", d.CountryFile: "AUTO_SETUP_NET_WIFI_COUNTRY_CODE=US\n", d.Transaction.Paths[3]: "old interfaces\n"}
			for p, s := range files {
				if err := AtomicWrite(p, []byte(s), 0600); err != nil {
					t.Fatal(err)
				}
			}
			var mu sync.Mutex
			var calls []string
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			d.Run = func(ctx context.Context, _ time.Duration, name string, args ...string) (string, error) {
				mu.Lock()
				defer mu.Unlock()
				calls = append(calls, name+" "+strings.Join(args, " "))
				if name == d.Tool {
					_ = os.WriteFile(d.Transaction.Paths[3], []byte("candidate interfaces"), 0600)
				}
				if name == "systemctl" && len(args) > 0 && args[0] == "--no-block" {
					deadline, ok := ctx.Deadline()
					if !ok || time.Until(deadline) <= 50*time.Second || time.Until(deadline) > RollbackTimeout {
						t.Error("rollback must use the shared 55-second budget independently of join cancellation")
					}
					b, _ := os.ReadFile(d.Transaction.Paths[3])
					if string(b) != files[d.Transaction.Paths[3]] {
						t.Error("restart before restored configuration")
					}
				}
				return "", nil
			}
			c := &controlFake{failSave: scenario == "save_failure", wrongKey: scenario == "wrong_password"}
			d.OpenControl = func(string, string) (Control, error) {
				if scenario == "cancel" {
					cancel()
				}
				return c, nil
			}
			d.AddressFor = func(context.Context, string) (string, error) { return "192.0.2.5", nil }
			d.Poll = time.Millisecond
			cmd := protocol.Command{SSID: "candidate", Country: "GB", Password: protocol.NewSecret("dummy-passphrase")}
			defer cmd.Password.Clear()
			ip, err := d.Join(ctx, cmd, func(byte) {})
			mu.Lock()
			all := strings.Join(calls, "\n")
			mu.Unlock()
			if strings.Contains(all, "ifup --force") {
				t.Fatal("network daemon would belong to supervisor process group")
			}
			if strings.Contains(all, cmd.Password.Reveal()) || strings.Contains(all, "candidate interfaces") {
				t.Fatal("credential-bearing argv")
			}
			if scenario == "success" {
				if err != nil || ip != "192.0.2.5" {
					t.Fatalf("join %s %v", ip, err)
				}
				b, _ := os.ReadFile(d.Database)
				if !strings.Contains(string(b), "candidate") {
					t.Fatal("did not persist")
				}
				if !strings.Contains(strings.Join(c.commands, "\n"), "ENABLE_NETWORK 0") {
					t.Fatal("saved network not restored")
				}
			} else {
				if err == nil {
					t.Fatal("expected error")
				}
				for _, p := range d.Transaction.Paths {
					b, e := os.ReadFile(p)
					if old, exists := files[p]; exists {
						if e != nil || string(b) != old {
							t.Errorf("rollback failed: %s", p)
						}
					} else if !os.IsNotExist(e) {
						t.Errorf("candidate file remained: %s", p)
					}
				}
				if !strings.Contains(all, "systemctl --no-block restart kalinka-wifi@wlan0.service") {
					t.Fatal("missing restored network restart")
				}
			}
			if d.Transaction.Pending() {
				t.Fatal("transaction remained")
			}
		})
	}
}
func TestRunnerRedactsErrorsAndBoundsCancellation(t *testing.T) {
	_, err := Run(context.Background(), time.Second, "sh", "-c", "echo secret >&2; exit 1")
	if err != protocol.Unavailable {
		t.Fatal("command output exposed")
	}
	start := time.Now()
	_, err = Run(context.Background(), 20*time.Millisecond, "sh", "-c", "sleep 30 & wait")
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) > 2*time.Second {
		t.Fatal("child process not cancelled")
	}
}

const (
	routeHeader = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
	wlanDefault = "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
	wlanSubnet  = "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n"
	cableSubnet = "eth0\t000A0A0A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
	ethDefault  = "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
	ethSubnet   = "eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
)

type stagedLink struct {
	name      string
	flags     net.Flags
	physical  bool
	addresses []string
}

func stageHost(t *testing.T, routes string, links ...stagedLink) Host {
	t.Helper()
	root := t.TempDir()
	if err := AtomicWrite(filepath.Join(root, "proc/net/route"), []byte(routeHeader+routes), 0600); err != nil {
		t.Fatal(err)
	}
	var interfaces []net.Interface
	addresses := map[string][]net.Addr{}
	for _, l := range links {
		dir := filepath.Join(root, "sys/class/net", l.name)
		if l.physical {
			dir = filepath.Join(dir, "device")
		}
		if err := os.MkdirAll(dir, 0700); err != nil {
			t.Fatal(err)
		}
		interfaces = append(interfaces, net.Interface{Name: l.name, Flags: l.flags})
		for _, cidr := range l.addresses {
			ip, network, err := net.ParseCIDR(cidr)
			if err != nil {
				t.Fatal(err)
			}
			addresses[l.name] = append(addresses[l.name], &net.IPNet{IP: ip, Mask: network.Mask})
		}
	}
	return Host{
		Root:       root,
		Interfaces: func() ([]net.Interface, error) { return interfaces, nil },
		Addrs:      func(i *net.Interface) ([]net.Addr, error) { return addresses[i.Name], nil },
	}
}
func TestAddressCountsOnlyARoutedPhysicalInterface(t *testing.T) {
	up := net.FlagUp | net.FlagRunning
	lo := stagedLink{"lo", up | net.FlagLoopback, false, []string{"127.0.0.1/8"}}
	cable := stagedLink{"eth0", up, true, []string{"10.10.10.1/24"}}
	wlan := stagedLink{"wlan0", up, true, []string{"192.168.1.20/24"}}
	unaddressed := stagedLink{"wlan0", up, true, nil}
	for _, c := range []struct {
		name, routes, device, want string
		links                      []stagedLink
	}{
		{"Wi-Fi beside a cable to an amplifier", wlanDefault + cableSubnet + wlanSubnet, "", "192.168.1.20", []stagedLink{lo, cable, wlan}},
		{"routed Wi-Fi without an address beside the cable", wlanDefault + cableSubnet, "", "", []stagedLink{lo, cable, unaddressed}},
		{"only the cable once Wi-Fi breaks", cableSubnet, "", "", []stagedLink{lo, cable, unaddressed}},
		{"single interface", wlanDefault + wlanSubnet, "", "192.168.1.20", []stagedLink{lo, wlan}},
		{"default route on loopback", "lo\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n", "", "", []stagedLink{{"lo", up | net.FlagLoopback, true, []string{"192.0.2.9/32"}}}},
		{"default route on a virtual interface", "tun0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n", "", "", []stagedLink{lo, {"tun0", up, false, []string{"10.8.0.2/24"}}}},
		{"half of the address space is no default route", "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000080\t0\t0\t0\n", "", "", []stagedLink{lo, wlan}},
		{"named device without a default route", wlanDefault + cableSubnet + wlanSubnet, "eth0", "10.10.10.1", []stagedLink{lo, cable, wlan}},
	} {
		t.Run(c.name, func(t *testing.T) {
			host := stageHost(t, c.routes, c.links...)
			if got, err := host.Address(context.Background(), c.device); err != nil || got != c.want {
				t.Fatalf("address %q error %v, want %q", got, err, c.want)
			}
		})
	}
}
func TestAddressReadsRoutesOnlyWithoutADevice(t *testing.T) {
	host := stageHost(t, "", stagedLink{"eth0", net.FlagUp | net.FlagRunning, true, []string{"10.10.10.1/24"}})
	if err := os.Remove(filepath.Join(host.Root, "proc/net/route")); err != nil {
		t.Fatal(err)
	}
	if _, err := host.Address(context.Background(), ""); err != protocol.Unavailable {
		t.Fatalf("unreadable route table gave %v", err)
	}
	if got, err := host.Address(context.Background(), "eth0"); err != nil || got != "10.10.10.1" {
		t.Fatalf("named device %q error %v", got, err)
	}
}
func TestBackendsCountAnyRoutedInterface(t *testing.T) {
	host := stageHost(t, ethDefault+ethSubnet,
		stagedLink{"eth0", net.FlagUp | net.FlagRunning, true, []string{"192.168.1.30/24"}},
		stagedLink{"wlan0", net.FlagUp, true, nil})
	d, err := NewDietPi("wlan0", t.TempDir(), t.TempDir(), t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	n := NewNetworkManager("wlan0", t.TempDir())
	d.AddressFor, n.AddressFor = host.Address, host.Address
	for _, b := range []Backend{d, n} {
		if got, err := b.Address(context.Background()); err != nil || got != "192.168.1.30" {
			t.Fatalf("%T address %q error %v", b, got, err)
		}
	}
}
