// Package rfkill clears the kernel's soft radio blocks through sysfs.
package rfkill

import (
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
)

// Unblock clears the soft block on every radio of kind ("wlan", "bluetooth")
// listed under root's /sys/class/rfkill, and logs each one it clears and each
// one blocked by a hardware switch, which software cannot clear. It returns the
// names of both; a host without rfkill has neither. Writing needs root and a
// writable /sys.
func Unblock(root, kind string) (cleared, hard []string, err error) {
	entries, _ := filepath.Glob(filepath.Join(root, "sys/class/rfkill/rfkill*"))
	var errs []error
	for _, entry := range entries {
		if attribute(entry, "type") != kind {
			continue
		}
		name := attribute(entry, "name")
		if attribute(entry, "soft") == "1" {
			if err := os.WriteFile(filepath.Join(entry, "soft"), []byte("0"), 0644); err != nil {
				errs = append(errs, err)
			} else {
				slog.Info("Cleared a radio's soft block", "radio", name, "type", kind)
				cleared = append(cleared, name)
			}
		}
		if attribute(entry, "hard") == "1" {
			slog.Warn("Radio is blocked by a hardware switch", "radio", name, "type", kind)
			hard = append(hard, name)
		}
	}
	return cleared, hard, errors.Join(errs...)
}

// attribute is "" for an entry unplugged since the glob.
func attribute(entry, name string) string {
	b, _ := os.ReadFile(filepath.Join(entry, name))
	return strings.TrimSpace(string(b))
}
