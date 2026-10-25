package soundcard

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"kalinka/supervisor/internal/devicetree"
	"kalinka/supervisor/internal/devicetree/devicetreetest"
)

type box struct {
	t    *testing.T
	root string
}

func (b box) write(path, body string) {
	b.t.Helper()
	full := filepath.Join(b.root, path)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		b.t.Fatal(err)
	}
	if err := os.WriteFile(full, []byte(body), 0o644); err != nil {
		b.t.Fatal(err)
	}
}

// pi stages a Raspberry Pi of model with the images' firmware and onboard audio off.
func pi(t *testing.T, model string) box {
	b := box{t, t.TempDir()}
	b.write("proc/device-tree/model", model+"\x00")
	b.write(configTxt, "dtparam=audio=off\nenable_uart=1\n")
	for name := range shippedOverlays(t) {
		b.write(filepath.Join(overlays, name+".dtbo"), "")
	}
	return b
}

func (b box) hat(vendor, product string) {
	b.write("proc/device-tree/hat/vendor", vendor+"\x00")
	b.write("proc/device-tree/hat/product", product+"\x00")
	b.write("proc/device-tree/hat/product_id", "0x0003\x00")
	b.write("proc/device-tree/hat/product_ver", "0x0001\x00")
	b.write("proc/device-tree/hat/uuid", "6f7e6e2a-0c8b-4f0e-9d7a-2c1b8d2f3a4b\x00")
}

// hatMap stages the firmware's own table of HATs it sets up from their ID chips.
func (b box) hatMap(entries ...devicetree.Node) {
	b.write(hatMap, string(devicetreetest.Blob(entries...)))
}

// mapEntry is a table entry for the UUID box.hat stages.
func mapEntry(overlay string) devicetree.Node {
	return devicetree.Node{Name: "board", Props: map[string][]byte{
		"uuid":    devicetreetest.Cells(0x6f7e6e2a, 0x0c8b4f0e, 0x9d7a2c1b, 0x8d2f3a4b),
		"overlay": devicetreetest.String(overlay),
	}}
}

const (
	digiCard  = " 1 [sndrpihifiberry]: RPi-WM8804 - snd_rpi_hifiberry_digi\n                      snd_rpi_hifiberry_digi\n"
	hdmiCards = " 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0\n                      vc4-hdmi-0\n" +
		" 1 [vc4hdmi1       ]: vc4-hdmi - vc4-hdmi-1\n                      vc4-hdmi-1\n"
	hifiberryCard = " 2 [sndrpihifiberry]: RPi-simple - snd_rpi_hifiberry_dacplus\n                      snd_rpi_hifiberry_dacplus\n"
)

type fakeBus struct {
	registers map[[2]uint8]uint8
	closed    bool
}

func (f *fakeBus) ReadRegister(addr, reg uint8) (uint8, error) {
	if v, ok := f.registers[[2]uint8{addr, reg}]; ok {
		return v, nil
	}
	return 0, errors.New("no answer")
}

func (f *fakeBus) Close() error {
	f.closed = true
	return nil
}

func opens(bus *fakeBus, opened *int) func() (Bus, error) {
	return func() (Bus, error) {
		*opened++
		return bus, nil
	}
}

func noBus() (Bus, error) { return nil, os.ErrNotExist }

func names(cards []Card) []string {
	var found []string
	for _, c := range cards {
		found = append(found, c.Name)
	}
	return found
}

func TestNotAPi(t *testing.T) {
	b := box{t, t.TempDir()}
	b.write(configTxt, "dtparam=audio=off\n")
	d := New(b.root, noBus)
	r := d.Report()
	if d.Supported() || r.Supported || len(r.Options) != 0 || r.Suggested == nil || r.Options == nil || r.USB == nil || r.Name != nil {
		t.Fatalf("report off a Pi = %+v", r)
	}
	if d.Offers(None) {
		t.Fatal("a box that is no Pi offers a card")
	}
	b.write("proc/device-tree/model", "Raspberry Pi 4 Model B Rev 1.4\x00")
	if err := os.Remove(filepath.Join(b.root, configTxt)); err != nil {
		t.Fatal(err)
	}
	if New(b.root, noBus).Supported() {
		t.Fatal("a Pi without a boot configuration to edit is supported")
	}
}

