package wifi

import (
	"context"
	"maps"
	"math"
	"slices"
	"strconv"
	"strings"
	"time"

	"kalinka/supervisor/internal/protocol"
)

// nearbyCountry is the country nearby access points announce, for a host that has none of its own; empty otherwise.
func nearbyCountry(ctx context.Context, run Runner, iface string) string {
	reg, err := run(ctx, 5*time.Second, "iw", "reg", "get")
	if err != nil || globalCountry(reg) != "00" {
		return ""
	}
	dump, err := run(ctx, 5*time.Second, "iw", "dev", iface, "scan", "dump")
	if err != nil {
		return ""
	}
	return announcedCountry(dump)
}

func globalCountry(reg string) string {
	for _, line := range strings.Split(reg, "\n") {
		if code, ok := strings.CutPrefix(line, "country "); ok {
			code, _, _ = strings.Cut(code, ":")
			return code
		}
	}
	return ""
}

// announcedCountry takes the country most access points in an iw scan dump announce, the loudest one's on a tie.
func announcedCountry(dump string) string {
	votes := map[string]int{}
	loudest := map[string]int{}
	signal := math.MinInt
	for _, line := range strings.Split(dump, "\n") {
		fields := strings.Fields(line)
		switch {
		case strings.HasPrefix(line, "BSS "):
			signal = math.MinInt
		case len(fields) > 1 && fields[0] == "signal:":
			// iw prints -45.5 dBm as "-45.-50".
			whole, _, _ := strings.Cut(fields[1], ".")
			if dBm, err := strconv.Atoi(whole); err == nil {
				signal = dBm
			}
		case len(fields) > 1 && fields[0] == "Country:" && protocol.CountryPattern.MatchString(fields[1]):
			code := fields[1]
			votes[code]++
			if heard, ok := loudest[code]; !ok || signal > heard {
				loudest[code] = signal
			}
		}
	}
	best := ""
	for _, code := range slices.Sorted(maps.Keys(votes)) {
		if best == "" || votes[code] > votes[best] || votes[code] == votes[best] && loudest[code] > loudest[best] {
			best = code
		}
	}
	return best
}
