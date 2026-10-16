package wifi

import (
	"context"
	"encoding/hex"
	"strings"
	"time"

	"kalinka/supervisor/internal/protocol"
)

// ConnectedSSID reads the associated network without scanning or reading saved credentials.
func ConnectedSSID(ctx context.Context, iface string, run Runner) (string, error) {
	if !interfacePattern.MatchString(iface) {
		return "", protocol.Unavailable
	}
	output, err := run(ctx, time.Second, "iw", "dev", iface, "link")
	if err != nil {
		return "", err
	}
	if !strings.HasPrefix(output, "Connected to ") {
		return "", nil
	}
	match := ssidPattern.FindStringSubmatch(output)
	if match == nil {
		return "", nil
	}
	return decodeSSID(match[1]), nil
}

func decodeSSID(s string) string {
	var raw []byte
	for len(s) > 0 {
		if strings.HasPrefix(s, `\x`) {
			if len(s) < 4 {
				return ""
			}
			b, err := hex.DecodeString(s[2:4])
			if err != nil {
				return ""
			}
			raw = append(raw, b...)
			s = s[4:]
		} else {
			raw = append(raw, s[0])
			s = s[1:]
		}
	}
	if !protocol.ValidSSID(string(raw)) {
		return ""
	}
	return string(raw)
}