func TestOptions(t *testing.T) {
	pi4 := New(pi(t, "Raspberry Pi 4 Model B Rev 1.4").root, noBus).Options()
	if len(pi4) != len(Catalog) {
		t.Fatalf("a Pi 4 with the images' firmware offers %v", names(pi4))
	}
	for _, model := range []string{"Raspberry Pi 5 Model B Rev 1.0", "Raspberry Pi Zero 2 W Rev 1.0", "Raspberry Pi 400 Rev 1.0", "Raspberry Pi Compute Module 4 Rev 1.0"} {
		got := names(New(pi(t, model).root, noBus).Options())
		if slices.Contains(got, HeadphoneSocket) || !slices.Contains(got, HDMI) {
			t.Errorf("%s offers %v", model, got)
		}
	}
	b := pi(t, "Raspberry Pi 3 Model B Plus Rev 1.3")
	if err := os.Remove(filepath.Join(b.root, overlays, "i2s-dac.dtbo")); err != nil {
		t.Fatal(err)
	}
	d := New(b.root, noBus)
	if d.Offers("i2s-dac") || !d.Offers("hifiberry-digi") || !d.Offers(None) || d.Offers("format-disk") {
		t.Fatalf("offers on older firmware: %v", names(d.Options()))
	}
}

func TestReportForAHATWithNothingConfigured(t *testing.T) {
	b := pi(t, "Raspberry Pi 4 Model B Rev 1.4")
	b.write("proc/asound/cards", hdmiCards)
	b.hat("HiFiBerry", "Digi2 Pro")
	r := New(b.root, noBus).Report()
	if r.HAT == nil || r.HAT.Product != "Digi2 Pro" || r.HAT.ProductID != "0x0003" || r.HAT.UUID == "" {
		t.Fatalf("hat = %+v", r.HAT)
	}
	if r.Configured != nil || r.ConfiguredBy != nil || r.Active != nil || !slices.Equal(r.Suggested, []string{"hifiberry-digi-pro"}) {
		t.Fatalf("report = %+v", r)
	}
	if r.Name == nil || *r.Name != "HiFiBerry Digi2 Pro" {
		t.Fatalf("name = %v", r.Name)
	}
}

func TestReportForAConfiguredHAT(t *testing.T) {
	b := pi(t, "Raspberry Pi 4 Model B Rev 1.4")
	b.write(configTxt, "#dtoverlay=hifiberry-digi\ndtparam=audio=off\n[all]\ndtoverlay=vc4-kms-v3d,noaudio\ndtoverlay=hifiberry-dacplus-std,slave\r\n")
	b.write("proc/asound/cards", hdmiCards+hifiberryCard)
	opened := 0
	r := New(b.root, opens(&fakeBus{}, &opened)).Report()
	if r.Configured == nil || *r.Configured != "hifiberry-dacplus-std" || r.ConfiguredBy == nil || *r.ConfiguredBy != ByBootConfig {
		t.Fatalf("configured = %v by %v", r.Configured, r.ConfiguredBy)
	}
	if r.Active == nil || *r.Active != (Active{ID: "sndrpihifiberry", Name: "snd_rpi_hifiberry_dacplus", PCM: "hw:CARD=sndrpihifiberry,DEV=0"}) {
		t.Fatalf("active = %+v", r.Active)
	}
	if r.Name == nil || *r.Name != "HiFiBerry DAC+ Standard, Amp2 or Amp4" {
		t.Fatalf("name = %v", r.Name)
	}
	if len(r.Suggested) != 0 || opened != 0 {
		t.Fatalf("a working card sent detection to the bus: %v, %d opens", r.Suggested, opened)
	}
}

