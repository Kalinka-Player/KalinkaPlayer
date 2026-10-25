package wifi

import (
	"context"
	"strings"
	"testing"
	"time"
)

const worldReg = `global
country 00: DFS-UNSET
	(755 - 928 @ 2), (N/A, 20), (N/A), NO-IR
	(2402 - 2472 @ 40), (N/A, 20), (N/A)
	(2457 - 2482 @ 20), (N/A, 20), (N/A), AUTO-BW, NO-IR
`

func scanDump(stations ...string) string {
	var b strings.Builder
	for i, s := range stations {
		signal, country, _ := strings.Cut(s, " ")
		b.WriteString("BSS 02:00:00:00:00:0" + string(rune('0'+i)) + "(on wlan0)\n")
		b.WriteString("\tfreq: 2467\n\tbeacon interval: 100 TUs\n\tsignal: " + signal + " dBm\n\tlast seen: 120 ms ago\n\tSSID: Home\n")
		if country != "" {
			b.WriteString("\tCountry: " + country + "\tEnvironment: Indoor/Outdoor\n\t\tChannels [1 - 13] @ 20 dBm\n")
		}
		b.WriteString("\tWMM:\t * Parameter version 1\n")
	}
	return b.String()
}

func TestGlobalCountry(t *testing.T) {
	if got := globalCountry(worldReg); got != "00" {
		t.Errorf("world domain read as %q", got)
	}
	if got := globalCountry("global\ncountry GB: DFS-ETSI\n\nphy#0 (self-managed)\ncountry US: DFS-FCC\n"); got != "GB" {
		t.Errorf("the global country, not a phy's, read as %q", got)
	}
}

func TestAnnouncedCountry(t *testing.T) {
	for _, c := range []struct {
		name, dump, want string
	}{
		{"most access points win", scanDump("-40.00 DE", "-48.00 DE", "-70.00 GB", "-75.00 GB", "-80.00 GB"), "GB"},
		{"a tie goes to the loudest", scanDump("-80.00 GB", "-40.00 DE", "-70.00 GB", "-50.00 DE"), "DE"},
		{"half a dBm as iw prints it", scanDump("-45.-50 DE", "-60.00 GB"), "DE"},
		{"stations without the element do not count", scanDump("-30.00", "-90.00 IE"), "IE"},
		{"no announcement leaves the host alone", scanDump("-30.00", "-40.00"), ""},
		{"a malformed code is ignored", scanDump("-30.00 d3", "-90.00 GB"), "GB"},
	} {
		if got := announcedCountry(c.dump); got != c.want {
			t.Errorf("%s: %q, want %q", c.name, got, c.want)
		}
	}
}

func TestNearbyCountryOnlyWithoutOneOfTheHosts(t *testing.T) {
	var commands []string
	reg := worldReg
	run := func(_ context.Context, _ time.Duration, name string, args ...string) (string, error) {
		commands = append(commands, strings.Join(append([]string{name}, args...), " "))
		if args[0] == "reg" {
			return reg, nil
		}
		return scanDump("-40.00 DE"), nil
	}
	if got := nearbyCountry(context.Background(), run, "wlan0"); got != "DE" || commands[1] != "iw dev wlan0 scan dump" {
		t.Fatalf("%q after %q", got, commands)
	}
	commands, reg = nil, "global\ncountry GB: DFS-ETSI\n"
	if got := nearbyCountry(context.Background(), run, "wlan0"); got != "" || len(commands) != 1 {
		t.Fatalf("overrode the host's GB with %q after %q", got, commands)
	}
}
