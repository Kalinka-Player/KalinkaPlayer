package dashboard

import (
	"bufio"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// hostSeries keys the whole machine's CPU samples, beside one series per unit.
const hostSeries = ""

// sample is cumulative CPU use against cumulative capacity, in matching units:
// ticks for the host, microseconds for a unit's control group.
type sample struct {
	at             time.Time
	used, capacity float64
}

// Sample records how much CPU the host and each kalinka unit have used so far.
func (c *Collector) Sample(now time.Time) {
	readings := map[string]sample{}
	if busy, total, ok := c.hostTicks(); ok {
		readings[hostSeries] = sample{now, busy, total}
	}
	if cpus := c.cpus(); cpus > 0 {
		wall := float64(now.UnixMicro()) * float64(cpus)
		for _, unit := range c.units() {
			if used, ok := c.cpuUsage(unit); ok {
				readings[unit] = sample{now, used, wall}
			}
		}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	for key := range c.series {
		if _, ok := readings[key]; !ok {
			delete(c.series, key)
		}
	}
	for key, s := range readings {
		series := c.series[key]
		// A restarted unit gets a new control group, whose counter starts again.
		if n := len(series); n > 0 && s.used < series[n-1].used {
			series = nil
		}
		series = append(series, s)
		for len(series) > 2 && now.Sub(series[0].at) > c.window {
			series = series[1:]
		}
		c.series[key] = series
	}
}

// average is the share of capacity used across the samples held, in percent; c.mu must be held.
func (c *Collector) average(key string) *float64 {
	series := c.series[key]
	if len(series) < 2 {
		return nil
	}
	first, last := series[0], series[len(series)-1]
	capacity := last.capacity - first.capacity
	if capacity <= 0 {
		return nil
	}
	p := min(max((last.used-first.used)/capacity*100, 0), 100)
	return &p
}

func (c *Collector) cgroup(unit string) string {
	dir := c.path("sys/fs/cgroup/system.slice/" + unit)
	if _, err := os.Stat(dir); err == nil {
		return dir
	}
	if matches, _ := filepath.Glob(c.path("sys/fs/cgroup/system.slice/system-*.slice/" + unit)); len(matches) > 0 {
		return matches[0]
	}
	return ""
}

func (c *Collector) cpuUsage(unit string) (float64, bool) {
	dir := c.cgroup(unit)
	if dir == "" {
		return 0, false
	}
	v, ok := field(filepath.Join(dir, "cpu.stat"), "usage_usec")
	return v, ok
}

// memory counts a unit's processes and sums their memory; nil when the unit has no control group.
func (c *Collector) memory(unit string) (int, *uint64) {
	dir := c.cgroup(unit)
	if dir == "" {
		return 0, nil
	}
	b, err := os.ReadFile(filepath.Join(dir, "cgroup.procs"))
	if err != nil {
		return 0, nil
	}
	var total uint64
	pids := strings.Fields(string(b))
	for _, pid := range pids {
		if kib, ok := c.processMemory(pid); ok {
			total += uint64(kib) * 1024
		}
	}
	return len(pids), &total
}

// processMemory prefers PSS, which splits shared pages among their sharers so forked
// workers are not each charged for them; resident memory stands in where it is unreadable.
func (c *Collector) processMemory(pid string) (float64, bool) {
	if kib, ok := field(c.path("proc/"+pid+"/smaps_rollup"), "Pss:"); ok {
		return kib, true
	}
	return field(c.path("proc/"+pid+"/status"), "VmRSS:")
}

func (c *Collector) hostTicks() (busy, total float64, ok bool) {
	f, err := os.Open(c.path("proc/stat"))
	if err != nil {
		return 0, 0, false
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	if !s.Scan() {
		return 0, 0, false
	}
	fields := strings.Fields(s.Text())
	if len(fields) < 9 || fields[0] != "cpu" {
		return 0, 0, false
	}
	var ticks [8]float64
	for i := range ticks {
		if ticks[i], err = strconv.ParseFloat(fields[i+1], 64); err != nil {
			return 0, 0, false
		}
	}
	for _, t := range ticks {
		total += t
	}
	idle := ticks[3] + ticks[4]
	return total - idle, total, true
}

func (c *Collector) cpus() int {
	f, err := os.Open(c.path("proc/stat"))
	if err != nil {
		return 0
	}
	defer f.Close()
	n := 0
	s := bufio.NewScanner(f)
	for s.Scan() {
		line := s.Text()
		if len(line) > 3 && strings.HasPrefix(line, "cpu") && line[3] >= '0' && line[3] <= '9' {
			n++
		}
	}
	return n
}

// field reads the number after key on the first line that starts with it.
func field(path, key string) (float64, bool) {
	f, err := os.Open(path)
	if err != nil {
		return 0, false
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		fields := strings.Fields(s.Text())
		if len(fields) >= 2 && fields[0] == key {
			v, err := strconv.ParseFloat(fields[1], 64)
			return v, err == nil
		}
	}
	return 0, false
}
