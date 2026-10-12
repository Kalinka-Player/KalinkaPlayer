package soundcard

import (
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"kalinka/supervisor/internal/devicetree"
)

// hatMap is the firmware's own table of HATs whose overlay it loads from the ID chip alone.
const hatMap = "boot/firmware/overlays/hat_map.dtb"

// maxHATMap bounds the read; the firmware's table is a few kilobytes.
const maxHATMap = 1 << 16

// mappedOverlay names the overlay the firmware's table loads for hat, or ""
// when it lists no such board. Parameters after the name are dropped.
func mappedOverlay(root string, hat *HAT) string {
	f, err := os.Open(filepath.Join(root, hatMap))
	if err != nil {
		return ""
	}
	defer f.Close()
	blob, err := io.ReadAll(io.LimitReader(f, maxHATMap))
	if err != nil {
		return ""
	}
	entries, err := devicetree.Children(blob)
	if err != nil {
		return ""
	}
	for _, e := range entries {
		if mapped(e, hat) {
			name, _, _ := strings.Cut(devicetree.String(e.Props["overlay"]), ",")
			return name
		}
	}
	return ""
}

// mapped matches an entry as the firmware does: by UUID, or by vendor,
// product and, where the entry gives one, product version.
func mapped(e devicetree.Node, hat *HAT) bool {
	if uuid := e.Props["uuid"]; len(uuid) == 16 {
		return strings.EqualFold(fmt.Sprintf("%x-%x-%x-%x-%x", uuid[:4], uuid[4:6], uuid[6:8], uuid[8:10], uuid[10:]), hat.UUID)
	}
	vendor, product := e.Props["vendor"], e.Props["product"]
	if vendor == nil || product == nil || devicetree.String(vendor) != hat.Vendor || devicetree.String(product) != hat.Product {
		return false
	}
	pver, ok := e.Props["pver"]
	if !ok {
		return true
	}
	v, err := strconv.ParseUint(hat.ProductVer, 0, 32)
	return err == nil && len(pver) == 4 && uint32(v) == binary.BigEndian.Uint32(pver)
}
