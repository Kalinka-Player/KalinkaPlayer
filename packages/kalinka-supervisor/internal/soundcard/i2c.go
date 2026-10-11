package soundcard

import "slices"

// Bus reads single registers from the chips on the HAT's I2C bus.
type Bus interface {
	ReadRegister(addr, reg uint8) (uint8, error)
	Close() error
}

// probe is one register read: a value the chip's reset state gives names the
// chip, and the chip only narrows the board down to those that carry it.
type probe struct {
	addr, reg uint8
	values    []uint8
	cards     []string
}

// probes are the reads HiFiBerry's own detect-hifiberry makes, most specific
// chip first, and the boards from any maker that use each chip.
var probes = []probe{
	{addr: 0x4a, reg: 25, values: []uint8{0x07}, cards: []string{"hifiberry-dacplusadcpro"}},
	{addr: 0x62, reg: 17, values: []uint8{0x8c}, cards: []string{"hifiberry-dacplushd"}},
	{addr: 0x1b, reg: 0, values: []uint8{0x6c, 0x60}, cards: []string{"hifiberry-amp"}},
	{addr: 0x3b, reg: 1, values: []uint8{0x88}, cards: []string{"hifiberry-digi", "hifiberry-digi-pro", "allo-digione", "justboom-digi", "iqaudio-digi-wm8804-audio"}},
	{addr: 0x4d, reg: 40, values: []uint8{0x02}, cards: []string{"hifiberry-dacplus-std", "hifiberry-dacplus-pro", "allo-boss-dac-pcm512x-audio", "justboom-dac"}},
	{addr: 0x4c, reg: 40, values: []uint8{0x02}, cards: []string{"rpi-dacplus", "rpi-dacpro", "iqaudio-dacplus"}},
}

// probeBus returns the cards whose chips answer, and whether the bus could be opened.
func probeBus(open func() (Bus, error)) ([]string, bool) {
	bus, err := open()
	if err != nil {
		return nil, false
	}
	defer bus.Close()
	var found []string
	for _, p := range probes {
		if v, err := bus.ReadRegister(p.addr, p.reg); err == nil && slices.Contains(p.values, v) {
			found = append(found, p.cards...)
		}
	}
	return found, true
}
