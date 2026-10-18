// Package timezone reads the zones in the box's tzdata and makes one of them
// the box's own through systemd-timedated.
package timezone

import (
	"bufio"
	"context"
	"errors"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/godbus/dbus/v5"
	"kalinka/supervisor/internal/dbusx"
)

const (
	zoneinfo = "usr/share/zoneinfo"
	// timedated is activated over D-Bus; without this file nobody answers its name.
	timedatedService = "usr/share/dbus-1/system-services/org.freedesktop.timedate1.service"
	// A mask leaves the activation file in place, but D-Bus then cannot start timedated.
	timedatedUnit = "etc/systemd/system/systemd-timedated.service"
	timedate1     = "org.freedesktop.timedate1"
	timedatePath  = dbus.ObjectPath("/org/freedesktop/timedate1")
)

var namePattern = regexp.MustCompile(`^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$`)

// ValidName reports whether name has the shape of a tzdata name: no leading
// '/', no '.' and so no '..', nothing a path could leave the zone directory by.
func ValidName(name string) bool {
	return len(name) <= 64 && namePattern.MatchString(name)
}

// Zones reads the tzdata under root; New("/") reads the box's own. Safe for concurrent use.
type Zones struct{ root string }

func New(root string) *Zones { return &Zones{root} }

// Supported reports whether the box has the tzdata to choose from and an unmasked timedated to set a zone with.
func (z *Zones) Supported() bool {
	_, service := os.Stat(filepath.Join(z.root, timedatedService))
	_, table := os.Stat(z.path("zone.tab"))
	mask, _ := os.Readlink(filepath.Join(z.root, timedatedUnit))
	return service == nil && table == nil && mask != "/dev/null"
}

// Resolve returns the zone timedated should be given for name: name itself
// when tzdata has it, else the zone tzdata links that name to. Browsers and
// phones still report links such as Asia/Calcutta, whose files trixie leaves to tzdata-legacy.
func (z *Zones) Resolve(name string) (string, bool) {
	if !ValidName(name) {
		return "", false
	}
	if z.zone(name) {
		return name, true
	}
	if target := z.linkTarget(name); ValidName(target) && z.zone(target) {
		return target, true
	}
	return "", false
}

// Current is the zone /etc/localtime names, read as timedated reads it: UTC
// without the link, nil for a link to no zone or a copied zone file. Reading
// the system's own value keeps a zone set through dietpi-config in view.
func (z *Zones) Current() *string {
	target, err := os.Readlink(filepath.Join(z.root, "etc/localtime"))
	if errors.Is(err, fs.ErrNotExist) {
		utc := "UTC"
		return &utc
	}
	if err != nil {
		return nil
	}
	for _, prefix := range []string{"/usr/share/zoneinfo/", "../usr/share/zoneinfo/"} {
		if name, found := strings.CutPrefix(target, prefix); found && ValidName(name) {
			return &name
		}
	}
	return nil
}

// Offered lists UTC and the zones zone.tab offers people to choose from, sorted.
func (z *Zones) Offered() []string {
	zones := []string{}
	if z.zone("UTC") {
		zones = append(zones, "UTC")
	}
	f, err := os.Open(z.path("zone.tab"))
	if err != nil {
		return zones
	}
	defer f.Close()
	lines := bufio.NewScanner(f)
	for lines.Scan() {
		columns := strings.Split(lines.Text(), "\t")
		if len(columns) >= 3 && !strings.HasPrefix(columns[0], "#") && ValidName(columns[2]) && z.zone(columns[2]) {
			zones = append(zones, columns[2])
		}
	}
	slices.Sort(zones)
	return slices.Compact(zones)
}

func (z *Zones) path(name string) string { return filepath.Join(z.root, zoneinfo, name) }

// zone reports whether name is a compiled zone, the check timedated itself makes.
func (z *Zones) zone(name string) bool {
	f, err := os.Open(z.path(name))
	if err != nil {
		return false
	}
	defer f.Close()
	magic := make([]byte, 4)
	_, err = io.ReadFull(f, magic)
	return err == nil && string(magic) == "TZif"
}

// linkTarget reads tzdata.zi's "L target name" lines, which name every link whether or not its file is installed.
func (z *Zones) linkTarget(name string) string {
	f, err := os.Open(z.path("tzdata.zi"))
	if err != nil {
		return ""
	}
	defer f.Close()
	lines := bufio.NewScanner(f)
	for lines.Scan() {
		if fields := strings.Fields(lines.Text()); len(fields) == 3 && fields[0] == "L" && fields[2] == name {
			return fields[1]
		}
	}
	return ""
}

// Setter makes a zone the box's own through timedated, which writes
// /etc/localtime outside the supervisor's sandbox. Safe for concurrent use.
type Setter struct {
	simulated bool
	dial      func() (dbusx.Bus, error)
}

// NewSetter sets zones for real unless simulated.
func NewSetter(simulated bool) *Setter {
	return &Setter{simulated: simulated, dial: func() (dbusx.Bus, error) { return dbusx.System(timedate1) }}
}

// Set hands timedated a zone Zones.Resolve returned; timedated checks it against tzdata again.
func (s *Setter) Set(ctx context.Context, zone string) error {
	if s.simulated {
		slog.Info("Simulated time zone change", "timezone", zone)
		return nil
	}
	// The API answers only "unavailable", so the journal is where timedated's reason is kept.
	if err := s.call(ctx, zone); err != nil {
		slog.Warn("Time zone change failed", "timezone", zone, "error", err)
		return err
	}
	slog.Info("Time zone set", "timezone", zone)
	return nil
}

func (s *Setter) call(ctx context.Context, zone string) error {
	bus, err := s.dial()
	if err != nil {
		return err
	}
	defer bus.Close()
	_, err = bus.Call(ctx, timedatePath, timedate1+".SetTimezone", zone, false)
	return err
}
