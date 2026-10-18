package timezone

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/godbus/dbus/v5"
	"kalinka/supervisor/internal/dbusx"
)

// stage builds a box with a little tzdata, as Debian's tzdata installs it without tzdata-legacy, and timedated.
func stage(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	write := func(name, content string) {
		path := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0644); err != nil {
			t.Fatal(err)
		}
	}
	for _, zone := range []string{"UTC", "Etc/UTC", "Europe/London", "Europe/Amsterdam", "Asia/Kolkata", "Etc/GMT+5"} {
		write(zoneinfo+"/"+zone, "TZif2 compiled zone")
	}
	write(zoneinfo+"/SECURITY", "Please report security issues")
	write(zoneinfo+"/zone.tab", "# tzdb timezone descriptions\n#\n"+
		"GB\t+513030-0000731\tEurope/London\n"+
		"NL\t+5222+00454\tEurope/Amsterdam\n"+
		"IN\t+2232+08822\tAsia/Kolkata\n"+
		"AQ\t-7750+16636\tAntarctica/McMurdo\tNew Zealand time - McMurdo, South Pole\n")
	write(zoneinfo+"/tzdata.zi", "# version 2026c\n"+
		"Z Asia/Kolkata 5:53:28 - LMT 1854 Jun 28\n"+
		"L Asia/Kolkata Asia/Calcutta\n"+
		"L America/New_York US/Eastern\n"+
		"L Europe/London GB\n")
	write(timedatedService, "[D-BUS Service]\nName=org.freedesktop.timedate1\n")
	return root
}

func TestValidName(t *testing.T) {
	for _, name := range []string{"UTC", "Europe/London", "America/Argentina/Buenos_Aires", "Etc/GMT+5", "Etc/GMT-14", "America/Port-au-Prince"} {
		if !ValidName(name) {
			t.Errorf("%q refused", name)
		}
	}
	for _, name := range []string{"", "/UTC", "../etc/passwd", "Europe/../../etc/passwd", "Europe//London", "Europe/London/",
		"zone.tab", "Europe/Lon don", `Europe\London`, "Europe/London\n", strings.Repeat("A", 65)} {
		if ValidName(name) {
			t.Errorf("%q accepted", name)
		}
	}
}

func TestResolve(t *testing.T) {
	zones := New(stage(t))
	for _, tc := range []struct{ name, want string }{
		{"Europe/London", "Europe/London"},
		{"UTC", "UTC"},
		{"Etc/GMT+5", "Etc/GMT+5"},
		{"Asia/Calcutta", "Asia/Kolkata"},
		{"GB", "Europe/London"},
		{"US/Eastern", ""},
		{"Nowhere/Atlantis", ""},
		{"../etc/passwd", ""},
		{"/UTC", ""},
		{"Europe", ""},
		{"SECURITY", ""},
	} {
		if got, ok := zones.Resolve(tc.name); got != tc.want || ok != (tc.want != "") {
			t.Errorf("Resolve(%q) = %q, %v; want %q", tc.name, got, ok, tc.want)
		}
	}
}

func TestOffered(t *testing.T) {
	root := stage(t)
	if got, want := New(root).Offered(), []string{"Asia/Kolkata", "Europe/Amsterdam", "Europe/London", "UTC"}; !slices.Equal(got, want) {
		t.Fatalf("offered %q, want %q", got, want)
	}
	if err := os.Remove(filepath.Join(root, zoneinfo, "zone.tab")); err != nil {
		t.Fatal(err)
	}
	if got := New(root).Offered(); !slices.Equal(got, []string{"UTC"}) {
		t.Fatalf("without zone.tab, offered %q", got)
	}
}