func TestReportForAHATTheFirmwareSetsUp(t *testing.T) {
	b := pi(t, "Raspberry Pi 4 Model B Rev 1.5")
	b.hat("HiFiBerry", "Digi2 Pro")
	b.hatMap(mapEntry("hifiberry-digi-pro"))
	b.write("proc/asound/cards", hdmiCards+digiCard)
	opened := 0
	d := New(b.root, opens(&fakeBus{}, &opened))
	r := d.Report()
	if r.Configured == nil || *r.Configured != "hifiberry-digi-pro" || r.ConfiguredBy == nil || *r.ConfiguredBy != ByHAT {
		t.Fatalf("configured = %v by %v", r.Configured, r.ConfiguredBy)
	}
	if len(r.Suggested) != 0 || opened != 0 {
		t.Fatalf("a card the firmware set up was suggested again: %v, %d bus opens", r.Suggested, opened)
	}
	if r.Name == nil || *r.Name != "HiFiBerry Digi2 Pro" || r.Active == nil || r.Active.PCM != "hw:CARD=sndrpihifiberry,DEV=0" {
		t.Fatalf("report = %+v", r)
	}
	if got := d.HATCard(); got != "hifiberry-digi-pro" {
		t.Fatalf("HAT card = %q", got)
	}
	b.write(configTxt, "dtparam=audio=off\ndtoverlay=hifiberry-digi\n")
	if r := d.Report(); r.Configured == nil || *r.Configured != "hifiberry-digi" || *r.ConfiguredBy != ByBootConfig {
		t.Fatalf("an overlay in config.txt lost to the chip: %v", r.Configured)
	}
}

func TestATableTheFirmwareCannotFollowSetsNothingUp(t *testing.T) {
	b := pi(t, "Raspberry Pi 4 Model B Rev 1.5")
	b.hat("HiFiBerry", "DSP 2x4")
	b.write("proc/asound/cards", hdmiCards)
	for _, table := range []string{
		string(devicetreetest.Blob(mapEntry("hifiberry-dacplus-dsp"))),
		string(devicetreetest.Blob(mapEntry("hifiberry-dacplusdsp")))[:60],
		"",
	} {
		b.write(hatMap, table)
		d := New(b.root, noBus)
		r := d.Report()
		if r.Configured != nil || !slices.Equal(r.Suggested, []string{"hifiberry-dacplusdsp"}) || d.HATCard() != "" {
			t.Fatalf("report with table %q = %+v", table, r)
		}
	}
}

func TestATableEntryForAnotherBoardSetsUpNoSoundCard(t *testing.T) {
	b := pi(t, "Raspberry Pi 5 Model B Rev 1.0")
	b.hat("Hailo Technologies", "Raspberry Pi AI Hat, Model Hailo-8")
	b.hatMap(devicetree.Node{Name: "hailo-8", Props: map[string][]byte{
		"vendor":  devicetreetest.String("Hailo Technologies"),
		"product": devicetreetest.String("Raspberry Pi AI Hat, Model Hailo-8"),
		"overlay": devicetreetest.String("none,pciex1,pciex1_gen=3"),
	}})
	b.write("proc/asound/cards", hdmiCards)
	d := New(b.root, noBus)
	if r := d.Report(); r.Configured != nil || r.Name != nil || d.HATCard() != "" {
		t.Fatalf("report = %+v", r)
	}
}

func TestHATMapMatching(t *testing.T) {
	hat := &HAT{Vendor: "Raspberry Pi", Product: "Sense HAT", ProductVer: "0x0002", UUID: "6f7e6e2a-0c8b-4f0e-9d7a-2c1b8d2f3a4b"}
	byBoard := func(props map[string][]byte) devicetree.Node {
		props["vendor"], props["product"] = devicetreetest.String("Raspberry Pi"), devicetreetest.String("Sense HAT")
		return devicetree.Node{Name: "sensehat", Props: props}
	}
	for _, tc := range []struct {
		name  string
		entry devicetree.Node
		hat   HAT
		want  bool
	}{
		{"its UUID", mapEntry("x"), *hat, true},
		{"its UUID in capitals", mapEntry("x"), HAT{UUID: strings.ToUpper(hat.UUID)}, true},
		{"another UUID", mapEntry("x"), HAT{Vendor: hat.Vendor, Product: hat.Product, UUID: "00000000-0000-0000-0000-000000000000"}, false},
		{"its vendor and product", byBoard(map[string][]byte{}), *hat, true},
		{"its version", byBoard(map[string][]byte{"pver": devicetreetest.Cells(2)}), *hat, true},
		{"another version", byBoard(map[string][]byte{"pver": devicetreetest.Cells(1)}), *hat, false},
		{"a version the chip does not give", byBoard(map[string][]byte{"pver": devicetreetest.Cells(2)}), HAT{Vendor: hat.Vendor, Product: hat.Product}, false},
		{"another product", byBoard(map[string][]byte{}), HAT{Vendor: hat.Vendor, Product: "Sense HAT v3"}, false},
		{"neither", devicetree.Node{Props: map[string][]byte{"overlay": devicetreetest.String("x")}}, *hat, false},
	} {
		if got := mapped(tc.entry, &tc.hat); got != tc.want {
			t.Errorf("%s: matched %v", tc.name, got)
		}
	}
}

