package soundcard

import (
	"bufio"
	"os"
	"path/filepath"
	"strings"

	"kalinka/supervisor/internal/devicetree"
)

const (
	configTxt = "boot/firmware/config.txt"
	overlays  = "boot/firmware/overlays"
	dietPiTxt = "boot/dietpi.txt"
	// choiceRecord is where the helper records the card it last applied.
	choiceRecord = "var/lib/kalinka-supervisor/soundcard"
)

// supported reports whether root is a Raspberry Pi whose boot configuration the helper can edit.
func supported(root string) bool {
	if !strings.HasPrefix(devicetree.Model(root), "Raspberry Pi") {
		return false
	}
	_, err := os.Stat(filepath.Join(root, configTxt))
	return err == nil
}

func overlayPresent(root, name string) bool {
	_, err := os.Stat(filepath.Join(root, overlays, name+".dtbo"))
	return err == nil
}

// boardsWithoutSocket are the models with no 3.5 mm output of their own.
var boardsWithoutSocket = []string{"Raspberry Pi 5", "Raspberry Pi 400", "Zero", "Compute Module"}

func hasHeadphoneSocket(model string) bool {
	for _, board := range boardsWithoutSocket {
		if strings.Contains(model, board) {
			return false
		}
	}
	return true
}

// configured names the catalogued card the boot configuration sets up, or "".
// The overlay in config.txt is what the firmware loads; DietPi's own setting
// only tells the Pi's two outputs apart, which config.txt switches on together.
func configured(root string) string {
	if name := configuredOverlay(filepath.Join(root, configTxt)); name != "" {
		return name
	}
	if name := dietPiSetting(filepath.Join(root, dietPiTxt), "CONFIG_SOUNDCARD"); name == HDMI || name == HeadphoneSocket {
		return name
	}
	return ""
}

// recordedOnboard names the Pi's own output the helper last applied, or "".
// Where dietpi.txt exists, DietPi's tools change the choice too, so its setting alone decides.
func recordedOnboard(root string) string {
	if _, err := os.Stat(filepath.Join(root, dietPiTxt)); err == nil {
		return ""
	}
	// Every onboard choice the helper applies switches the outputs on; off means config.txt was edited since.
	if !onboardAudioOn(filepath.Join(root, configTxt)) {
		return ""
	}
	b, err := os.ReadFile(filepath.Join(root, choiceRecord))
	if err != nil {
		return ""
	}
	switch name := strings.TrimSpace(string(b)); {
	case name == HDMI, name == HeadphoneSocket && hasHeadphoneSocket(devicetree.Model(root)):
		return name
	}
	return ""
}

// onboardAudioOn reads dtparam=audio as the firmware does: several to a line, bare is on, last wins, off unless set.
func onboardAudioOn(path string) bool {
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	on := false
	s := bufio.NewScanner(f)
	for s.Scan() {
		params, ok := strings.CutPrefix(strings.TrimSpace(s.Text()), "dtparam=")
		if !ok {
			continue
		}
		for _, param := range strings.Split(params, ",") {
			switch name, value, _ := strings.Cut(param, "="); {
			case name != "audio":
			case value == "", value == "on", value == "true", value == "yes", value == "okay":
				on = true
			default:
				on = false
			}
		}
	}
	return on
}

func configuredOverlay(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		value, ok := strings.CutPrefix(strings.TrimSpace(s.Text()), "dtoverlay=")
		if !ok {
			continue
		}
		name, _, _ := strings.Cut(value, ",")
		if card, ok := Lookup(strings.TrimSpace(name)); ok && card.HAT() {
			return card.Name
		}
	}
	return ""
}

// dietPiSetting reads key the way DietPi does: the first match wins, and a commented-out key is not set.
func dietPiSetting(path, key string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		line := strings.TrimLeft(strings.TrimSuffix(s.Text(), "\r"), " \t")
		if value, ok := strings.CutPrefix(line, key+"="); ok {
			return value
		}
	}
	return ""
}