func TestCurrentReadsTheSystemsOwnLink(t *testing.T) {
	root := stage(t)
	zones := New(root)
	if got := zones.Current(); got == nil || *got != "UTC" {
		t.Fatalf("a box without /etc/localtime reads as %v", got)
	}
	localtime := filepath.Join(root, "etc/localtime")
	if err := os.MkdirAll(filepath.Dir(localtime), 0755); err != nil {
		t.Fatal(err)
	}
	link := func(target string) {
		t.Helper()
		_ = os.Remove(localtime)
		if err := os.Symlink(target, localtime); err != nil {
			t.Fatal(err)
		}
	}
	for target, want := range map[string]string{
		"/usr/share/zoneinfo/Europe/London":    "Europe/London",
		"../usr/share/zoneinfo/Europe/London":  "Europe/London",
		"/usr/share/zoneinfo/Etc/UTC":          "Etc/UTC",
		"../usr/share/zoneinfo/Asia/Kolkata":   "Asia/Kolkata",
		"/usr/share/zoneinfo/America/New_York": "America/New_York",
	} {
		link(target)
		if got := zones.Current(); got == nil || *got != want {
			t.Errorf("a link to %s reads as %v, want %s", target, got, want)
		}
	}
	for _, target := range []string{"/etc/zones/Europe/London", "/usr/share/zoneinfo/../../../etc/passwd", "/usr/share/zoneinfo/"} {
		link(target)
		if got := zones.Current(); got != nil {
			t.Errorf("a link to %s names %q", target, *got)
		}
	}
	_ = os.Remove(localtime)
	if err := os.WriteFile(localtime, []byte("TZif2 a copy"), 0644); err != nil {
		t.Fatal(err)
	}
	if got := zones.Current(); got != nil {
		t.Fatalf("a copied zone file names %q", *got)
	}
}

func TestSupported(t *testing.T) {
	for _, missing := range []string{"", timedatedService, zoneinfo + "/zone.tab"} {
		root := stage(t)
		if missing != "" {
			if err := os.Remove(filepath.Join(root, missing)); err != nil {
				t.Fatal(err)
			}
		}
		if got := New(root).Supported(); got != (missing == "") {
			t.Errorf("without %q, supported = %v", missing, got)
		}
	}
	root := stage(t)
	unit := filepath.Join(root, timedatedUnit)
	if err := os.MkdirAll(filepath.Dir(unit), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("/dev/null", unit); err != nil {
		t.Fatal(err)
	}
	if New(root).Supported() {
		t.Error("a masked timedated is supported")
	}
}

type busCall struct {
	path   dbus.ObjectPath
	method string
	args   []any
}

type fakeTimedate1 struct {
	calls *[]busCall
	err   error
}

func (b fakeTimedate1) Call(_ context.Context, path dbus.ObjectPath, method string, args ...any) ([]any, error) {
	*b.calls = append(*b.calls, busCall{path, method, args})
	return nil, b.err
}
func (fakeTimedate1) Close() error { return nil }

func setterWith(simulated bool, err error) (*Setter, *[]busCall) {
	calls := &[]busCall{}
	s := NewSetter(simulated)
	s.dial = func() (dbusx.Bus, error) { return fakeTimedate1{calls, err}, nil }
	return s, calls
}

func TestSetCallsTimedated(t *testing.T) {
	s, calls := setterWith(false, nil)
	if err := s.Set(context.Background(), "Asia/Kolkata"); err != nil {
		t.Fatal(err)
	}
	want := []busCall{{"/org/freedesktop/timedate1", "org.freedesktop.timedate1.SetTimezone", []any{"Asia/Kolkata", false}}}
	if !reflect.DeepEqual(*calls, want) {
		t.Fatalf("calls = %v", *calls)
	}
}

func TestSetReportsWhatTimedatedRefuses(t *testing.T) {
	refused := dbus.Error{Name: "org.freedesktop.DBus.Error.InvalidArgs", Body: []any{"Invalid or not installed time zone"}}
	s, _ := setterWith(false, refused)
	if err := s.Set(context.Background(), "Europe/London"); dbusx.ErrorName(err) != refused.Name {
		t.Fatalf("Set = %v", err)
	}
	s.dial = func() (dbusx.Bus, error) { return nil, errors.New("no bus") }
	if s.Set(context.Background(), "Europe/London") == nil {
		t.Fatal("a missing bus was ignored")
	}
}

func TestSimulatedSetLeavesTheBoxAlone(t *testing.T) {
	s, calls := setterWith(true, nil)
	if err := s.Set(context.Background(), "Europe/London"); err != nil || len(*calls) != 0 {
		t.Fatalf("simulated Set = %v, calls %v", err, *calls)
	}
}
