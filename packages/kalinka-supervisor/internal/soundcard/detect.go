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
	// Name is the DAC as a person would call it, from its ID chip, the configured choice or ALSA, in that order.
	Name       *string  `json:"name"`
	HAT        *HAT     `json:"hat"`
	Configured *string  `json:"configured"`
	Active     *Active  `json:"active"`
	Suggested  []string `json:"suggested"`
	Options    []Card   `json:"options"`
}

// Detector reads a box for the sound card it has and the ones it could be set
// up for. Safe for concurrent use.
type Detector struct {
	root    string
	openBus func() (Bus, error)

	probeOnce sync.Once
	probed    []string
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

func (d *Detector) Report() Report {
	r := Report{Suggested: []string{}, Options: []Card{}}
	if r.Supported = d.Supported(); !r.Supported {
		return r
	}
	r.Options = d.options()
	r.HAT = readHAT(d.root)
	configured := configured(d.root)
	if configured != "" {
		r.Configured = &configured
	}
	r.Active = activeCard(readCards(d.root), configured)
	r.Suggested = offered(d.guesses(r.HAT, r.Active), r.Options)
	r.Name = name(r.HAT, configured, r.Active)
	return r
}

// guesses names the cards the hardware points to. The I2C bus is only read
// when no card came up and no ID chip settled it, and only once: a HAT cannot
// change without a restart.
func (d *Detector) guesses(hat *HAT, active *Active) []string {
	if hat != nil {
		if s := hat.suggestion(); s != "" {
			return []string{s}
		}
		if hat.selfConfiguring() {
			return nil
		}
	}
	if active != nil {
		return nil
	}
	d.probeOnce.Do(func() { d.probed = probeBus(d.openBus) })
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
	case hat != nil:
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