func TestReportForASelfConfiguringHAT(t *testing.T) {
	b := pi(t, "Raspberry Pi 5 Model B Rev 1.0")
	b.hat("Raspberry Pi Ltd.", "Raspberry Pi DAC Pro")
	b.write("proc/asound/cards", " 0 [DACPro         ]: RPi-simple - RPi DAC Pro\n                      RPi DAC Pro\n")
	opened := 0
	r := New(b.root, opens(&fakeBus{}, &opened)).Report()
	if r.Name == nil || *r.Name != "Raspberry Pi DAC Pro" || r.Active == nil || r.Active.PCM != "hw:CARD=DACPro,DEV=0" {
		t.Fatalf("report = %+v", r)
	}
	if len(r.Suggested) != 0 || opened != 0 {
		t.Fatalf("a board the firmware set up was second-guessed: %v", r.Suggested)
	}
	b.write("proc/asound/cards", "")
	if r := New(b.root, opens(&fakeBus{}, &opened)).Report(); len(r.Suggested) != 0 || opened != 0 {
		t.Fatalf("a self-configuring board that failed to come up sent detection to the bus: %+v", r)
	}
}

func TestAnotherHATDoesNotNameTheSoundCard(t *testing.T) {
	b := pi(t, "Raspberry Pi 5 Model B Rev 1.0")
	b.hat("Raspberry Pi Ltd.", "Raspberry Pi M.2 HAT+")
	b.write("proc/asound/cards", hdmiCards)
	if r := New(b.root, noBus).Report(); r.Name != nil {
		t.Fatalf("name = %q", *r.Name)
	}
	b.write(configTxt, "dtparam=audio=off\ndtoverlay=hifiberry-dacplus-std\n")
	b.write("proc/asound/cards", hdmiCards+hifiberryCard)
	if r := New(b.root, noBus).Report(); r.Name == nil || *r.Name != "HiFiBerry DAC+ Standard, Amp2 or Amp4" {
		t.Fatalf("name = %v", r.Name)
	}
}

func TestReportProbesTheBusOnce(t *testing.T) {
	b := pi(t, "Raspberry Pi 4 Model B Rev 1.4")
	b.write("proc/asound/cards", hdmiCards)
	bus := &fakeBus{registers: map[[2]uint8]uint8{{0x4d, 40}: 0x02, {0x3b, 1}: 0x01}}
	opened := 0
	d := New(b.root, opens(bus, &opened))
	want := []string{"hifiberry-dacplus-std", "hifiberry-dacplus-pro", "allo-boss-dac-pcm512x-audio", "justboom-dac"}
	for range 3 {
		if r := d.Report(); !slices.Equal(r.Suggested, want) || r.Name != nil {
			t.Fatalf("report = %+v", r)
		}
	}
	if opened != 1 || !bus.closed {
		t.Fatalf("bus opened %d times, closed %v", opened, bus.closed)
	}
}

