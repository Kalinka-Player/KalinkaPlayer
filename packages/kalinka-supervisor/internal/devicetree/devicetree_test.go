package devicetree

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func stage(t *testing.T, root, name, content string) {
	t.Helper()
	path := filepath.Join(root, "proc/device-tree", name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestProperty(t *testing.T) {
	root := t.TempDir()
	stage(t, root, "model", "Raspberry Pi 4 Model B Rev 1.4\x00")
	stage(t, root, "hat/product", "DAC+ ADC Pro\x00")
	stage(t, root, "hat/vendor", strings.Repeat("x", 4096))

	if got := Model(root); got != "Raspberry Pi 4 Model B Rev 1.4" {
		t.Fatalf("model = %q", got)
	}
	if got, ok := Property(root, "hat/product"); !ok || got != "DAC+ ADC Pro" {
		t.Fatalf("product = %q %v", got, ok)
	}
	if got, ok := Property(root, "hat/vendor"); !ok || len(got) != maxProperty {
		t.Fatalf("an oversized property was read whole: %d bytes", len(got))
	}
	if got, ok := Property(root, "hat/uuid"); ok || got != "" {
		t.Fatalf("missing property = %q %v", got, ok)
	}
	if got := Model(t.TempDir()); got != "" {
		t.Fatalf("model off a Pi = %q", got)
	}
}
