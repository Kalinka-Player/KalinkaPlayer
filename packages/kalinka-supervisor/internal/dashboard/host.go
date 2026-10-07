package dashboard

import (
	"bufio"
	"os"
	"strconv"
	"strings"
	"syscall"
)

func (c *Collector) host() Host {
	h := Host{OS: c.osName(), DietPi: c.dietPi(), Kernel: c.firstLine("proc/sys/kernel/osrelease"), CPUs: c.cpus()}
	h.Hostname, _ = c.hostname()
	if f := strings.Fields(c.firstLine("proc/uptime")); len(f) > 0 {
		h.UptimeSeconds, _ = strconv.ParseFloat(f[0], 64)
	}
	if f := strings.Fields(c.firstLine("proc/loadavg")); len(f) >= 3 {
		for i := range h.Load {
			h.Load[i], _ = strconv.ParseFloat(f[i], 64)
		}
	}
	if kib, ok := field(c.path("proc/meminfo"), "MemTotal:"); ok {
		h.MemoryTotal = uint64(kib) * 1024
	}
	if kib, ok := field(c.path("proc/meminfo"), "MemAvailable:"); ok {
		h.MemoryAvailable = uint64(kib) * 1024
	}
	var fs syscall.Statfs_t
	if syscall.Statfs(c.path("/"), &fs) == nil {
		h.DiskTotal = fs.Blocks * uint64(fs.Bsize)
		h.DiskFree = fs.Bavail * uint64(fs.Bsize)
	}
	if milli, err := strconv.ParseFloat(c.firstLine("sys/class/thermal/thermal_zone0/temp"), 64); err == nil {
		celsius := milli / 1000
		h.TemperatureC = &celsius
	}
	return h
}

func (c *Collector) firstLine(p string) string {
	f, err := os.Open(c.path(p))
	if err != nil {
		return ""
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	s.Scan()
	return strings.TrimSpace(s.Text())
}

func (c *Collector) osName() string {
	return assignments(c.path("etc/os-release"))["PRETTY_NAME"]
}

// dietPi formats DietPi's own version file as "v9.7.1", or "" off DietPi.
func (c *Collector) dietPi() string {
	v := assignments(c.path("boot/dietpi/.version"))
	if v["G_DIETPI_VERSION_CORE"] == "" {
		return ""
	}
	return "v" + v["G_DIETPI_VERSION_CORE"] + "." + v["G_DIETPI_VERSION_SUB"] + "." + v["G_DIETPI_VERSION_RC"]
}

// assignments reads a shell-style KEY=value file, unquoting values.
func assignments(path string) map[string]string {
	values := map[string]string{}
	f, err := os.Open(path)
	if err != nil {
		return values
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		key, value, ok := strings.Cut(strings.TrimSpace(s.Text()), "=")
		if !ok || strings.HasPrefix(key, "#") {
			continue
		}
		values[strings.TrimSpace(key)] = strings.Trim(strings.TrimSpace(value), `"'`)
	}
	return values
}
