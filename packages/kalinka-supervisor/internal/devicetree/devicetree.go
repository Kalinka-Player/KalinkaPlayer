// Package devicetree reads the strings the firmware leaves in the kernel's
// device tree, such as the board model and a HAT's identity.
package devicetree

import (
	"io"
	"os"
	"path/filepath"
	"strings"
)

// maxProperty bounds a read; the strings this package serves are a few dozen bytes.
const maxProperty = 256

// Property returns the string property name under root's /proc/device-tree,
// without the NUL the device tree ends it with, and whether it exists.
func Property(root, name string) (string, bool) {
	f, err := os.Open(filepath.Join(root, "proc/device-tree", name))
	if err != nil {
		return "", false
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, maxProperty))
	if err != nil {
		return "", false
	}
	return strings.TrimSpace(strings.TrimRight(string(b), "\x00")), true
}

// Model names the board, such as "Raspberry Pi 4 Model B Rev 1.4", or "" where the firmware gives none.
func Model(root string) string {
	model, _ := Property(root, "model")
	return model
}
