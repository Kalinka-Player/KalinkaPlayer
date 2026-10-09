// Package wifi implements transactional networking without credential-bearing argv.
package wifi

import (
	"bytes"
	"context"
	"crypto/rand"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"time"

	"kalinka/supervisor/internal/protocol"
)

// RollbackTimeout leaves cancellation and polling headroom within the app's 75-second reset wait.
const RollbackTimeout = 55 * time.Second

// Backend owns recovery before setup and rollback before returning a join error.
type Backend interface {
	Address(context.Context) (string, error)
	Scan(context.Context, string) ([]protocol.Network, error)
	Join(context.Context, protocol.Command, func(byte)) (string, error)
	Recover(context.Context) error
	Close() error
}

var interfacePattern = regexp.MustCompile(`^[a-zA-Z0-9_.-]{1,15}$`)

func UUID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic("random source unavailable")
	}
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}
func Pause(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// Host is the box an address is looked up on. Root prefixes /proc and /sys,
// and Interfaces and Addrs stand in for the kernel's, so tests can stage a box.
type Host struct {
	Root       string
	Interfaces func() ([]net.Interface, error)
	Addrs      func(*net.Interface) ([]net.Addr, error)
}

// LANAddress is Host.Address on the running box.
func LANAddress(ctx context.Context, device string) (string, error) {
	return Host{Root: "/", Interfaces: net.Interfaces, Addrs: (*net.Interface).Addrs}.Address(ctx, device)
}

// Address returns the first global IPv4 address on a physical interface that
// is up with a link, or "": on device when one is named, otherwise on an
// interface carrying an IPv4 default route. One without it, like a cable to an
// amplifier, is taken to reach no phone, which is wrong only on a LAN with no
// gateway at all.
func (h Host) Address(_ context.Context, device string) (string, error) {
	interfaces, err := h.Interfaces()
	if err != nil {
		return "", protocol.Unavailable
	}
	candidates := map[string]bool{device: true}
	if device == "" {
		if candidates, err = h.defaultRouted(); err != nil {
			return "", err
		}
	}
	for _, iface := range interfaces {
		if !candidates[iface.Name] {
			continue
		}
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagRunning == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		if _, err := os.Stat(filepath.Join(h.Root, "sys/class/net", iface.Name, "device")); err != nil {
			continue
		}
		addresses, err := h.Addrs(&iface)
		if err != nil {
			continue
		}
		for _, address := range addresses {
			ip, _, err := net.ParseCIDR(address.String())
			if err == nil && ip.To4() != nil && ip.IsGlobalUnicast() {
				return ip.String(), nil
			}
		}
	}
	return "", nil
}

// defaultRouted names the interfaces that /proc/net/route gives an IPv4 default route.
// Its Flags column is no filter: the kernel sets RTF_UP on every route it lists there.
func (h Host) defaultRouted() (map[string]bool, error) {
	table, err := os.ReadFile(filepath.Join(h.Root, "proc/net/route"))
	if err != nil {
		return nil, protocol.Unavailable
	}
	routed := map[string]bool{}
	for _, line := range strings.Split(string(table), "\n") {
		if f := strings.Fields(line); len(f) >= 8 && f[1] == "00000000" && f[7] == "00000000" {
			routed[f[0]] = true
		}
	}
	return routed, nil
}
func strongest(networks []protocol.Network) []protocol.Network {
	found := map[string]protocol.Network{}
	for _, n := range networks {
		if !protocol.ValidSSID(n.SSID) {
			continue
		}
		key := n.SSID + "\x00" + n.Security
		if old, ok := found[key]; !ok || n.Signal > old.Signal {
			found[key] = n
		}
	}
	result := make([]protocol.Network, 0, len(found))
	for _, n := range found {
		result = append(result, n)
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Signal != result[j].Signal {
			return result[i].Signal > result[j].Signal
		}
		return result[i].SSID < result[j].SSID
	})
	return result[:min(len(result), 30)]
}

// Runner is injectable for recorder tests. Inputs must never contain credentials.
type Runner func(context.Context, time.Duration, string, ...string) (string, error)
type limitedBuffer struct{ bytes.Buffer }

func (b *limitedBuffer) Write(p []byte) (int, error) {
	if b.Len()+len(p) > 1024*1024 {
		return 0, protocol.Unavailable
	}
	return b.Buffer.Write(p)
}
func Run(parent context.Context, timeout time.Duration, name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
	cmd.WaitDelay = time.Second
	var out limitedBuffer
	cmd.Stdout = &out
	if cmd.Run() != nil {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return "", protocol.Unavailable
	}
	return out.String(), nil
}

// DryRun exercises the same BLE service without changing host networking.
type DryRun struct {
	Result, IP string
	Delay      time.Duration
}

func (d *DryRun) Address(ctx context.Context) (string, error) {
	if d.IP != "" {
		return d.IP, nil
	}
	return LANAddress(ctx, "")
}
func (d *DryRun) Recover(context.Context) error { return nil }
func (d *DryRun) Close() error                  { return nil }
func (d *DryRun) Scan(ctx context.Context, _ string) ([]protocol.Network, error) {
	if err := Pause(ctx, d.Delay); err != nil {
		return nil, err
	}
	return []protocol.Network{{SSID: "Test network", Signal: -42, Security: "wpa2"}, {SSID: "Test mixed WPA2-WPA3", Signal: -61, Security: "wpa2"}, {SSID: "Test WPA3 only", Signal: -68, Security: "unsupported"}, {SSID: "Test open network", Signal: -74, Security: "open"}}, nil
}
func (d *DryRun) Join(ctx context.Context, c protocol.Command, progress func(byte)) (string, error) {
	slog.Info("Simulated Wi-Fi join", "ssid", c.SSID, "country", c.Country)
	for _, s := range []byte{protocol.Authenticating, protocol.GettingAddress, protocol.Saving} {
		progress(s)
		if err := Pause(ctx, d.Delay); err != nil {
			return "", err
		}
	}
	if d.Result != "" && d.Result != "joined" {
		return "", protocol.Error(d.Result)
	}
	address, err := d.Address(ctx)
	if err != nil {
		return "", err
	}
	if address == "" {
		address = "192.0.2.1"
	}
	return address, nil
}
