package dashboard

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

type box struct {
	t    *testing.T
	root string
}

func (b box) write(path, body string) {
	b.t.Helper()
	full := filepath.Join(b.root, path)
	if err := os.MkdirAll(filepath.Dir(full), 0755); err != nil {
		b.t.Fatal(err)
	}
	if err := os.WriteFile(full, []byte(body), 0644); err != nil {
		b.t.Fatal(err)
	}
}

func stage(t *testing.T) box {
	b := box{t, t.TempDir()}
	b.write("proc/stat", "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0 0 0\ncpu1 50 0 50 350 50 0 0 0 0 0\nintr 1\n")
	b.write("proc/meminfo", "MemTotal:        4000000 kB\nMemFree:          100000 kB\nMemAvailable:    3000000 kB\n")
	b.write("proc/uptime", "93784.21 180000.00\n")
	b.write("proc/loadavg", "0.25 0.50 1.00 1/200 4242\n")
	b.write("proc/sys/kernel/osrelease", "6.12.47+rpt-rpi-v8\n")
	b.write("etc/os-release", "NAME=\"Debian GNU/Linux\"\nPRETTY_NAME=\"Debian GNU/Linux 13 (trixie)\"\n")
	b.write("boot/dietpi/.version", "G_DIETPI_VERSION_CORE=9\nG_DIETPI_VERSION_SUB=17\nG_DIETPI_VERSION_RC=2\n")
	b.write("sys/class/thermal/thermal_zone0/temp", "48312\n")
	b.write("sys/fs/cgroup/system.slice/kalinka.service/cgroup.procs", "101\n102\n")
	b.write("sys/fs/cgroup/system.slice/kalinka.service/cpu.stat", "usage_usec 1000000\nuser_usec 800000\n")
	b.write("sys/fs/cgroup/system.slice/kalinka-supervisor.service/cgroup.procs", "201\n")
	b.write("sys/fs/cgroup/system.slice/kalinka-supervisor.service/cpu.stat", "usage_usec 50000\n")
	b.write("sys/fs/cgroup/system.slice/system-kalinka\\x2dwifi.slice/kalinka-wifi@wlan0.service/cgroup.procs", "301\n")
	b.write("sys/fs/cgroup/system.slice/system-kalinka\\x2dwifi.slice/kalinka-wifi@wlan0.service/cpu.stat", "usage_usec 10\n")
	b.write("proc/101/status", "Name:\tkalinka-server\nVmRSS:\t  200000 kB\n")
	b.write("proc/101/smaps_rollup", "00400000-ffffd000 ---p 00000000 00:00 0  [rollup]\nRss:  200000 kB\nPss_Anon:  90000 kB\nPss:  150000 kB\n")
	b.write("proc/102/status", "Name:\tpython3\nVmRSS:\t   50000 kB\n")
	b.write("proc/201/status", "Name:\tkalinka-superv\nVmRSS:\t   12000 kB\n")
	b.write("proc/301/status", "Name:\tdhclient\nVmRSS:\t    4000 kB\n")
	b.write("var/lib/dpkg/status", ""+
		"Package: bash\nStatus: install ok installed\nVersion: 5.2\n\n"+
		"Package: kalinka-server\nStatus: install ok installed\nVersion: 4.3.2\nDescription: x\n multi-line\n\n"+
		"Package: kalinka-renderer\nStatus: install ok installed\nVersion: 0.4.0\n\n"+
		"Package: kalinka-plugin-musiccast\nStatus: deinstall ok config-files\nVersion: 1.0.0\n")
	venv := "opt/kalinka/venv/lib/python3.13/site-packages/"
	b.write("opt/kalinka/venv/pyvenv.cfg", "home = /usr/bin\nversion_info = 3.13.5\n")
	b.write(venv+"kalinka_plugin_localfiles-2.1.0.dist-info/METADATA", "Metadata-Version: 2.4\nName: kalinka-plugin-localfiles\nVersion: 2.1.0\n\nName: not a header\n")
	b.write(venv+"kalinka_plugin_localfiles-2.1.0.dist-info/entry_points.txt", "[kalinka.plugins]\nkalinka_plugin_localfiles = kalinka_plugin_localfiles:Plugin\n")
	b.write(venv+"kalinka_server-4.3.2.dist-info/METADATA", "Name: kalinka-server\nVersion: 4.3.2\n")
	b.write(venv+"kalinka_server-4.3.2.dist-info/entry_points.txt", "[console_scripts]\nkalinka-server = kalinka_server.__main__:main\n")
	b.write(venv+"numpy-2.0.0.dist-info/METADATA", "Name: numpy\nVersion: 2.0.0\n")
	return b
}