func TestReportTriesTheBusAgainUntilItOpens(t *testing.T) {
	b := pi(t, "Raspberry Pi 4 Model B Rev 1.4")
	b.write("proc/asound/cards", hdmiCards)
	bus := &fakeBus{registers: map[[2]uint8]uint8{{0x3b, 1}: 0x88}}
	opened, missing := 0, true
	d := New(b.root, func() (Bus, error) {
		opened++
		if missing {
			return nil, os.ErrNotExist
		}
		return bus, nil
	})
	if r := d.Report(); len(r.Suggested) != 0 {
		t.Fatalf("suggested without a bus: %v", r.Suggested)
	}
	missing = false
	for range 2 {
		if r := d.Report(); len(r.Suggested) == 0 || r.Suggested[0] != "hifiberry-digi" {
			t.Fatalf("suggested once the bus appeared: %v", r.Suggested)
		}
	}
	if opened != 2 {
		t.Fatalf("bus opened %d times", opened)
	}
}

func TestSuggestionsFollowWhatTheBoxOffers(t *testing.T) {
	b := pi(t, "Raspberry Pi 4 Model B Rev 1.4")
	if err := os.Remove(filepath.Join(b.root, overlays, "hifiberry-dacplus-pro.dtbo")); err != nil {
		t.Fatal(err)
	}
	bus := &fakeBus{registers: map[[2]uint8]uint8{{0x4a, 25}: 0x07, {0x4d, 40}: 0x02}}
	got := New(b.root, opens(bus, new(int))).Report().Suggested
	want := []string{"hifiberry-dacplusadcpro", "hifiberry-dacplus-std", "allo-boss-dac-pcm512x-audio", "justboom-dac"}
	if !slices.Equal(got, want) {
		t.Fatalf("suggested = %v", got)
	}
}

func TestOnboardChoices(t *testing.T) {
	b := pi(t, "Raspberry Pi 4 Model B Rev 1.4")
	b.write(dietPiTxt, "AUTO_SETUP_NET_HOSTNAME=kalinka\nCONFIG_SOUNDCARD=rpi-bcm2835-3.5mm\n")
	b.write("proc/asound/cards", " 0 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones\n                      bcm2835 Headphones at 0xfe00b840, irq 0\n"+hdmiCards)
	r := New(b.root, noBus).Report()
	if r.Configured == nil || *r.Configured != HeadphoneSocket || r.Active == nil || r.Active.PCM != "hw:CARD=Headphones,DEV=0" {
		t.Fatalf("headphone socket = %+v", r)
	}
	if r.Name == nil || *r.Name != "Raspberry Pi headphone socket" {
		t.Fatalf("name = %v", r.Name)
	}
	b.write(dietPiTxt, "CONFIG_SOUNDCARD=rpi-bcm2835-hdmi\n")
	if r := New(b.root, noBus).Report(); r.Active == nil || r.Active.PCM != "default:CARD=vc4hdmi0" {
		t.Fatalf("hdmi = %+v", r.Active)
	}
	b.write(dietPiTxt, "CONFIG_SOUNDCARD=hifiberry-digi\n")
	if r := New(b.root, noBus).Report(); r.Configured != nil {
		t.Fatalf("DietPi's setting outranked config.txt for a HAT: %v", *r.Configured)
	}
}

func TestOnboardChoicesWithoutDietPi(t *testing.T) {
	b := pi(t, "Raspberry Pi 5 Model B Rev 1.0")
	b.write(configTxt, "dtparam=audio=on\ndtoverlay=vc4-kms-v3d\n")
	b.write(choiceRecord, HDMI+"\n")
	b.write("proc/asound/cards", hdmiCards)
	r := New(b.root, noBus).Report()
	if r.Configured == nil || *r.Configured != HDMI || r.ConfiguredBy == nil || *r.ConfiguredBy != ByBootConfig {
		t.Fatalf("configured = %v by %v", r.Configured, r.ConfiguredBy)
	}
	if r.Active == nil || r.Active.PCM != "default:CARD=vc4hdmi0" {
		t.Fatalf("active = %+v", r.Active)
	}
	if r.Name == nil || *r.Name != "Raspberry Pi HDMI" {
		t.Fatalf("name = %v", r.Name)
	}
	b.write(choiceRecord, "hifiberry-digi\n")
	if r := New(b.root, noBus).Report(); r.Configured != nil {
		t.Fatalf("the helper's record outranked config.txt for a HAT: %v", *r.Configured)
	}
	b.write(choiceRecord, HeadphoneSocket+"\n")
	if r := New(b.root, noBus).Report(); r.Configured != nil {
		t.Fatalf("a Pi 5 read back a headphone socket it lacks: %v", *r.Configured)
	}
	b.write(choiceRecord, HDMI+"\n")
	b.write(configTxt, "dtparam=audio=on\ndtoverlay=vc4-kms-v3d\ndtparam=audio=off\n")
	if r := New(b.root, noBus).Report(); r.Configured != nil {
		t.Fatalf("the helper's record outranked a config.txt that switches the Pi's outputs off: %v", *r.Configured)
	}
	b.write(configTxt, "dtparam=audio=off\ndtparam=i2c_arm=on,audio=on\n")
	if r := New(b.root, noBus).Report(); r.Configured == nil || *r.Configured != HDMI {
		t.Fatalf("a config.txt that switches the outputs on among other parameters hid the record: %v", r.Configured)
	}
	b.write(configTxt, "dtparam=audio=on\ndtoverlay=vc4-kms-v3d\n")
	b.write(dietPiTxt, "CONFIG_SOUNDCARD=none\n")
	if r := New(b.root, noBus).Report(); r.Configured != nil {
		t.Fatalf("the helper's record outranked DietPi's setting: %v", *r.Configured)
	}
}

