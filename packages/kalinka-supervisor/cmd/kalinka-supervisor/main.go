// Kalinka Supervisor runs independently of Core and its Python installation.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
	"sync"
	"syscall"
	"time"

	"kalinka/supervisor/internal/control"
	"kalinka/supervisor/internal/coreconf"
	"kalinka/supervisor/internal/dashboard"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/provision"
	"kalinka/supervisor/internal/system"
)

var version = "development"

const (
	defaultListenPort = 8001
	dietpiConfig      = "/boot/dietpi.txt"
	tick              = 2 * time.Second
)

type options struct {
	provision.Options
	config               string
	corePort, listenPort int
	version              bool
}

func parse(args []string) (options, error) {
	var o options
	f := flag.NewFlagSet("kalinka-supervisor", flag.ContinueOnError)
	f.StringVar(&o.Backend, "backend", "auto", "Networking backend: auto, dietpi, nm")
	f.StringVar(&o.Interface, "interface", "", "Wi-Fi interface (auto-detected by default)")
	f.StringVar(&o.Adapter, "adapter", "hci0", "BlueZ adapter")
	f.StringVar(&o.IdentityFile, "server-id-file", coreconf.IdentityPath, "Core identity file")
	f.StringVar(&o.config, "server-config", coreconf.DefaultPath, "Core configuration file")
	f.StringVar(&o.State, "state-dir", "", "Private persistent supervisor state directory")
	f.StringVar(&o.Runtime, "runtime-dir", "", "Private runtime directory")
	f.BoolVar(&o.Test, "test", false, "Real BLE with simulated Wi-Fi and simulated control actions")
	f.BoolVar(&o.AlwaysAdvertise, "always-advertise", false, "Keep setup available while online (local testing only)")
	f.StringVar(&o.TestResult, "test-result", "joined", "Simulated result: joined, wrong_password, no_address, timeout")
	f.StringVar(&o.TestAddress, "test-address", "", "IPv4 address returned in test mode")
	f.IntVar(&o.corePort, "port", 0, "Core HTTP port reported over BLE and trusted as the web player's origin (default: follow Core's configuration)")
	f.IntVar(&o.listenPort, "listen-port", defaultListenPort, "Control API port")
	f.BoolVar(&o.version, "version", false, "Print version")
	if err := f.Parse(args); err != nil {
		return o, err
	}
	if f.NArg() != 0 {
		return o, protocol.Invalid
	}
	if o.version {
		return o, nil
	}
	if o.Backend != "auto" && o.Backend != "dietpi" && o.Backend != "nm" {
		return o, protocol.Invalid
	}
	if !o.Test && (o.TestAddress != "" || o.TestResult != "joined") {
		return o, protocol.Invalid
	}
	switch o.TestResult {
	case "joined", "wrong_password", "no_address", "timeout":
	default:
		return o, protocol.Invalid
	}
	if o.TestAddress != "" && net.ParseIP(o.TestAddress).To4() == nil {
		return o, protocol.Invalid
	}
	if o.corePort < 0 || o.corePort > 65535 || o.listenPort < 1 || o.listenPort > 65535 {
		return o, protocol.Invalid
	}
	if o.State == "" {
		if os.Geteuid() == 0 {
			o.State = "/var/lib/kalinka-supervisor"
		} else {
			base, err := os.UserConfigDir()
			if err != nil {
				return o, protocol.Storage
			}
			o.State = filepath.Join(base, "kalinka-supervisor")
		}
	}
	if o.Runtime == "" {
		if os.Geteuid() == 0 {
			o.Runtime = "/run/kalinka-supervisor"
		} else {
			base := os.Getenv("XDG_RUNTIME_DIR")
			if base == "" {
				base = os.TempDir()
			}
			o.Runtime = filepath.Join(base, "kalinka-supervisor-"+strconv.Itoa(os.Geteuid()))
		}
	}
	if o.Backend == "auto" {
		o.Backend = "nm"
		if _, err := os.Stat("/boot/dietpi/dietpi-network"); err == nil {
			o.Backend = "dietpi"
		}
	}
	return o, nil
}

// enabled is false when name=0 is set in the environment or in DietPi's configuration file.
func enabled(name, dietpi string) bool {
	if os.Getenv(name) == "0" {
		return false
	}
	b, err := os.ReadFile(dietpi)
	if err != nil {
		return true
	}
	r := regexp.MustCompile(`(?m)^\s*` + regexp.QuoteMeta(name) + `\s*=\s*['"]?0['"]?\s*(?:#.*)?$`)
	return !r.Match(b)
}