func states(_ context.Context, unit string) (string, error) {
	switch unit {
	case "kalinka.service", "kalinka-supervisor.service", "kalinka-wifi@wlan0.service":
		return "active", nil
	case "kalinka-renderer.service":
		return "inactive", nil
	}
	return "", errors.New("no such unit")
}

func newCollector(b box) *Collector {
	c := New(b.root, states)
	c.hostname = func() (string, error) { return "kalinka-kitchen", nil }
	return c
}

func TestSnapshotReadsTheBox(t *testing.T) {
	b := stage(t)
	s := newCollector(b).Snapshot(context.Background())

	h := s.Host
	if h.Hostname != "kalinka-kitchen" || h.OS != "Debian GNU/Linux 13 (trixie)" || h.DietPi != "v9.17.2" || h.Kernel != "6.12.47+rpt-rpi-v8" {
		t.Fatalf("host identity = %+v", h)
	}
	if h.CPUs != 2 || h.UptimeSeconds != 93784.21 || h.Load != [3]float64{0.25, 0.5, 1} {
		t.Fatalf("host load = %+v", h)
	}
	if h.MemoryTotal != 4000000*1024 || h.MemoryAvailable != 3000000*1024 || h.DiskTotal == 0 {
		t.Fatalf("host capacity = %+v", h)
	}
	if h.TemperatureC == nil || *h.TemperatureC != 48.312 {
		t.Fatalf("temperature = %v", h.TemperatureC)
	}
	if h.CPUPercent != nil {
		t.Fatal("a CPU average from a single reading")
	}

	want := []struct {
		unit, state string
		processes   int
		memory      *uint64
	}{
		{"kalinka.service", "active", 2, ptr(uint64(200000 * 1024))},
		{"kalinka-renderer.service", "inactive", 0, nil},
		{"kalinka-supervisor.service", "active", 1, ptr(uint64(12000 * 1024))},
		{"kalinka-wifi@wlan0.service", "active", 1, ptr(uint64(4000 * 1024))},
	}
	if len(s.Services) != len(want) {
		t.Fatalf("services = %+v", s.Services)
	}
	for i, w := range want {
		got := s.Services[i]
		if got.Unit != w.unit || got.State != w.state || got.Processes != w.processes || !sameUint(got.Memory, w.memory) {
			t.Errorf("service %d = %+v, want %+v", i, got, w)
		}
	}
	if s.Kalinka.Memory == nil || *s.Kalinka.Memory != (200000+12000+4000)*1024 || s.Kalinka.CPUPercent != nil {
		t.Fatalf("totals = %+v", s.Kalinka)
	}

	if len(s.Packages) != 2 || s.Packages[0] != (Package{"kalinka-renderer", "0.4.0"}) || s.Packages[1] != (Package{"kalinka-server", "4.3.2"}) {
		t.Fatalf("packages = %+v", s.Packages)
	}
	if len(s.Plugins) != 1 || s.Plugins[0] != (Package{"kalinka-plugin-localfiles", "2.1.0"}) || s.Python != "3.13.5" {
		t.Fatalf("plugins = %+v, python %q", s.Plugins, s.Python)
	}
}

