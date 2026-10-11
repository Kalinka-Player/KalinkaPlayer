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