func TestHDMIPlaysThroughThePortWithAScreen(t *testing.T) {
	b := pi(t, "Raspberry Pi 5 Model B Rev 1.0")
	b.write(configTxt, "dtparam=audio=on\ndtoverlay=vc4-kms-v3d\n")
	b.write(choiceRecord, HDMI+"\n")
	b.write("proc/asound/cards", hdmiCards)
	b.write("sys/class/drm/card1-HDMI-A-1/status", "disconnected\n")
	b.write("sys/class/drm/card1-HDMI-A-2/status", "disconnected\n")
	if r := New(b.root, noBus).Report(); r.Active == nil || r.Active.PCM != "default:CARD=vc4hdmi0" {
		t.Fatalf("with nothing plugged in, active = %+v", r.Active)
	}
	b.write("sys/class/drm/card1-HDMI-A-2/status", "connected\n")
	if r := New(b.root, noBus).Report(); r.Active == nil || r.Active.PCM != "default:CARD=vc4hdmi1" || r.Active.Name != "vc4-hdmi-1" {
		t.Fatalf("with a screen on the second port, active = %+v", r.Active)
	}
}

func TestAHATFittedAfterAnOnboardChoice(t *testing.T) {
	dacPro := " 2 [DACPro         ]: RPi-simple - RPi DAC Pro\n                      RPi DAC Pro\n"
	mic := " 2 [sndrpii2scard  ]: RPi-simple - snd_rpi_i2s_card\n                      snd_rpi_i2s_card\n"
	for _, tc := range []struct {
		name, vendor, product, table, cards, configured, pcm string
		plays                                                bool
	}{
		{"one the firmware's table sets up", "HiFiBerry", "Digi2 Pro", "hifiberry-digi-pro", hdmiCards + digiCard, "hifiberry-digi-pro", "hw:CARD=sndrpihifiberry,DEV=0", true},
		{"one that sets itself up", "Raspberry Pi Ltd.", "Raspberry Pi DAC Pro", "", hdmiCards + dacPro, "", "hw:CARD=DACPro,DEV=0", true},
		{"another board's chip", "Raspberry Pi Ltd.", "Raspberry Pi M.2 HAT+", "", hdmiCards, HDMI, "default:CARD=vc4hdmi0", false},
		{"a microphone with no playback", "Raspberry Pi Ltd.", "Raspberry Pi M.2 HAT+", "", hdmiCards + mic, HDMI, "default:CARD=vc4hdmi0", false},
	} {
		b := pi(t, "Raspberry Pi 5 Model B Rev 1.0")
		b.write(configTxt, "dtparam=audio=on\ndtoverlay=vc4-kms-v3d\n")
		b.write(choiceRecord, HDMI+"\n")
		b.hat(tc.vendor, tc.product)
		if tc.table != "" {
			b.hatMap(mapEntry(tc.table))
		}
		b.write("proc/asound/cards", tc.cards)
		if tc.plays {
			b.write("proc/asound/card2/pcm0p/info", "")
		}
		r := New(b.root, noBus).Report()
		got := ""
		if r.Configured != nil {
			got = *r.Configured
		}
		if got != tc.configured || r.Active == nil || r.Active.PCM != tc.pcm {
			t.Errorf("%s: configured %q, active %+v", tc.name, got, r.Active)
		}
	}
}

