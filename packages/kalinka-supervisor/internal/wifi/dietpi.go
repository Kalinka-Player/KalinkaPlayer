package wifi

import (
	"context"
	"crypto/pbkdf2"
	"crypto/sha1"
	"encoding/hex"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"kalinka/supervisor/internal/protocol"
)

// DietPi preserves the native ifupdown and wpa_supplicant configuration paths.
type DietPi struct {
	Interface, Root, Runtime, Conf, Database, CountryFile, Tool string
	Transaction                                                 Transaction
	Run                                                         Runner
	OpenControl                                                 func(string, string) (Control, error)
	AddressFor                                                  func(context.Context, string) (string, error)
	Poll, JoinTimeout                                           time.Duration
}

func NewDietPi(iface, root, state, runtime string) (*DietPi, error) {
	if iface == "" {
		matches, _ := filepath.Glob("/sys/class/net/*/wireless")
		sort.Strings(matches)
		if len(matches) > 0 {
			iface = filepath.Base(filepath.Dir(matches[0]))
		} else {
			iface = "wlan0"
		}
	}
	if !interfacePattern.MatchString(iface) {
		return nil, protocol.Unavailable
	}
	d := &DietPi{Interface: iface, Root: root, Runtime: runtime, Conf: filepath.Join(root, "etc/wpa_supplicant/wpa_supplicant.conf"), Database: filepath.Join(root, "var/lib/dietpi/dietpi-wifi.db"), CountryFile: filepath.Join(root, "boot/dietpi.txt"), Tool: filepath.Join(root, "boot/dietpi/dietpi-network"), Run: Run, OpenControl: OpenControl, AddressFor: LANAddress, Poll: time.Second, JoinTimeout: 60 * time.Second}
	d.Transaction = Transaction{filepath.Join(state, "provision-rollback"), []string{d.Conf, d.Database, d.CountryFile, filepath.Join(root, "etc/network/interfaces"), filepath.Join(root, "etc/network/interfaces.d/"+iface+".conf")}}
	return d, nil
}
func (d *DietPi) Address(ctx context.Context) (string, error) { return d.AddressFor(ctx, "") }
func (d *DietPi) Close() error                                { return nil }
func (d *DietPi) Scan(ctx context.Context, country string) ([]protocol.Network, error) {
	if !protocol.CountryPattern.MatchString(country) {
		return nil, protocol.Invalid
	}
	for _, args := range [][]string{{"iw", "reg", "set", country}, {"rfkill", "unblock", "wlan"}, {"ip", "link", "set", "dev", d.Interface, "up"}} {
		if _, err := d.Run(ctx, 15*time.Second, args[0], args[1:]...); err != nil {
			return nil, err
		}
	}
	out, err := d.Run(ctx, 15*time.Second, "iw", "dev", d.Interface, "scan")
	if err != nil {
		return nil, err
	}
	return ParseScan(out), nil
}
func (d *DietPi) Recover(ctx context.Context) error {
	legacy := Transaction{filepath.Join(d.Root, "var/lib/kalinka-image/provision-rollback"), d.Transaction.Paths}
	for _, t := range []Transaction{legacy, d.Transaction} {
		if !t.Pending() {
			continue
		}
		if err := d.takeDown(ctx); err != nil {
			return err
		}
		if err := t.Restore(); err != nil {
			return err
		}
		if _, err := d.Run(ctx, 60*time.Second, "systemctl", "restart", "kalinka-wifi@"+d.Interface+".service"); err != nil {
			return err
		}
	}
	return nil
}
func (d *DietPi) takeDown(ctx context.Context) error {
	// Stop also cancels an outstanding restart job before restoring its files.
	if _, err := d.Run(ctx, 20*time.Second, "systemctl", "stop", "ifup@"+d.Interface+".service", "kalinka-wifi@"+d.Interface+".service"); err != nil {
		return err
	}
	_, _ = d.Run(ctx, 15*time.Second, "ifdown", "--force", d.Interface)
	if ctx.Err() != nil {
		return ctx.Err()
	}
	// An interface which was never configured can legitimately fail ifdown.
	return nil
}
func (d *DietPi) rollback() error {
	ctx, cancel := context.WithTimeout(context.Background(), RollbackTimeout)
	defer cancel()
	if err := d.takeDown(ctx); err != nil {
		return err
	}
	if err := d.Transaction.Restore(); err != nil {
		return err
	}
	_, err := d.Run(ctx, 15*time.Second, "systemctl", "--no-block", "restart", "kalinka-wifi@"+d.Interface+".service")
	return err
}

