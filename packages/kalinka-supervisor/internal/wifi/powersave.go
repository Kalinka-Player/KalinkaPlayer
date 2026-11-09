package wifi

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"kalinka/supervisor/internal/dbusx"
)

// PowerSavingConf holds a box's own Wi-Fi power-saving choice, under Root. The
// package ships the default, off, under /usr/lib/NetworkManager/conf.d; a
// file of the same name here shadows it.
const PowerSavingConf = "etc/NetworkManager/conf.d/kalinka-wifi-powersave.conf"

// ErrNotApplied reports a choice saved for the next activation whose running
// connection could not be switched over.
var ErrNotApplied = errors.New("Wi-Fi power saving saved but not applied")

var powerSavingState = map[bool]string{false: "off", true: "on"}

// PowerSaving is a NetworkManager box's Wi-Fi power-saving switch: off by the
// package default, otherwise whatever the box's own choice in PowerSavingConf
// says. Safe for concurrent use.
type PowerSaving struct {
	// Root prefixes /etc and /sys, so tests can stage a box.
	Root string
	Run  Runner
	// Dial opens the connection Set has NetworkManager reread its configuration over.
	Dial func() (dbusx.Bus, error)

	simulated bool
	mu        sync.Mutex
	pretended bool
}

// NewPowerSaving switches the box under root ("/" in production). Simulated,
// it changes nothing and only remembers the last choice.
func NewPowerSaving(root string, simulated bool) *PowerSaving {
	return &PowerSaving{Root: root, Run: Run, Dial: func() (dbusx.Bus, error) { return dbusx.System(nm) }, simulated: simulated}
}

// Offered reports whether the box has a wireless interface the choice applies to.
func (p *PowerSaving) Offered() bool { return p.simulated || len(p.interfaces()) > 0 }

// Enabled reports the box's own choice, or the package default, off, without one.
func (p *PowerSaving) Enabled() bool {
	if p.simulated {
		p.mu.Lock()
		defer p.mu.Unlock()
		return p.pretended
	}
	b, err := os.ReadFile(filepath.Join(p.Root, PowerSavingConf))
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(b), "\n") {
		if key, value, ok := strings.Cut(line, "="); ok && strings.TrimSpace(key) == "wifi.powersave" {
			return strings.TrimSpace(value) == "3"
		}
	}
	return false
}

// Set saves on as the box's choice, has NetworkManager reread its
// configuration so the next activation follows it, and switches every
// wireless interface over now. A choice saved but not applied returns ErrNotApplied.
func (p *PowerSaving) Set(ctx context.Context, on bool) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.simulated {
		p.pretended = on
		slog.Info("Simulated Wi-Fi power saving change", "state", powerSavingState[on])
		return nil
	}
	value := "2"
	if on {
		value = "3"
	}
	conf := "# This streamer's own Wi-Fi power-saving choice, made on the supervisor's dashboard.\n[connection]\nwifi.powersave=" + value + "\n"
	if err := AtomicWrite(filepath.Join(p.Root, PowerSavingConf), []byte(conf), 0644); err != nil {
		return err
	}
	if err := p.reload(ctx); err != nil {
		slog.Warn("NetworkManager did not reread its configuration", "error", err)
		return ErrNotApplied
	}
	for _, iface := range p.interfaces() {
		if _, err := p.Run(ctx, 5*time.Second, "iw", "dev", iface, "set", "power_save", powerSavingState[on]); err != nil {
			slog.Warn("The running connection kept its Wi-Fi power saving", "interface", iface, "error", err)
			return ErrNotApplied
		}
	}
	slog.Info("Wi-Fi power saving switched", "state", powerSavingState[on])
	return nil
}

func (p *PowerSaving) reload(ctx context.Context) error {
	bus, err := p.Dial()
	if err != nil {
		return err
	}
	defer bus.Close()
	// Flag 1 rereads the configuration alone, as SIGHUP does.
	_, err = bus.Call(ctx, nmRoot, nm+".Reload", uint32(1))
	return err
}

// interfaces names the kernel's wireless interfaces, in name order.
func (p *PowerSaving) interfaces() []string {
	matches, _ := filepath.Glob(filepath.Join(p.Root, "sys/class/net/*/phy80211"))
	var names []string
	for _, m := range matches {
		if name := filepath.Base(filepath.Dir(m)); interfacePattern.MatchString(name) {
			names = append(names, name)
		}
	}
	return names
}
