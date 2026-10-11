package soundcard

import (
	"bufio"
	"os"
	"testing"
)

// shippedOverlays lists the overlays in the DietPi Trixie images' firmware, which every HAT in the catalog must name.
func shippedOverlays(t *testing.T) map[string]bool {
	t.Helper()
	f, err := os.Open("testdata/dietpi-trixie-overlays.txt")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	found := map[string]bool{}
	s := bufio.NewScanner(f)
	for s.Scan() {
		found[s.Text()] = true
	}
	return found
}

func TestCatalog(t *testing.T) {
	shipped := shippedOverlays(t)
	seen := map[string]bool{}
	for _, c := range Catalog {
		if seen[c.Name] {
			t.Errorf("%s is catalogued twice", c.Name)
		}
		seen[c.Name] = true
		if !ValidName(c.Name) {
			t.Errorf("%s does not fit the name pattern", c.Name)
		}
		if c.Label == "" {
			t.Errorf("%s has no label", c.Name)
		}
		if c.HAT() && !shipped[c.Name] {
			t.Errorf("%s is no overlay the images' firmware carries", c.Name)
		}
		if c.HAT() && c.Vendor == "" {
			t.Errorf("%s names no maker", c.Name)
		}
		if got, ok := Lookup(c.Name); !ok || got != c {
			t.Errorf("Lookup(%s) = %+v %v", c.Name, got, ok)
		}
	}
	for _, name := range []string{HDMI, HeadphoneSocket, None} {
		if c, ok := Lookup(name); !ok || c.HAT() {
			t.Errorf("%s is not catalogued as a choice without an overlay", name)
		}
	}
	if _, ok := Lookup("hifiberry-dacplus"); ok {
		t.Error("an unlisted overlay was found")
	}
}

func TestValidName(t *testing.T) {
	for name, want := range map[string]bool{
		"hifiberry-digi": true, "rpi-bcm2835-3.5mm": true, "i2s-dac": true,
		"": false, "../config": false, "Hifiberry": false, "a b": false, "-x": false, "x;reboot": false,
		"a234567890123456789012345678901234567890123456789": false,
	} {
		if ValidName(name) != want {
			t.Errorf("ValidName(%q) = %v", name, !want)
		}
	}
}
