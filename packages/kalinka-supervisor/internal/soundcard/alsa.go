package soundcard

import (
	"bufio"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Active is an ALSA card that came up, as the renderer can open it.
type Active struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	PCM  string `json:"pcm"`
}

type alsaCard struct {
	index        int
	id, driver   string
	name         string
	usb, virtual bool
	// plays is whether the card has a playback device; a USB webcam's has none.
	plays bool
}

func (c alsaCard) hdmi() bool {
	return strings.HasPrefix(c.driver, "vc4-hdmi") ||
		strings.HasPrefix(c.driver, "bcm2835") && strings.Contains(c.name, "HDMI")
}

func (c alsaCard) headphones() bool {
	return strings.HasPrefix(c.driver, "bcm2835") && strings.Contains(c.name, "Headphones")
}

// hat reports whether the card is one an overlay or an ID chip brought up: not the Pi's own, not USB, not virtual.
func (c alsaCard) hat() bool {
	return !c.usb && !c.virtual && !c.hdmi() && !strings.HasPrefix(c.driver, "bcm2835")
}

func (c alsaCard) active() *Active {
	return &Active{ID: c.id, Name: c.name, PCM: "hw:CARD=" + c.id + ",DEV=0"}
}

// virtualDrivers make cards with no hardware behind them.
var virtualDrivers = []string{"Loopback", "Dummy"}

// parseCards reads /proc/asound/cards, whose cards each take a header line,
// " 0 [id             ]: driver - name", and an indented long name.
func parseCards(r io.Reader) []alsaCard {
	var cards []alsaCard
	s := bufio.NewScanner(r)
	for s.Scan() {
		line := s.Text()
		open, end := strings.IndexByte(line, '['), strings.Index(line, "]: ")
		if open < 0 || end < open {
			continue
		}
		index, err := strconv.Atoi(strings.TrimSpace(line[:open]))
		if err != nil {
			continue
		}
		driver, name, ok := strings.Cut(line[end+3:], " - ")
		if !ok {
			continue
		}
		card := alsaCard{index: index, id: strings.TrimSpace(line[open+1 : end]), driver: strings.TrimSpace(driver), name: strings.TrimSpace(name)}
		for _, v := range virtualDrivers {
			card.virtual = card.virtual || card.driver == v
		}
		cards = append(cards, card)
	}
	return cards
}

func readCards(root string) []alsaCard {
	f, err := os.Open(filepath.Join(root, "proc/asound/cards"))
	if err != nil {
		return nil
	}
	defer f.Close()
	cards := parseCards(f)
	for i := range cards {
		dir := filepath.Join(root, "proc/asound", "card"+strconv.Itoa(cards[i].index))
		_, err := os.Stat(filepath.Join(dir, "usbid"))
		cards[i].usb = err == nil
		playback, _ := filepath.Glob(filepath.Join(dir, "pcm*p"))
		cards[i].plays = len(playback) > 0
	}
	return cards
}

// activeCard finds the card the configured choice brought up: the Pi's own
// output for an onboard choice, otherwise a HAT, which an ID chip may have set
// up with nothing configured at all.
func activeCard(cards []alsaCard, configured string) *Active {
	match := alsaCard.hat
	switch configured {
	case HDMI:
		match = alsaCard.hdmi
	case HeadphoneSocket:
		match = alsaCard.headphones
	}
	for _, c := range cards {
		if match(c) {
			return c.active()
		}
	}
	return nil
}

func usbCards(cards []alsaCard) []Active {
	found := []Active{}
	for _, c := range cards {
		if c.usb && c.plays {
			found = append(found, *c.active())
		}
	}
	return found
}
