// Package dashboard reports what runs on the box and what it costs, from the
// files the kernel, dpkg and Core's environment leave on disk. It never talks
// to Core and never runs anything from Core's environment.
package dashboard

import (
	"context"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"time"
)

// MainUnits are reported whether they run or not; other kalinka units only while they run.
var MainUnits = []string{"kalinka.service", "kalinka-renderer.service", "kalinka-supervisor.service"}

// CoreEnvironment is where Core's Python environment lives on an install.
const CoreEnvironment = "/opt/kalinka/venv"

// Snapshot is the dashboard at one moment. A figure the box cannot provide is
// null rather than zero.
type Snapshot struct {
	Host     Host      `json:"host"`
	Services []Service `json:"services"`
	Kalinka  Usage     `json:"kalinka"`
	Packages []Package `json:"packages"`
	Plugins  []Package `json:"plugins"`
	Python   string    `json:"python"`
}

type Host struct {
	Hostname        string     `json:"hostname"`
	OS              string     `json:"os"`
	DietPi          string     `json:"dietpi"`
	Kernel          string     `json:"kernel"`
	UptimeSeconds   float64    `json:"uptime_seconds"`
	Load            [3]float64 `json:"load"`
	CPUs            int        `json:"cpus"`
	CPUPercent      *float64   `json:"cpu_percent"`
	MemoryTotal     uint64     `json:"memory_total"`
	MemoryAvailable uint64     `json:"memory_available"`
	DiskTotal       uint64     `json:"disk_total"`
	DiskFree        uint64     `json:"disk_free"`
	TemperatureC    *float64   `json:"temperature_c"`
}

// Service is one systemd unit. Memory is the proportional set size across its
// processes, so pages they share count once; CPUPercent is its share of the
// whole machine over the last minute.
type Service struct {
	Unit       string   `json:"unit"`
	State      string   `json:"state"`
	Processes  int      `json:"processes"`
	Memory     *uint64  `json:"memory"`
	CPUPercent *float64 `json:"cpu_percent"`
}

// Usage totals the services that could be measured.
type Usage struct {
	Memory     *uint64  `json:"memory"`
	CPUPercent *float64 `json:"cpu_percent"`
}

type Package struct {
	Name    string `json:"name"`
	Version string `json:"version"`
}

// Collector builds snapshots and keeps the CPU samples their averages need.
// Sample and Snapshot are safe to call concurrently.
type Collector struct {
	root      string
	unitState func(context.Context, string) (string, error)
	hostname  func() (string, error)
	window    time.Duration

	mu     sync.Mutex
	series map[string][]sample
	// installed is reread only when dpkg's database or Core's environment changes.
	installed      software
	installedStamp string
}

// New reads the box under root ("/" in production). unitState reports a
// unit's systemd ActiveState.
func New(root string, unitState func(context.Context, string) (string, error)) *Collector {
	return &Collector{root: root, unitState: unitState, hostname: os.Hostname, window: time.Minute, series: map[string][]sample{}}
}

func (c *Collector) path(p string) string { return filepath.Join(c.root, p) }

// Run samples CPU use every interval until ctx ends.
func (c *Collector) Run(ctx context.Context, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		c.Sample(time.Now())
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func (c *Collector) Snapshot(ctx context.Context) Snapshot {
	sw := c.software()
	s := Snapshot{Host: c.host(), Packages: sw.packages, Plugins: sw.plugins, Python: sw.python}
	c.mu.Lock()
	s.Host.CPUPercent = c.average(hostSeries)
	c.mu.Unlock()
	for _, unit := range c.units() {
		svc := Service{Unit: unit, State: "unknown"}
		if state, err := c.unitState(ctx, unit); err == nil {
			svc.State = state
		}
		svc.Processes, svc.Memory = c.memory(unit)
		c.mu.Lock()
		svc.CPUPercent = c.average(unit)
		c.mu.Unlock()
		s.Services = append(s.Services, svc)
		s.Kalinka.Memory = addUint(s.Kalinka.Memory, svc.Memory)
		s.Kalinka.CPUPercent = addFloat(s.Kalinka.CPUPercent, svc.CPUPercent)
	}
	return s
}

// units lists MainUnits, then every other kalinka unit that has a control group now.
func (c *Collector) units() []string {
	units := slices.Clone(MainUnits)
	var found []string
	for _, pattern := range []string{"sys/fs/cgroup/system.slice/kalinka*.service", "sys/fs/cgroup/system.slice/system-kalinka*.slice/kalinka*.service"} {
		matches, _ := filepath.Glob(c.path(pattern))
		for _, m := range matches {
			if unit := filepath.Base(m); !slices.Contains(units, unit) && !slices.Contains(found, unit) {
				found = append(found, unit)
			}
		}
	}
	slices.Sort(found)
	return append(units, found...)
}

func addUint(total, v *uint64) *uint64 {
	if v == nil {
		return total
	}
	sum := *v
	if total != nil {
		sum += *total
	}
	return &sum
}

func addFloat(total, v *float64) *float64 {
	if v == nil {
		return total
	}
	sum := *v
	if total != nil {
		sum += *total
	}
	return &sum
}
