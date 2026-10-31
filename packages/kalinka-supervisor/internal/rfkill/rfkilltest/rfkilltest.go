// Package rfkilltest stages the kernel's /sys/class/rfkill for tests of what
// clears radio blocks.
package rfkilltest

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// Radio is one rfkill switch of a staged host.
type Radio struct {
	Type, Name string
	Soft, Hard bool
}

// Stage lists radios as rfkill0, rfkill1, … under a temporary root, which it returns.
func Stage(t testing.TB, radios ...Radio) string {
	t.Helper()
	root := t.TempDir()
	flag := map[bool]string{false: "0\n", true: "1\n"}
	for i, r := range radios {
		entry := Entry(root, "rfkill"+strconv.Itoa(i))
		if err := os.MkdirAll(entry, 0755); err != nil {
			t.Fatal(err)
		}
		for name, value := range map[string]string{"type": r.Type + "\n", "name": r.Name + "\n", "soft": flag[r.Soft], "hard": flag[r.Hard]} {
			if err := os.WriteFile(filepath.Join(entry, name), []byte(value), 0644); err != nil {
				t.Fatal(err)
			}
		}
	}
	return root
}

// Entry is the path of a staged switch, such as "rfkill0".
func Entry(root, name string) string { return filepath.Join(root, "sys/class/rfkill", name) }

// Soft reports whether a staged switch is soft-blocked.
func Soft(t testing.TB, root, name string) bool {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(Entry(root, name), "soft"))
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(string(b)) == "1"
}
