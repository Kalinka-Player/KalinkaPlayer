// Kalinka Supervisor runs independently of Core and its Python installation.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"kalinka/supervisor/internal/gatt"
	"kalinka/supervisor/internal/machine"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/system"
	"kalinka/supervisor/internal/wifi"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

var version = "development"

type options struct {
	backend, iface, adapter, identity, config, state, runtime, result, address string
	test, always, version, checkEnabled                                        bool
	port                                                                       int
}

func parse(args []string) (options, error) {
	var o options
	f := flag.NewFlagSet("kalinka-supervisor provision", flag.ContinueOnError)
	f.StringVar(&o.backend, "backend", "auto", "Networking backend: auto, dietpi, nm")
	f.StringVar(&o.iface, "interface", "", "Wi-Fi interface (auto-detected by default)")
	f.StringVar(&o.adapter, "adapter", "hci0", "BlueZ adapter")
	f.StringVar(&o.identity, "server-id-file", "/var/lib/kalinka/server_id", "Core identity file")
	f.StringVar(&o.config, "server-config", "/etc/kalinka/kalinka_conf.cfg", "Core configuration file")
	f.StringVar(&o.state, "state-dir", "", "Private persistent supervisor state directory")
	f.StringVar(&o.runtime, "runtime-dir", "", "Private runtime directory")
	f.BoolVar(&o.test, "test", false, "Real BLE with simulated Wi-Fi")
	f.BoolVar(&o.always, "always-advertise", false, "Keep setup available while online (local testing only)")
	f.StringVar(&o.result, "test-result", "joined", "Simulated result: joined, wrong_password, no_address, timeout")
	f.StringVar(&o.address, "test-address", "", "IPv4 address returned in test mode")
	f.IntVar(&o.port, "port", 0, "Core HTTP port (default: read Core config or 8000)")
	f.BoolVar(&o.version, "version", false, "Print version")
	f.BoolVar(&o.checkEnabled, "check-enabled", false, "Exit 1 when nearby setup is disabled (systemd condition)")
	if len(args) > 0 && args[0] == "provision" {
		args = args[1:]
	}
	if err := f.Parse(args); err != nil {
		return o, err
	}
	if f.NArg() != 0 {
		return o, protocol.Invalid
	}
	if o.version || o.checkEnabled {
		return o, nil
	}
	if o.backend != "auto" && o.backend != "dietpi" && o.backend != "nm" {
		return o, protocol.Invalid
	}
	if !o.test && (o.address != "" || o.result != "joined") {
		return o, protocol.Invalid
	}
	switch o.result {
	case "joined", "wrong_password", "no_address", "timeout":
	default:
		return o, protocol.Invalid
	}
	if o.address != "" && net.ParseIP(o.address).To4() == nil {
		return o, protocol.Invalid
	}
	if o.port == 0 {
		o.port = 8000
		if b, err := os.ReadFile(o.config); err == nil {
			var config map[string]json.RawMessage
			if json.Unmarshal(b, &config) == nil {
				if p, ok := config["base_config.server.port"]; ok {
					if json.Unmarshal(p, &o.port) != nil {
						return o, protocol.Invalid
					}
				}
			}
		}
	}
	if o.port < 1 || o.port > 65535 {
		return o, protocol.Invalid
	}
	if o.state == "" {
		if os.Geteuid() == 0 {
			o.state = "/var/lib/kalinka-supervisor"
		} else {
			base, err := os.UserConfigDir()
			if err != nil {
				return o, protocol.Storage
			}
			o.state = filepath.Join(base, "kalinka-supervisor")
		}
	}
	if o.runtime == "" {
		if os.Geteuid() == 0 {
			o.runtime = "/run/kalinka-supervisor"
		} else {
			base := os.Getenv("XDG_RUNTIME_DIR")
			if base == "" {
				base = os.TempDir()
			}
			o.runtime = filepath.Join(base, "kalinka-supervisor-"+strconv.Itoa(os.Geteuid()))
		}
	}
	if o.backend == "auto" {
		o.backend = "nm"
		if _, err := os.Stat("/boot/dietpi/dietpi-network"); err == nil {
			o.backend = "dietpi"
		}
	}
	return o, nil
}
func enabled() bool {
	if os.Getenv("KALINKA_BLE_SETUP") == "0" {
		return false
	}
	b, err := os.ReadFile("/boot/dietpi.txt")
	if err != nil {
		return true
	}
	r := regexp.MustCompile(`(?m)^\s*KALINKA_BLE_SETUP\s*=\s*['"]?0['"]?\s*(?:#.*)?$`)
	return !r.Match(b)
}
func serve(ctx context.Context, o options) error {
	if err := system.PrivateDir(o.runtime); err != nil {
		return err
	}
	if err := system.PrivateDir(o.state); err != nil {
		return err
	}
	lock, err := system.Lock(filepath.Join(o.runtime, "lock"))
	if err != nil {
		return err
	}
	defer lock.Close()
	var backend wifi.Backend
	if o.test {
		backend = &wifi.DryRun{Result: o.result, IP: o.address, Delay: time.Second}
	} else if o.backend == "nm" {
		backend = wifi.NewNetworkManager(o.iface, o.state)
	} else {
		backend, err = wifi.NewDietPi(o.iface, "/", o.state, o.runtime)
		if err != nil {
			return err
		}
	}
	defer backend.Close()
	recovery, cancel := context.WithTimeout(ctx, 75*time.Second)
	err = backend.Recover(recovery)
	cancel()
	if err != nil {
		return err
	}
	marker := filepath.Join(o.state, "networked")
	if o.test || o.always {
		marker = ""
	}
	if marker != "" {
		if _, err = os.Stat("/var/lib/kalinka-image/networked"); err == nil {
			if err = wifi.AtomicWrite(marker, nil, 0600); err != nil {
				return err
			}
		}
	}
	m := machine.New(ctx, backend, machine.Config{IdentityFile: o.identity, Marker: marker, Port: uint16(o.port), Test: o.test, AlwaysAdvertise: o.always})
	defer m.Close()
	radio, err := gatt.New(ctx, m, o.adapter)
	if err != nil {
		return err
	}
	defer radio.Close()
	eventsCtx, stopEvents := context.WithCancel(ctx)
	defer stopEvents()
	events := make(chan error, 1)
	go func() { events <- radio.Events(eventsCtx) }()
	if err = system.Notify("READY=1\nSTATUS=Watching network availability"); err != nil {
		return err
	}
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	lastWatchdog := time.Now()
	watchdog := system.WatchdogInterval()
	advertising := false
	for {
		active, err := m.Tick(ctx)
		if err != nil {
			return err
		}
		if active && !advertising {
			if err = radio.Enable(ctx); err != nil {
				return err
			}
			slog.Info("Nearby setup available", "name", radio.Name)
			advertising = true
		} else if !active && advertising {
			radio.Disable(ctx)
			advertising = false
			slog.Info("Nearby setup closed")
		}
		if watchdog > 0 && time.Since(lastWatchdog) >= watchdog {
			if err = system.Notify("WATCHDOG=1"); err != nil {
				return err
			}
			lastWatchdog = time.Now()
		}
		select {
		case <-ctx.Done():
			_ = system.Notify("STOPPING=1")
			return nil
		case err := <-events:
			return err
		case <-ticker.C:
		}
	}
}
func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, nil)))
	o, err := parse(os.Args[1:])
	if err == flag.ErrHelp {
		return
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "Invalid supervisor options; use --help")
		os.Exit(2)
	}
	if o.checkEnabled {
		if !enabled() {
			os.Exit(1)
		}
		return
	}
	if o.version {
		fmt.Println("kalinka-supervisor " + version)
		return
	}
	if !o.test && !o.always && !enabled() {
		slog.Info("Nearby setup disabled")
		return
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err = serve(ctx, o); err != nil && ctx.Err() == nil {
		code := "unavailable"
		if p, ok := err.(protocol.Error); ok {
			code = strings.ToLower(string(p))
		}
		slog.Error("Supervisor unavailable", "reason", code)
		os.Exit(1)
	}
}
