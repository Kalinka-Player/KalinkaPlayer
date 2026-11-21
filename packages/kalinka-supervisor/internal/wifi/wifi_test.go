package wifi

import (
	"context"
	"errors"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/rfkill/rfkilltest"
	"os"
	"path/filepath"
	"slices"
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
			files := map[string]string{d.Tool: "tool", d.Conf: "update_config=1\ncountry=DE\nnetwork={\n ssid=\"saved\"\n}\n", d.Database: "aWIFI_SSID[0]='saved'\n", d.CountryFile: "AUTO_SETUP_NET_WIFI_COUNTRY_CODE=US\n", d.Transaction.Paths[3]: "old interfaces\n"}
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
			var reported []string
			d.OnCountrySet(func(country string) { reported = append(reported, country) })
			cmd := protocol.Command{SSID: "candidate", Country: "GB", Password: protocol.NewSecret("dummy-passphrase")}
			defer cmd.Password.Clear()
			ip, err := d.Join(ctx, cmd, func(byte) {})
			want := []string{"GB", "DE"}
			if scenario == "success" {
				want = want[:1]
			}
			if !slices.Equal(reported, want) {
				t.Fatalf("reported %q, want %q", reported, want)
			}
			mu.Lock()
			all := strings.Join(calls, "\n")
			mu.Unlock()
			if strings.Contains(all, "rfkill") {
				t.Fatal("joining implicitly changes radio blocks")
			}
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
func TestDietPiRollbackReportsOnlyARestoredCountry(t *testing.T) {
	for _, tc := range []struct {
		name, saved string
		restart     error
		want        []string
	}{
		{"saved with one", "update_config=1\n\tcountry=DE \n", nil, []string{"DE"}},
		{"saved without one", "update_config=1\n", nil, nil},
		{"restart failing", "update_config=1\ncountry=DE\n", protocol.Unavailable, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			d, err := NewDietPi("wlan0", root, filepath.Join(root, "state"), filepath.Join(root, "run"))
			if err != nil {
				t.Fatal(err)
			}
			if err = AtomicWrite(d.Conf, []byte(tc.saved), 0600); err != nil {
				t.Fatal(err)
			}
			if err = d.Transaction.Begin(); err != nil {
				t.Fatal(err)
			}
			if err = AtomicWrite(d.Conf, []byte("update_config=1\ncountry=GB\n"), 0600); err != nil {
				t.Fatal(err)
			}
			d.Run = func(_ context.Context, _ time.Duration, name string, args ...string) (string, error) {
				if name == "systemctl" && args[0] == "--no-block" {
					return "", tc.restart
				}
				return "", nil
			}
			var reported []string
			d.OnCountrySet(func(country string) { reported = append(reported, country) })
			if err := d.rollback(); err != tc.restart {
				t.Fatalf("rollback gave %v, want %v", err, tc.restart)
			}
			if !slices.Equal(reported, tc.want) {
				t.Fatalf("reported %q, want %q", reported, tc.want)
			}
		})
	}
}
func TestDietPiEnableRadio(t *testing.T) {
	restart := [][]string{{"systemctl", "--no-block", "restart", "kalinka-wifi@wlan0.service"}}
	for _, tc := range []struct {
		name     string
		blocked  bool
		address  string
		commands [][]string
	}{
		{"blocked, no address", true, "", restart},
		{"blocked, stale address", true, "192.0.2.5", restart},
		{"not blocked", false, "", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := rfkilltest.Stage(t, rfkilltest.Radio{Type: "wlan", Name: "phy0", Soft: tc.blocked}, rfkilltest.Radio{Type: "bluetooth", Name: "hci0", Soft: true})
			rfkilltest.LinkWiFi(t, root, "wlan0", "phy0")
			d, err := NewDietPi("wlan0", root, filepath.Join(root, "state"), filepath.Join(root, "run"))
			if err != nil {
				t.Fatal(err)
			}
			var commands [][]string
			d.Run = recordCommands(&commands, nil)
			d.AddressFor = func(_ context.Context, device string) (string, error) {
				if device != "wlan0" {
					t.Fatalf("address read on %q", device)
				}
				return tc.address, nil
			}
			if err := d.EnableRadio(context.Background()); err != nil {
				t.Fatal(err)
			}
			if rfkilltest.Soft(t, root, "rfkill0") || !rfkilltest.Soft(t, root, "rfkill1") {
				t.Fatal("cleared the wrong radios")
			}
			if !slices.EqualFunc(commands, tc.commands, slices.Equal) {
				t.Fatalf("commands %v", commands)
			}
		})
	}
}

