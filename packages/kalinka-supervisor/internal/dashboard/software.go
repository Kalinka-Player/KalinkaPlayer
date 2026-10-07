package dashboard

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

type software struct {
	packages, plugins []Package
	python            string
}

// software returns what is installed, reading dpkg's database and Core's
// environment again only after either has changed.
func (c *Collector) software() software {
	stamp := c.softwareStamp()
	c.mu.Lock()
	if stamp == c.installedStamp && c.installed.packages != nil {
		cached := c.installed
		c.mu.Unlock()
		return cached
	}
	c.mu.Unlock()
	fresh := software{packages: c.packages(), plugins: c.plugins(), python: c.python()}
	c.mu.Lock()
	c.installed, c.installedStamp = fresh, stamp
	c.mu.Unlock()
	return fresh
}

// softwareStamp changes whenever a package, or a distribution in Core's environment, is installed or removed.
func (c *Collector) softwareStamp() string {
	paths := []string{c.path("var/lib/dpkg/status"), c.path(CoreEnvironment + "/pyvenv.cfg")}
	dirs, _ := filepath.Glob(c.path(CoreEnvironment + "/lib/python3*/site-packages"))
	var b strings.Builder
	for _, p := range append(paths, dirs...) {
		b.WriteString(p)
		if info, err := os.Stat(p); err == nil {
			fmt.Fprintf(&b, " %d %d", info.ModTime().UnixNano(), info.Size())
		}
		b.WriteByte('\n')
	}
	return b.String()
}

// packages lists the installed kalinka* Debian packages from dpkg's database.
func (c *Collector) packages() []Package {
	found := []Package{}
	f, err := os.Open(c.path("var/lib/dpkg/status"))
	if err != nil {
		return found
	}
	defer f.Close()
	var name, version, status string
	flush := func() {
		if strings.HasPrefix(name, "kalinka") && status == "install ok installed" {
			found = append(found, Package{name, version})
		}
		name, version, status = "", "", ""
	}
	s := bufio.NewScanner(f)
	s.Buffer(make([]byte, 64*1024), 1024*1024)
	for s.Scan() {
		line := s.Text()
		if line == "" {
			flush()
			continue
		}
		key, value, _ := strings.Cut(line, ":")
		switch key {
		case "Package":
			name = strings.TrimSpace(value)
		case "Version":
			version = strings.TrimSpace(value)
		case "Status":
			status = strings.TrimSpace(value)
		}
	}
	flush()
	slices.SortFunc(found, func(a, b Package) int { return strings.Compare(a.Name, b.Name) })
	return found
}

// plugins lists the distributions in Core's environment that declare a
// kalinka.plugins entry point, as Core's own inventory does, without importing them.
func (c *Collector) plugins() []Package {
	found := []Package{}
	dirs, _ := filepath.Glob(c.path(CoreEnvironment + "/lib/python3*/site-packages/*.dist-info"))
	for _, dir := range dirs {
		if !declaresPlugin(filepath.Join(dir, "entry_points.txt")) {
			continue
		}
		meta := headers(filepath.Join(dir, "METADATA"))
		if meta["Name"] != "" {
			found = append(found, Package{meta["Name"], meta["Version"]})
		}
	}
	slices.SortFunc(found, func(a, b Package) int { return strings.Compare(a.Name, b.Name) })
	return found
}

func (c *Collector) python() string {
	v := assignments(c.path(CoreEnvironment + "/pyvenv.cfg"))
	if v["version_info"] != "" {
		return v["version_info"]
	}
	return v["version"]
}

func declaresPlugin(path string) bool {
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		if strings.TrimSpace(s.Text()) == "[kalinka.plugins]" {
			return true
		}
	}
	return false
}

// headers reads the RFC 822 header block that opens a METADATA file.
func headers(path string) map[string]string {
	values := map[string]string{}
	f, err := os.Open(path)
	if err != nil {
		return values
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() && s.Text() != "" {
		if key, value, ok := strings.Cut(s.Text(), ":"); ok {
			if _, seen := values[key]; !seen {
				values[key] = strings.TrimSpace(value)
			}
		}
	}
	return values
}