var numeric = regexp.MustCompile(`^[0-9]+$`)

func (d *DietPi) Join(parent context.Context, c protocol.Command, progress func(byte)) (address string, result error) {
	ctx, cancel := context.WithTimeout(parent, 100*time.Second)
	defer cancel()
	if _, err := os.Stat(d.Tool); err != nil {
		return "", protocol.Unavailable
	}
	keyBytes, err := pbkdf2.Key(sha1.New, c.Password.Reveal(), []byte(c.SSID), 4096, 32)
	if err != nil {
		return "", protocol.Invalid
	}
	defer clear(keyBytes)
	key := hex.EncodeToString(keyBytes)
	database, err := readOptional(d.Database)
	if err != nil {
		return "", err
	}
	updated, err := updateDatabase(string(database), c.SSID, key)
	clear(database)
	if err != nil {
		return "", err
	}
	if err = d.Transaction.Begin(); err != nil {
		return "", err
	}
	var control, monitor Control
	var upDone chan error
	var upCancel context.CancelFunc
	defer func() {
		if upCancel != nil {
			upCancel()
		}
		if upDone != nil {
			<-upDone
		}
		if control != nil {
			_ = control.Close()
		}
		if monitor != nil {
			_ = monitor.Close()
		}
		if result != nil && d.rollback() != nil {
			result = protocol.Storage
		}
	}()
	run := func(name string, args ...string) error { _, e := d.Run(ctx, 15*time.Second, name, args...); return e }
	if err = run("iw", "reg", "set", c.Country); err != nil {
		return "", err
	}
	if err = run("rfkill", "unblock", "wlan"); err != nil {
		return "", err
	}
	contents, err := readOptional(d.Conf)
	if err != nil {
		return "", err
	}
	if len(contents) == 0 {
		contents = []byte("ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev\nupdate_config=1\n")
		if err = AtomicWrite(d.Conf, contents, 0600); err != nil {
			return "", err
		}
	}
	staged, states, err := stageNetworks(string(contents))
	clear(contents)
	if err != nil {
		return "", err
	}
	staged = replaceSetting(staged, "country", c.Country)
	if err = run(d.Tool, "apply", d.Interface, "--enable", "--dhcp", "--client", "--no-restart"); err != nil {
		return "", err
	}
	if err = d.takeDown(ctx); err != nil {
		return "", err
	}
	if err = run("ip", "-4", "address", "flush", "dev", d.Interface); err != nil {
		return "", err
	}
	if err = AtomicWrite(d.Conf, []byte(staged), 0600); err != nil {
		return "", err
	}
	upCtx, stop := context.WithCancel(ctx)
	upCancel = stop
	upDone = make(chan error, 1)
	go func() {
		_, e := d.Run(upCtx, 80*time.Second, "systemctl", "restart", "kalinka-wifi@"+d.Interface+".service")
		upDone <- e
	}()
	for i := 0; i < 30; i++ {
		if err = Pause(ctx, 250*time.Millisecond); err != nil {
			return "", err
		}
		control, err = d.OpenControl(d.Interface, d.Runtime)
		if err == nil {
			pong, e := control.Request(ctx, "PING")
			if e == nil && pong == "PONG" {
				break
			}
			_ = control.Close()
		}
		control = nil
	}
	if control == nil {
		return "", protocol.Unavailable
	}
	monitor, err = d.OpenControl(d.Interface, d.Runtime)
	if err != nil {
		return "", err
	}
	if _, err = monitor.Request(ctx, "ATTACH"); err != nil {
		return "", err
	}
	type savedNetwork struct{ id, ssid, disabled string }
	var saved []savedNetwork
	list, err := control.Request(ctx, "LIST_NETWORKS")
	if err != nil {
		return "", err
	}
	lines := strings.Split(strings.TrimSpace(list), "\n")
	for _, line := range lines[1:] {
		id := strings.Split(line, "\t")[0]
		if !numeric.MatchString(id) {
			continue
		}
		if len(saved) >= len(states) {
			return "", protocol.Storage
		}
		ssid, err := control.Request(ctx, "GET_NETWORK "+id+" ssid")
		if err != nil {
			return "", err
		}
		saved = append(saved, savedNetwork{id, ssid, states[len(saved)]})
	}
	if len(saved) != len(states) {
		return "", protocol.Storage
	}
	index, err := control.Request(ctx, "ADD_NETWORK")
	if err != nil || !numeric.MatchString(index) {
		return "", protocol.Unavailable
	}
	for _, pair := range [][2]string{{"ssid", hex.EncodeToString([]byte(c.SSID))}, {"psk", key}, {"key_mgmt", "WPA-PSK"}, {"scan_ssid", "1"}} {
		if _, err = control.Request(ctx, "SET_NETWORK "+index+" "+pair[0]+" "+pair[1]); err != nil {
			return "", err
		}
	}
	if _, err = control.Request(ctx, "SELECT_NETWORK "+index); err != nil {
		return "", err
	}
	progress(protocol.Authenticating)
	associated := false
	deadline := time.Now().Add(d.JoinTimeout)
	for time.Now().Before(deadline) {
		events, err := monitor.Events(ctx)
		if err != nil {
			return "", err
		}
		for _, event := range events {
			if strings.Contains(event, "CTRL-EVENT-SSID-TEMP-DISABLED") && strings.Contains(event, "WRONG_KEY") {
				return "", protocol.WrongPassword
			}
		}
		raw, err := control.Request(ctx, "STATUS")
		if err != nil {
			return "", err
		}
		status := map[string]string{}
		for _, line := range strings.Split(raw, "\n") {
			if k, v, ok := strings.Cut(line, "="); ok {
				status[k] = v
			}
		}
		associated = status["wpa_state"] == "COMPLETED" && status["id"] == index
		if associated {
			progress(protocol.GettingAddress)
			address, err = d.AddressFor(ctx, d.Interface)
			if err != nil {
				return "", err
			}
			if address != "" {
				break
			}
		}
		if err = Pause(ctx, d.Poll); err != nil {
			return "", err
		}
	}
	if address == "" {
		if associated {
			return "", protocol.NoAddress
		}
		return "", protocol.Timeout
	}
	err = <-upDone
	upDone = nil
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	if err != nil {
		return "", protocol.Unavailable
	}
	progress(protocol.Saving)
	joined, err := control.Request(ctx, "GET_NETWORK "+index+" ssid")
	if err != nil {
		return "", err
	}
	for _, old := range saved {
		var command string
		if old.ssid == joined {
			command = "REMOVE_NETWORK " + old.id
		} else if old.disabled == "0" {
			command = "ENABLE_NETWORK " + old.id
		} else if old.disabled != "1" {
			command = "SET_NETWORK " + old.id + " disabled " + old.disabled
		}
		if command != "" {
			if _, err = control.Request(ctx, command); err != nil {
				return "", err
			}
		}
	}
	if _, err = control.Request(ctx, "SET country "+c.Country); err != nil {
		return "", err
	}
	if _, err = control.Request(ctx, "SAVE_CONFIG"); err != nil {
		return "", protocol.Storage
	}
	contents, err = os.ReadFile(d.Conf)
	if err != nil {
		return "", protocol.Storage
	}
	if err = AtomicWrite(d.Conf, contents, 0600); err != nil {
		return "", err
	}
	if err = AtomicWrite(d.Database, []byte(updated), 0600); err != nil {
		return "", err
	}
	country, err := os.ReadFile(d.CountryFile)
	if err != nil {
		return "", protocol.Storage
	}
	if err = AtomicWrite(d.CountryFile, []byte(replaceSetting(string(country), "AUTO_SETUP_NET_WIFI_COUNTRY_CODE", c.Country)), 0600); err != nil {
		return "", err
	}
	if err = d.Transaction.Commit(); err != nil {
		return "", err
	}
	return address, nil
}
