// Package soundcard knows the sound cards a Raspberry Pi box can be set up
// for, finds the one fitted, and applies a choice through the packaged
// helper, which runs outside the supervisor's sandbox.
package soundcard

import (
	"regexp"
	"slices"
)

// Kind groups cards by what they connect to.
type Kind string

const (
	DAC     Kind = "dac"
	Digital Kind = "digital"
	Amp     Kind = "amp"
	Onboard Kind = "onboard"
	NoHAT   Kind = "none"
)

// Card is one choice a box can be set up for. A HAT's Name is also the
// device-tree overlay the firmware loads for it; the other names are the ones
// DietPi's soundcard tool takes.
type Card struct {
	Name   string `json:"name"`
	Label  string `json:"label"`
	Vendor string `json:"vendor"`
	Kind   Kind   `json:"kind"`
}

// HAT reports whether the card is a board on the GPIO header, switched on by its overlay.
func (c Card) HAT() bool { return c.Kind != Onboard && c.Kind != NoHAT }

const (
	HDMI            = "rpi-bcm2835-hdmi"
	HeadphoneSocket = "rpi-bcm2835-3.5mm"
	None            = "none"
)

// Catalog lists every card the box can be set up for, in the order a picker shows them.
// Choices that fetch firmware or plugins from the internet are left out:
// setting a card up must work offline.
var Catalog = []Card{
	{Name: "hifiberry-dacplus-std", Label: "HiFiBerry DAC+ Standard, Amp2 or Amp4", Vendor: "HiFiBerry", Kind: DAC},
	{Name: "hifiberry-dacplus-pro", Label: "HiFiBerry DAC+ Pro or DAC2 Pro", Vendor: "HiFiBerry", Kind: DAC},
	{Name: "hifiberry-dac", Label: "HiFiBerry DAC+ Light, DAC+ Zero or MiniAmp", Vendor: "HiFiBerry", Kind: DAC},
	{Name: "hifiberry-dacplushd", Label: "HiFiBerry DAC2 HD", Vendor: "HiFiBerry", Kind: DAC},
	{Name: "hifiberry-dacplusadc", Label: "HiFiBerry DAC+ ADC", Vendor: "HiFiBerry", Kind: DAC},
	{Name: "hifiberry-dacplusadcpro", Label: "HiFiBerry DAC+ ADC Pro or DAC2 ADC Pro", Vendor: "HiFiBerry", Kind: DAC},
	{Name: "hifiberry-dacplusdsp", Label: "HiFiBerry DAC+ DSP or DSP 2x4", Vendor: "HiFiBerry", Kind: DAC},
	{Name: "hifiberry-dac8x", Label: "HiFiBerry DAC8x", Vendor: "HiFiBerry", Kind: DAC},
	{Name: "hifiberry-digi", Label: "HiFiBerry Digi+ or Digi2 Standard", Vendor: "HiFiBerry", Kind: Digital},
	{Name: "hifiberry-digi-pro", Label: "HiFiBerry Digi+ Pro or Digi2 Pro", Vendor: "HiFiBerry", Kind: Digital},
	{Name: "hifiberry-amp", Label: "HiFiBerry Amp+", Vendor: "HiFiBerry", Kind: Amp},
	{Name: "hifiberry-amp3", Label: "HiFiBerry Amp3", Vendor: "HiFiBerry", Kind: Amp},
	{Name: "hifiberry-amp4pro", Label: "HiFiBerry Amp4 Pro", Vendor: "HiFiBerry", Kind: Amp},
	{Name: "hifiberry-amp100", Label: "HiFiBerry Amp100", Vendor: "HiFiBerry", Kind: Amp},
	{Name: "rpi-dacplus", Label: "Raspberry Pi DAC+", Vendor: "Raspberry Pi", Kind: DAC},
	{Name: "rpi-dacpro", Label: "Raspberry Pi DAC Pro", Vendor: "Raspberry Pi", Kind: DAC},
	{Name: "rpi-digiampplus", Label: "Raspberry Pi DigiAMP+", Vendor: "Raspberry Pi", Kind: Amp},
	{Name: "rpi-codeczero", Label: "Raspberry Pi Codec Zero", Vendor: "Raspberry Pi", Kind: DAC},
	{Name: "iqaudio-dacplus", Label: "IQaudIO Pi-DAC+, Pi-DAC Pro or Pi-DAC Zero", Vendor: "IQaudIO", Kind: DAC},
	{Name: "iqaudio-dac", Label: "IQaudIO Pi-DAC", Vendor: "IQaudIO", Kind: DAC},
	{Name: "iqaudio-digi-wm8804-audio", Label: "IQaudIO Pi-Digi+", Vendor: "IQaudIO", Kind: Digital},
	{Name: "iqaudio-codec", Label: "IQaudIO Pi-Codec Zero", Vendor: "IQaudIO", Kind: DAC},
	{Name: "allo-boss-dac-pcm512x-audio", Label: "Allo Boss or Mini Boss", Vendor: "Allo", Kind: DAC},
	{Name: "allo-boss2-dac-audio", Label: "Allo Boss2", Vendor: "Allo", Kind: DAC},
	{Name: "allo-katana-dac-audio", Label: "Allo Katana", Vendor: "Allo", Kind: DAC},
	{Name: "allo-digione", Label: "Allo DigiOne", Vendor: "Allo", Kind: Digital},
	{Name: "justboom-dac", Label: "JustBoom DAC HAT, Amp HAT, DAC Zero or Amp Zero", Vendor: "JustBoom", Kind: DAC},
	{Name: "justboom-digi", Label: "JustBoom Digi HAT or Digi Zero", Vendor: "JustBoom", Kind: Digital},
	{Name: "i-sabre-q2m", Label: "Audiophonics I-Sabre Q2M", Vendor: "Audiophonics", Kind: DAC},
	{Name: "applepi-dac", Label: "Orchard Audio ApplePi-DAC", Vendor: "Orchard Audio", Kind: DAC},
	{Name: "dionaudio-loco", Label: "Dion Audio LOCO", Vendor: "Dion Audio", Kind: Amp},
	{Name: "dionaudio-loco-v2", Label: "Dion Audio LOCO V2", Vendor: "Dion Audio", Kind: Amp},
	{Name: "i2s-dac", Label: "Generic I2S DAC, such as a PCM5102A board", Vendor: "Generic", Kind: DAC},
	{Name: "i2s-master-dac", Label: "Generic I2S DAC that drives its own clock", Vendor: "Generic", Kind: DAC},
	{Name: HDMI, Label: "Raspberry Pi HDMI", Vendor: "Raspberry Pi", Kind: Onboard},
	{Name: HeadphoneSocket, Label: "Raspberry Pi headphone socket", Vendor: "Raspberry Pi", Kind: Onboard},
	{Name: None, Label: "No HAT: a USB DAC, or nothing", Kind: NoHAT},
}

// validName is the shape of every catalogued name; nothing else reaches a command line.
var validName = regexp.MustCompile(`^[a-z0-9][a-z0-9.-]{0,47}$`)

// ValidName reports whether name could be a catalogued card, before asking whether this box offers it.
func ValidName(name string) bool { return validName.MatchString(name) }

// Lookup returns the catalogued card called name.
func Lookup(name string) (Card, bool) {
	i := slices.IndexFunc(Catalog, func(c Card) bool { return c.Name == name })
	if i < 0 {
		return Card{}, false
	}
	return Catalog[i], true
}
