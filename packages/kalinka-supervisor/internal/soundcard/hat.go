package soundcard

import (
	"strings"

	"kalinka/supervisor/internal/devicetree"
)

// HAT is what a board's ID chip says about it, as the firmware copied it into the device tree.
type HAT struct {
	Vendor     string `json:"vendor"`
	Product    string `json:"product"`
	ProductID  string `json:"product_id"`
	ProductVer string `json:"product_ver"`
	UUID       string `json:"uuid"`
}

// hifiberryProducts maps HiFiBerry's ID-chip product names to their overlays,
// as HiFiBerry's own detect-hifiberry does: their chips name the board but
// do not switch it on.
var hifiberryProducts = map[string]string{
	"Amp100":       "hifiberry-amp100",
	"DAC+ ADC Pro": "hifiberry-dacplusadcpro",
	"DAC+ ADC":     "hifiberry-dacplusadc",
	"DAC2 ADC Pro": "hifiberry-dacplusadcpro",
	"DAC 2 HD":     "hifiberry-dacplushd",
	"Digi2 Pro":    "hifiberry-digi-pro",
	"Amp3":         "hifiberry-amp3",
	"Amp4 Pro":     "hifiberry-amp4pro",
	"Amp4":         "hifiberry-dacplus-std",
	"DAC8x":        "hifiberry-dac8x",
	"DSP 2x4":      "hifiberry-dacplusdsp",
}

// selfConfiguringVendors make ID chips the firmware sets the board up from, with nothing to choose.
var selfConfiguringVendors = []string{"Raspberry Pi Ltd.", "IQaudIO Limited."}

// companySuffixes are dropped from a vendor to leave the brand people know it by.
var companySuffixes = []string{"Ltd.", "Ltd", "Limited.", "Limited", "Inc.", "Inc", "GmbH", "LLC"}

// readHAT returns the fitted HAT's identity, or nil when no ID chip was read at boot.
func readHAT(root string) *HAT {
	product, ok := devicetree.Property(root, "hat/product")
	if !ok {
		return nil
	}
	read := func(name string) string {
		v, _ := devicetree.Property(root, "hat/"+name)
		return v
	}
	return &HAT{Vendor: read("vendor"), Product: product, ProductID: read("product_id"), ProductVer: read("product_ver"), UUID: read("uuid")}
}

// suggestion names the card a HiFiBerry chip asks for, or "" when there is none to make.
func (h *HAT) suggestion() string {
	if !strings.Contains(strings.ToLower(h.Vendor), "hifiberry") {
		return ""
	}
	return hifiberryProducts[h.Product]
}

// selfConfiguring reports whether the firmware already set the board up from its chip.
func (h *HAT) selfConfiguring() bool {
	for _, vendor := range selfConfiguringVendors {
		if h.Vendor == vendor {
			return true
		}
	}
	return false
}

// Name is the board as a person would call it: the product, after the brand unless the product already carries it.
func (h *HAT) Name() string {
	brand := strings.TrimSpace(h.Vendor)
	for _, suffix := range companySuffixes {
		if trimmed, ok := strings.CutSuffix(brand, " "+suffix); ok {
			brand = strings.TrimSpace(trimmed)
			break
		}
	}
	if brand == "" || strings.Contains(strings.ToLower(h.Product), strings.ToLower(brand)) {
		return h.Product
	}
	return brand + " " + h.Product
}
