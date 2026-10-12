package soundcard

import (
	"slices"
	"sync"

	"kalinka/supervisor/internal/devicetree"
)

// Report is what the box knows about its sound card. A fact it cannot
// establish is null; the lists are empty, never null.
type Report struct {
	// Supported is false off a Raspberry Pi, where no card can be chosen.
	Supported bool `json:"supported"`
	// Name is the DAC as a person would call it, from the sound card's ID chip, the configured choice or ALSA, in that order.
	Name       *string `json:"name"`
	HAT        *HAT    `json:"hat"`
	Configured *string `json:"configured"`
	// ConfiguredBy says what sets the configured card up; null when nothing is configured.
	ConfiguredBy *Setup  `json:"configured_by"`
	Active       *Active `json:"active"`
	// USB lists the USB audio cards plugged in, which play with nothing set up.
	USB       []Active `json:"usb"`
	Suggested []string `json:"suggested"`
	Options   []Card   `json:"options"`
}

// Setup is what sets a card up: the boot configuration, or the firmware from the HAT's ID chip.
type Setup string

const (
	ByBootConfig Setup = "boot_config"
	ByHAT        Setup = "hat"
)

// Detector reads a box for the sound card it has and the ones it could be set
// up for. Safe for concurrent use.
type Detector struct {
	root    string
	openBus func() (Bus, error)

	probeMu sync.Mutex
	probed  []string
	read    bool
}

// New reads the box under root ("/" in production); openBus reaches the HAT's I2C bus.
func New(root string, openBus func() (Bus, error)) *Detector {
	return &Detector{root: root, openBus: openBus}
}

func (d *Detector) Supported() bool { return supported(d.root) }

// Options lists the catalogued cards this box can be set up for: those whose overlay its firmware carries.
func (d *Detector) Options() []Card {
	if !d.Supported() {
		return []Card{}
	}
	return d.options()
}

func (d *Detector) options() []Card {
	found := []Card{}
	model := devicetree.Model(d.root)
	for _, c := range Catalog {
		if c.HAT() && !overlayPresent(d.root, c.Name) || c.Name == HeadphoneSocket && !hasHeadphoneSocket(model) {
			continue
		}
		found = append(found, c)
	}
	return found
}

func (d *Detector) Offers(name string) bool {
	return slices.ContainsFunc(d.Options(), func(c Card) bool { return c.Name == name })
}

// HATCard names the card the firmware sets up from the fitted HAT's ID chip, or "".
func (d *Detector) HATCard() string {
	if hat := readHAT(d.root); hat != nil {
		return hat.card
	}
	return ""
}

func (d *Detector) Report() Report {
	r := Report{USB: []Active{}, Suggested: []string{}, Options: []Card{}}
	if r.Supported = d.Supported(); !r.Supported {
		return r
	}
	r.Options = d.options()
	r.HAT = readHAT(d.root)
	configured, by := configured(d.root), ByBootConfig
	if configured == "" && r.HAT != nil && r.HAT.card != "" {
		configured, by = r.HAT.card, ByHAT
	}
	if configured != "" {
		r.Configured, r.ConfiguredBy = &configured, &by
	}
	cards := readCards(d.root)
	r.Active = activeCard(cards, configured)
	r.USB = usbCards(cards)
	r.Suggested = offered(d.guesses(r.HAT, r.Active), r.Options)
	r.Name = name(r.HAT, configured, r.Active)
	return r
}

// guesses names the cards the hardware points to. The I2C bus is only read
// when no card came up and no ID chip settled it, and only until it has been
// read once: a HAT cannot change without a restart, but the bus may appear
// after the supervisor starts.
func (d *Detector) guesses(hat *HAT, active *Active) []string {
	if hat != nil {
		if hat.card != "" || hat.selfConfiguring() {
			return nil
		}
		if s := hat.suggestion(); s != "" {
			return []string{s}
		}
	}
	if active != nil {
		return nil
	}
	d.probeMu.Lock()
	defer d.probeMu.Unlock()
	if !d.read {
		d.probed, d.read = probeBus(d.openBus)
	}
	return d.probed
}

// offered keeps the guesses this box can be set up for, once each, in the order they were made.
func offered(guesses []string, options []Card) []string {
	found := []string{}
	for _, g := range guesses {
		if !slices.Contains(found, g) && slices.ContainsFunc(options, func(c Card) bool { return c.Name == g }) {
			found = append(found, g)
		}
	}
	return found
}

func name(hat *HAT, configured string, active *Active) *string {
	var n string
	switch card, known := Lookup(configured); {
	// Other boards carry ID chips too, such as a PoE or M.2 HAT; only one that is the sound card names it.
	case hat != nil && (hat.card != "" || hat.suggestion() != "" || configured == "" && active != nil):
		n = hat.Name()
	case known:
		n = card.Label
	case active != nil:
		n = active.Name
	default:
		return nil
	}
	return &n
}