func TestSoftwareIsRereadOnlyAfterAnInstall(t *testing.T) {
	b := stage(t)
	c := newCollector(b)
	c.Snapshot(context.Background())
	meta := filepath.Join(b.root, "opt/kalinka/venv/lib/python3.13/site-packages/kalinka_plugin_localfiles-2.1.0.dist-info/METADATA")
	stamp, err := os.Stat(meta)
	if err != nil {
		t.Fatal(err)
	}
	b.write("opt/kalinka/venv/lib/python3.13/site-packages/kalinka_plugin_localfiles-2.1.0.dist-info/METADATA", "Name: kalinka-plugin-localfiles\nVersion: 9.9.9\n")
	if err = os.Chtimes(meta, stamp.ModTime(), stamp.ModTime()); err != nil {
		t.Fatal(err)
	}
	if got := c.Snapshot(context.Background()).Plugins[0].Version; got != "2.1.0" {
		t.Fatalf("plugins reread without an install: %q", got)
	}

	b.write("var/lib/dpkg/status", "Package: kalinka-server\nStatus: install ok installed\nVersion: 4.4.0\n")
	b.write("opt/kalinka/venv/lib/python3.13/site-packages/kalinka_plugin_web-1.0.0.dist-info/METADATA", "Name: kalinka-plugin-web\nVersion: 1.0.0\n")
	b.write("opt/kalinka/venv/lib/python3.13/site-packages/kalinka_plugin_web-1.0.0.dist-info/entry_points.txt", "[kalinka.plugins]\n")
	s := c.Snapshot(context.Background())
	if len(s.Packages) != 1 || s.Packages[0] != (Package{"kalinka-server", "4.4.0"}) {
		t.Fatalf("packages after an upgrade = %+v", s.Packages)
	}
	if len(s.Plugins) != 2 || s.Plugins[0].Version != "9.9.9" {
		t.Fatalf("plugins after an install = %+v", s.Plugins)
	}
}

func TestCPUAverages(t *testing.T) {
	b := stage(t)
	c := newCollector(b)
	start := time.Unix(10_000, 0)
	c.Sample(start)
	b.write("proc/stat", "cpu  150 0 150 800 100 0 0 0 0 0\ncpu0 1\ncpu1 1\n")
	b.write("sys/fs/cgroup/system.slice/kalinka.service/cpu.stat", "usage_usec 3000000\n")
	b.write("sys/fs/cgroup/system.slice/kalinka-supervisor.service/cpu.stat", "usage_usec 150000\n")
	c.Sample(start.Add(10 * time.Second))

	s := c.Snapshot(context.Background())
	// Host: 100 busy ticks out of 200; the server: 2 s of CPU in 10 s on two processors.
	if s.Host.CPUPercent == nil || *s.Host.CPUPercent != 50 {
		t.Fatalf("host CPU = %v", s.Host.CPUPercent)
	}
	server := s.Services[0]
	if server.CPUPercent == nil || *server.CPUPercent != 10 {
		t.Fatalf("server CPU = %v", server.CPUPercent)
	}
	if s.Kalinka.CPUPercent == nil || *s.Kalinka.CPUPercent != 10.5 {
		t.Fatalf("Kalinka CPU = %v", s.Kalinka.CPUPercent)
	}

	// A restarted server has a new control group whose counter starts at zero.
	b.write("sys/fs/cgroup/system.slice/kalinka.service/cpu.stat", "usage_usec 1000\n")
	c.Sample(start.Add(20 * time.Second))
	if got := c.Snapshot(context.Background()).Services[0].CPUPercent; got != nil {
		t.Fatalf("average across a restart = %v", *got)
	}
}

func TestOldSamplesAgeOut(t *testing.T) {
	b := stage(t)
	c := newCollector(b)
	start := time.Unix(10_000, 0)
	for i := range 30 {
		b.write("sys/fs/cgroup/system.slice/kalinka.service/cpu.stat", "usage_usec "+strconv.Itoa(1000000*(i+1))+"\n")
		c.Sample(start.Add(time.Duration(i) * 5 * time.Second))
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if n := len(c.series["kalinka.service"]); n > 14 {
		t.Fatalf("kept %d samples for a one-minute window", n)
	}
}

func TestMissingBoxStillAnswers(t *testing.T) {
	c := New(t.TempDir(), func(context.Context, string) (string, error) { return "", errors.New("no systemd") })
	c.Sample(time.Now())
	s := c.Snapshot(context.Background())
	if len(s.Services) != len(MainUnits) || s.Services[0].State != "unknown" || s.Services[0].Memory != nil {
		t.Fatalf("services = %+v", s.Services)
	}
	b, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	var round map[string]any
	if err := json.Unmarshal(b, &round); err != nil || round["packages"] == nil || round["plugins"] == nil {
		t.Fatalf("empty lists must encode as [], got %s", b)
	}
}

func ptr[T any](v T) *T { return &v }

func sameUint(a, b *uint64) bool {
	return a == nil && b == nil || a != nil && b != nil && *a == *b
}