func TestParseCards(t *testing.T) {
	cards := parseCards(strings.NewReader(hdmiCards + hifiberryCard +
		" 3 [Loopback       ]: Loopback - Loopback\n                      Loopback 1\n" +
		"--- no soundcards ---\n 4 [broken\n"))
	if len(cards) != 4 {
		t.Fatalf("cards = %+v", cards)
	}
	if c := cards[2]; c.index != 2 || c.id != "sndrpihifiberry" || c.driver != "RPi-simple" || c.name != "snd_rpi_hifiberry_dacplus" || !c.hat() {
		t.Fatalf("hifiberry = %+v", c)
	}
	if cards[0].hat() || !cards[0].hdmi() || cards[3].hat() {
		t.Fatalf("onboard or virtual cards taken for a HAT: %+v", cards)
	}
}

func TestUSBCardsAreNoHAT(t *testing.T) {
	b := pi(t, "Raspberry Pi 4 Model B Rev 1.4")
	b.write("proc/asound/cards", " 0 [D50s           ]: USB-Audio - Topping D50s\n                      Topping D50s at usb-0000:01:00.0-1.3, high speed\n"+
		" 1 [C920           ]: USB-Audio - HD Pro Webcam C920\n                      HD Pro Webcam C920 at usb-0000:01:00.0-1.4, high speed\n")
	b.write("proc/asound/card0/usbid", "152a:8750\n")
	b.write("proc/asound/card0/pcm0p/info", "")
	b.write("proc/asound/card1/usbid", "046d:082d\n")
	b.write("proc/asound/card1/pcm0c/info", "")
	r := New(b.root, noBus).Report()
	if r.Active != nil {
		t.Fatalf("a USB DAC was taken for a HAT: %+v", r.Active)
	}
	if !slices.Equal(r.USB, []Active{{ID: "D50s", Name: "Topping D50s", PCM: "hw:CARD=D50s,DEV=0"}}) || r.Name != nil {
		t.Fatalf("usb = %+v, name = %v", r.USB, r.Name)
	}
}

func TestHATName(t *testing.T) {
	for _, tc := range []struct{ vendor, product, want string }{
		{"HiFiBerry", "DAC+ ADC Pro", "HiFiBerry DAC+ ADC Pro"},
		{"Raspberry Pi Ltd.", "Raspberry Pi DAC Pro", "Raspberry Pi DAC Pro"},
		{"IQaudIO Limited.", "Pi-DigiAMP+", "IQaudIO Pi-DigiAMP+"},
		{"", "Mystery DAC", "Mystery DAC"},
	} {
		if got := (&HAT{Vendor: tc.vendor, Product: tc.product}).Name(); got != tc.want {
			t.Errorf("%q %q = %q", tc.vendor, tc.product, got)
		}
	}
}

func TestHiFiBerryProductsNameCataloguedCards(t *testing.T) {
	for product, card := range hifiberryProducts {
		if c, ok := Lookup(card); !ok || !c.HAT() {
			t.Errorf("%s maps to %s, which is not catalogued", product, card)
		}
		if got := (&HAT{Vendor: "HiFiBerry", Product: product}).suggestion(); got != card {
			t.Errorf("%s suggests %q", product, got)
		}
	}
	if got := (&HAT{Vendor: "Allo", Product: "Amp3"}).suggestion(); got != "" {
		t.Errorf("another maker's product was read as HiFiBerry's: %q", got)
	}
}

func TestProbesNameCataloguedCards(t *testing.T) {
	for _, p := range probes {
		for _, card := range p.cards {
			if c, ok := Lookup(card); !ok || !c.HAT() {
				t.Errorf("probe 0x%02x names %s, which is not catalogued", p.addr, card)
			}
		}
	}
}