func systemdFor(o options) control.Systemd {
	if o.Test || os.Geteuid() != 0 {
		slog.Info("Control actions are simulated")
		return control.Simulated{}
	}
	return control.NewSystemdBus()
}

func run(ctx context.Context, o options, setupOn, apiOn bool) error {
	if err := system.PrivateDir(o.Runtime); err != nil {
		return err
	}
	if err := system.PrivateDir(o.State); err != nil {
		return err
	}
	lock, err := system.Lock(filepath.Join(o.Runtime, "lock"))
	if err != nil {
		return err
	}
	defer lock.Close()
	core := coreconf.NewSource(o.config)
	corePort := func() int {
		if o.corePort != 0 {
			return o.corePort
		}
		return core.Current().Port
	}

	var components sync.WaitGroup
	defer components.Wait()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	setup := &componentState{name: "Nearby setup"}
	var service *provision.Service
	var network control.NetworkActivity
	if setupOn {
		service = provision.New(o.Options, func() uint16 { return uint16(corePort()) })
		network = service
		components.Go(func() {
			supervise(ctx, component{
				blocked: func() string { return provision.Blocked(o.Options, "/", exec.LookPath) },
				run:     service.Run,
				poll:    tick,
				backoff: 10 * time.Second,
				state:   setup,
			})
		})
	}
	var binding *control.Binding
	if apiOn {
		systemd := systemdFor(o)
		reinstalls := control.NewReinstallRecord(o.State)
		board := dashboard.New("/", func(ctx context.Context, unit string) (string, error) {
			state, err := systemd.Unit(ctx, unit)
			return state.Active, err
		})
		components.Go(func() { board.Run(ctx, 5*time.Second) })
		controller := control.NewController(systemd, network, reinstalls, time.Now)
		binding = control.NewBinding(o.listenPort, control.NewHandler(controller, control.HandlerConfig{
			Version:      version,
			ServerIDFile: o.IdentityFile,
			CorePort:     corePort,
			Setup:        setup.Phase,
			Dashboard:    func(ctx context.Context) any { return board.Snapshot(ctx) },
			Reinstalls:   reinstalls,
		}))
		defer binding.Close()
	}

	ticker := time.NewTicker(tick)
	defer ticker.Stop()
	watchdog := system.WatchdogInterval()
	lastPing := time.Now()
	ready := false
	api, status := "", ""
	for {
		core.Refresh()
		latest := "off"
		if binding != nil {
			latest = binding.Reconcile(core.Current())
		}
		if latest != api {
			slog.Info("Control API " + latest)
			api = latest
		}
		if line := "Control API " + api + "; nearby setup " + setup.Describe(); line != status {
			_ = system.Notify("STATUS=" + line)
			status = line
		}
		if !ready {
			if err = system.Notify("READY=1"); err != nil {
				return err
			}
			ready = true
		}
		if watchdog > 0 && time.Since(lastPing) >= watchdog && (service == nil || !service.Wedged(time.Now(), watchdog)) {
			if err = system.Notify("WATCHDOG=1"); err != nil {
				return err
			}
			lastPing = time.Now()
		}
		select {
		case <-ctx.Done():
			_ = system.Notify("STOPPING=1")
			// Draining HTTP overlaps with closing the radio and rolling back any join.
			cancel()
			if binding != nil {
				binding.Close()
			}
			return nil
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
	if o.version {
		fmt.Println("kalinka-supervisor " + version)
		return
	}
	setupOn := o.Test || o.AlwaysAdvertise || enabled("KALINKA_BLE_SETUP", dietpiConfig)
	apiOn := enabled("KALINKA_CONTROL_API", dietpiConfig)
	if !setupOn && !apiOn {
		slog.Info("Nearby setup and the control API are both disabled")
		// A notify unit that exits before READY fails as a protocol error and is restarted forever.
		_ = system.Notify("READY=1\nSTATUS=Nearby setup and the control API are both disabled")
		return
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err = run(ctx, o, setupOn, apiOn); err != nil && ctx.Err() == nil {
		slog.Error("Supervisor unavailable", "reason", failureCode(err))
		os.Exit(1)
	}
}
