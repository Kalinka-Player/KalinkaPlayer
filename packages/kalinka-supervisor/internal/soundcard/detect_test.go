package soundcard

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
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

const (
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
	if d.Supported() || r.Supported || len(r.Options) != 0 || r.Suggested == nil || r.Options == nil || r.Name != nil {
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
	if r.Configured != nil || r.Active != nil || !slices.Equal(r.Suggested, []string{"hifiberry-digi-pro"}) {
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
	if r.Configured == nil || *r.Configured != "hifiberry-dacplus-std" {
		t.Fatalf("configured = %v", r.Configured)
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
	if r := New(b.root, noBus).Report(); r.Active == nil || r.Active.PCM != "hw:CARD=vc4hdmi0,DEV=0" {
		t.Fatalf("hdmi = %+v", r.Active)
	}
	b.write(dietPiTxt, "CONFIG_SOUNDCARD=hifiberry-digi\n")
	if r := New(b.root, noBus).Report(); r.Configured != nil {
		t.Fatalf("DietPi's setting outranked config.txt for a HAT: %v", *r.Configured)
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
	b.write("proc/asound/cards", " 0 [D50s           ]: USB-Audio - Topping D50s\n                      Topping D50s at usb-0000:01:00.0-1.3, high speed\n")
	b.write("proc/asound/card0/usbid", "152a:8750\n")
	if r := New(b.root, noBus).Report(); r.Active != nil {
		t.Fatalf("a USB DAC was taken for a HAT: %+v", r.Active)
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
