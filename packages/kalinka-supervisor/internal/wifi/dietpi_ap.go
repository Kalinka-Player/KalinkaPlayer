package wifi

import (
	"context"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"kalinka/supervisor/internal/protocol"
)

// APProcess is a foreground daemon owned by the supervisor's systemd cgroup.
type APProcess interface {
	Stop()
	Alive() bool
}
type apProcess struct {
	cmd  *exec.Cmd
	done chan struct{}
}

func (p *apProcess) Alive() bool {
	select {
	case <-p.done:
		return false
	default:
		return true
	}
}

func (d *DietPi) APHealthy(context.Context) bool {
	if len(d.apProcesses) != 2 {
		return false
	}
	for _, p := range d.apProcesses {
		if !p.Alive() {
			return false
		}
	}
	return true
}

func (p *apProcess) Stop() {
	_ = p.cmd.Process.Signal(syscall.SIGTERM)
	select {
	case <-p.done:
	case <-time.After(2 * time.Second):
		_ = p.cmd.Process.Kill()
		<-p.done
	}
}

func startAPProcess(ctx context.Context, name string, args ...string) (APProcess, error) {
	cmd := exec.Command(name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Pdeathsig: syscall.SIGTERM}
	if cmd.Start() != nil {
		return nil, protocol.Unavailable
	}
	p := &apProcess{cmd, make(chan struct{})}
	go func() { _ = cmd.Wait(); close(p.done) }()
	select {
	case <-p.done:
		return nil, protocol.Unavailable
	case <-ctx.Done():
		p.Stop()
		return nil, ctx.Err()
	case <-time.After(time.Second):
		return p, nil
	}
}

func (d *DietPi) APInterface() string { return d.Interface }
func (d *DietPi) apRecord() string {
	return filepath.Join(filepath.Dir(d.Transaction.Dir), "hotspot-interface")
}

func (d *DietPi) StartAP(ctx context.Context, name string) (result error) {
	if !protocol.ValidSSID(name) {
		return protocol.Invalid
	}
	if err := d.StopAP(ctx); err != nil {
		return err
	}
	if err := AtomicWrite(d.apRecord(), []byte(d.Interface), 0600); err != nil {
		return err
	}
	defer func() {
		if result != nil {
			cleanup, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			_ = d.StopAP(cleanup)
		}
	}()
	if err := d.takeDown(ctx); err != nil {
		return err
	}
	for _, args := range [][]string{
		{"rfkill", "unblock", "wlan"},
		{"ip", "link", "set", "dev", d.Interface, "up"},
		{"ip", "address", "replace", APAddress + "/24", "dev", d.Interface},
	} {
		if _, err := d.Run(ctx, 10*time.Second, args[0], args[1:]...); err != nil {
			return err
		}
	}
	hostapd := fmt.Sprintf("interface=%s\ndriver=nl80211\nssid2=%s\nhw_mode=g\nchannel=1\nwmm_enabled=1\nauth_algs=1\nap_isolate=1\nwpa=2\nwpa_key_mgmt=WPA-PSK\nrsn_pairwise=CCMP\nwpa_passphrase=%s\n", d.Interface, hex.EncodeToString([]byte(name)), APPassword)
	for _, conf := range []struct{ name, body, binary string }{
		{"hotspot-hostapd.conf", hostapd, "hostapd"},
	} {
		path := filepath.Join(d.Runtime, conf.name)
		if err := AtomicWrite(path, []byte(conf.body), 0600); err != nil {
			return err
		}
		args := []string{path}
		start := d.StartAPProcess
		if start == nil {
			start = startAPProcess
		}
		process, err := start(ctx, conf.binary, args...)
		if err != nil {
			return err
		}
		d.apProcesses = append(d.apProcesses, process)
	}
	process, err := startAPDNS(ctx, d.Interface, d.Runtime, d.StartAPProcess)
	if err != nil {
		return err
	}
	d.apProcesses = append(d.apProcesses, process)
	return nil
}

func (d *DietPi) StopAP(ctx context.Context) error {
	for _, process := range d.apProcesses {
		process.Stop()
	}
	d.apProcesses = nil
	raw, err := os.ReadFile(d.apRecord())
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil || !interfacePattern.Match(raw) {
		return protocol.Storage
	}
	iface := string(raw)
	// The durable record also repairs a crash after assigning the static AP address.
	for _, args := range [][]string{
		{"ip", "link", "set", "dev", iface, "down"},
		{"iw", "dev", iface, "set", "type", "managed"},
		{"ip", "-4", "address", "flush", "dev", iface, "to", APAddress + "/32"},
		{"systemctl", "--no-block", "restart", "kalinka-wifi@" + iface + ".service"},
	} {
		if _, err = d.Run(ctx, 10*time.Second, args[0], args[1:]...); err != nil {
			return err
		}
	}
	return removeDurable(d.apRecord())
}