func TestDietPiRetriesFailedRestartAfterClearingOnlyItsRadio(t *testing.T) {
	root := rfkilltest.Stage(t, rfkilltest.Radio{Type: "wlan", Name: "phy0", Soft: true}, rfkilltest.Radio{Type: "wlan", Name: "phy1", Soft: true})
	rfkilltest.LinkWiFi(t, root, "wlan0", "phy0")
	d, err := NewDietPi("wlan0", root, t.TempDir(), t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	calls := 0
	d.Run = func(context.Context, time.Duration, string, ...string) (string, error) {
		calls++
		if calls == 1 {
			return "", protocol.Unavailable
		}
		return "", nil
	}
	if err := d.EnableRadio(context.Background()); err == nil {
		t.Fatal("restart failure hidden")
	}
	if err := d.EnableRadio(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := d.EnableRadio(context.Background()); err != nil {
		t.Fatal(err)
	}
	if calls != 2 || rfkilltest.Soft(t, root, "rfkill0") || !rfkilltest.Soft(t, root, "rfkill1") {
		t.Fatal("lost retry or touched another radio")
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

func TestDietPiScanReportsEachCountryTheRadioAccepts(t *testing.T) {
	d, err := NewDietPi("wlan0", t.TempDir(), t.TempDir(), t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	var commands [][]string
	refuse := ""
	d.Run = func(_ context.Context, _ time.Duration, name string, args ...string) (string, error) {
		commands = append(commands, append([]string{name}, args...))
		if name == "iw" && args[0] == "reg" && args[2] == refuse {
			return "", protocol.Unavailable
		}
		return "", nil
	}
	var reported []string
	d.OnCountrySet(func(country string) { reported = append(reported, country) })
	for _, country := range []string{"", "GB"} {
		if _, err := d.Scan(context.Background(), country); err != nil {
			t.Fatal(err)
		}
	}
	if want := [][]string{{"iw", "reg", "set", "GB"}, {"ip", "link", "set", "dev", "wlan0", "up"}, {"iw", "dev", "wlan0", "scan"}}; !slices.EqualFunc(commands[2:], want, slices.Equal) {
		t.Fatalf("scan under GB ran %v, want %v", commands[2:], want)
	}
	refuse = "FR"
	if _, err := d.Scan(context.Background(), "FR"); err == nil {
		t.Fatal("a scan under a refused country succeeded")
	}
	if !slices.Equal(reported, []string{"GB"}) {
		t.Fatalf("reported %q, want only GB", reported)
	}
}

func TestDryRunReportsEachCountryItIsGiven(t *testing.T) {
	d := &DryRun{IP: "192.0.2.10"}
	var reported []string
	d.OnCountrySet(func(country string) { reported = append(reported, country) })
	for _, country := range []string{"", "GB"} {
		if _, err := d.Scan(context.Background(), country); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := d.Join(context.Background(), protocol.Command{SSID: "home", Country: "DE"}, func(byte) {}); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(reported, []string{"GB", "DE"}) {
		t.Fatalf("reported %q, want GB then DE", reported)
	}
}

func TestDietPiScanPreservesRadioBlock(t *testing.T) {
	root := rfkilltest.Stage(t, rfkilltest.Radio{Type: "wlan", Name: "phy0", Soft: true})
	rfkilltest.LinkWiFi(t, root, "wlan0", "phy0")
	d, err := NewDietPi("wlan0", root, t.TempDir(), t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	d.Run = func(_ context.Context, _ time.Duration, name string, _ ...string) (string, error) {
		if name == "rfkill" {
			t.Fatal("scan implicitly clears a radio block")
		}
		return "", protocol.Unavailable
	}
	if _, err := d.Scan(context.Background(), ""); err == nil {
		t.Fatal("blocked scan succeeded")
	}
	if !rfkilltest.Soft(t, root, "rfkill0") {
		t.Fatal("scan enabled the radio")
	}
}
