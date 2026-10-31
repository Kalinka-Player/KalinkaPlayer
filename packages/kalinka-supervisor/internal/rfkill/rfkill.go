// Package rfkill clears explicitly authorized kernel soft blocks through sysfs.
package rfkill

import (
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
)

var ErrHardBlocked = errors.New("radio_hard_blocked")

type radioState struct {
	path, name string
	soft, hard bool
}

// states reads a named radio only. Read failures are not mistaken for an
// enabled radio; an entry removed during hotplug is skipped.
func states(root, kind, radio string) ([]radioState, error) {
	dir := filepath.Join(root, "sys/class/rfkill")
	entries, err := os.ReadDir(dir)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var result []radioState
	for _, entry := range entries {
		if !strings.HasPrefix(entry.Name(), "rfkill") {
			continue
		}
		path := filepath.Join(dir, entry.Name())
		attrs := make(map[string]string, 4)
		missing := false
		for _, key := range []string{"type", "name", "hard", "soft"} {
			b, err := os.ReadFile(filepath.Join(path, key))
			if os.IsNotExist(err) {
				missing = true
				break
			}
			if err != nil {
				return nil, err
			}
			attrs[key] = strings.TrimSpace(string(b))
			if key == "type" && attrs[key] != kind || key == "name" && attrs[key] != radio {
				break
			}
		}
		if missing || attrs["type"] != kind || attrs["name"] != radio {
			continue
		}
		if (attrs["soft"] != "0" && attrs["soft"] != "1") || (attrs["hard"] != "0" && attrs["hard"] != "1") {
			return nil, fmt.Errorf("invalid rfkill state for %s", radio)
		}
		result = append(result, radioState{path, radio, attrs["soft"] == "1", attrs["hard"] == "1"})
	}
	return result, nil
}

// CheckHardware reports a hardware block before a backend changes any switches.
func CheckHardware(root, kind, radio string) error {
	radios, err := states(root, kind, radio)
	if err != nil {
		return err
	}
	for _, r := range radios {
		if r.hard {
			slog.Warn("Radio is blocked by a hardware switch", "radio", r.name, "type", kind)
			return fmt.Errorf("%s: %w", r.name, ErrHardBlocked)
		}
	}
	return nil
}

// Unblock clears only the named radio's soft block. A hard block is reported
// without changing the soft block. A host without rfkill has nothing to clear.
func Unblock(root, kind, radio string) (cleared, hard []string, err error) {
	radios, err := states(root, kind, radio)
	if err != nil {
		return nil, nil, err
	}
	var errs []error
	for _, r := range radios {
		if r.hard {
			slog.Warn("Radio is blocked by a hardware switch", "radio", r.name, "type", kind)
			hard = append(hard, r.name)
			errs = append(errs, fmt.Errorf("%s: %w", r.name, ErrHardBlocked))
			continue
		}
		if !r.soft {
			continue
		}
		if err := os.WriteFile(filepath.Join(r.path, "soft"), []byte("0"), 0644); err != nil {
			errs = append(errs, err)
		} else {
			slog.Info("Cleared a radio's soft block", "radio", r.name, "type", kind)
			cleared = append(cleared, r.name)
		}
	}
	return cleared, hard, errors.Join(errs...)
}

// WiFiName maps an interface to its physical radio. Never fall back to all
// adapters if the selected interface disappeared.
func WiFiName(root, iface string) (string, error) {
	link, err := os.Readlink(filepath.Join(root, "sys/class/net", iface, "phy80211"))
	if err != nil {
		return "", err
	}
	return filepath.Base(link), nil
}
