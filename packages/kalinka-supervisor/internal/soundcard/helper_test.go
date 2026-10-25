package soundcard

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// The packaged helper must set up every catalogued card so that detection
// reads the same card back, the Pi's own outputs included, on Raspberry Pi OS,
// on the config.txt Raspberry Pi's own packages assemble, and where DietPi's
// dietpi.txt records the choice.
func TestHelperSetsUpEveryCard(t *testing.T) {
	for _, tool := range []string{"bash", "flock", "sha256sum", "sed"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("the helper needs %s", tool)
		}
	}
	helper, err := filepath.Abs("../../soundcard.sh")
	if err != nil {
		t.Fatal(err)
	}
	bin := t.TempDir()
	if err := os.WriteFile(filepath.Join(bin, "sync"), []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	assembled, err := os.ReadFile("testdata/raspberrypi-trixie-config.txt")
	if err != nil {
		t.Fatal(err)
	}
	minimal := "dtparam=audio=off\nenable_uart=1\n"
	setups := []struct {
		name, config string
		dietPi       bool
	}{{"Raspberry Pi OS", minimal, false}, {"DietPi", minimal, true}, {"rpi-image-gen", string(assembled), false}}
	for _, c := range Catalog {
		for _, setup := range setups {
			dietPi, name := setup.dietPi, setup.name
			b := box{t, t.TempDir()}
			b.write("proc/device-tree/model", "Raspberry Pi 4 Model B Rev 1.4\x00")
			b.write(configTxt, setup.config)
			if dietPi {
				b.write(dietPiTxt, "CONFIG_SOUNDCARD=none\n")
			}
			cmd := exec.Command("bash", helper, "apply", c.Name)
			cmd.Env = append(os.Environ(), "KALINKA_SOUNDCARD_ROOT="+b.root, "PATH="+bin+":"+os.Getenv("PATH"))
			out, err := cmd.Output()
			if err != nil {
				t.Errorf("%s (%s): %v", c.Name, name, err)
				continue
			}
			if verdict := strings.TrimSpace(string(out)); verdict != "changed" && verdict != "unchanged" {
				t.Errorf("%s (%s): verdict %q", c.Name, name, verdict)
			}
			want := ""
			if c.HAT() || c.Kind == Onboard {
				want = c.Name
			}
			got := ""
			if r := New(b.root, noBus).Report(); r.Configured != nil {
				got = *r.Configured
			}
			if got != want {
				t.Errorf("%s (%s) reads back as %q", c.Name, name, got)
			}
			written, err := os.ReadFile(filepath.Join(b.root, configTxt))
			if err != nil {
				t.Fatal(err)
			}
			if c.HAT() && !forEveryBoard(string(written), "dtoverlay="+c.Name) {
				t.Errorf("%s (%s) went under one board's section", c.Name, name)
			}
		}
	}
}

// forEveryBoard reports whether line is in config outside any section that limits it to some boards.
func forEveryBoard(config, line string) bool {
	section := "[all]"
	for _, l := range strings.Split(config, "\n") {
		l = strings.TrimSpace(l)
		if strings.HasPrefix(l, "[") {
			section = l
		} else if l == line {
			return section == "[all]"
		}
	}
	return false
}
