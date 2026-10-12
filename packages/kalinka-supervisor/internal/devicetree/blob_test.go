package devicetree_test

import (
	"bytes"
	"encoding/binary"
	"errors"
	"testing"

	"kalinka/supervisor/internal/devicetree"
	"kalinka/supervisor/internal/devicetree/devicetreetest"
)

func sample() []byte {
	return devicetreetest.Blob(
		devicetree.Node{Name: "hifiberry-digi2pro", Props: map[string][]byte{
			"uuid":    devicetreetest.Cells(0x5af941bb, 0x4dcf4eac, 0x82a8e36e, 0x84fcabef),
			"overlay": devicetreetest.String("hifiberry-digi-pro"),
		}},
		devicetree.Node{Name: "sensehat-v2", Props: map[string][]byte{
			"vendor":  devicetreetest.String("Raspberry Pi"),
			"product": devicetreetest.String("Sense HAT"),
			"pver":    devicetreetest.Cells(2),
			"overlay": devicetreetest.String("rpi-sense-v2"),
		}},
	)
}

func TestChildren(t *testing.T) {
	nodes, err := devicetree.Children(sample())
	if err != nil {
		t.Fatal(err)
	}
	if len(nodes) != 2 || nodes[0].Name != "hifiberry-digi2pro" || nodes[1].Name != "sensehat-v2" {
		t.Fatalf("nodes = %+v", nodes)
	}
	if got := devicetree.String(nodes[0].Props["overlay"]); got != "hifiberry-digi-pro" {
		t.Fatalf("overlay = %q", got)
	}
	if !bytes.Equal(nodes[0].Props["uuid"], devicetreetest.Cells(0x5af941bb, 0x4dcf4eac, 0x82a8e36e, 0x84fcabef)) {
		t.Fatalf("uuid = %x", nodes[0].Props["uuid"])
	}
	if got := devicetree.String(nodes[1].Props["overlay"]); got != "rpi-sense-v2" || len(nodes[1].Props) != 4 {
		t.Fatalf("second node = %+v", nodes[1].Props)
	}
}

func TestString(t *testing.T) {
	if got := devicetree.String([]byte("none\x00pciex1\x00")); got != "none" {
		t.Fatalf("first of a list = %q", got)
	}
	if got := devicetree.String(nil); got != "" {
		t.Fatalf("empty = %q", got)
	}
}

func TestChildrenRefusesWhatIsNoBlob(t *testing.T) {
	blob := sample()
	for n := range len(blob) {
		if _, err := devicetree.Children(blob[:n]); !errors.Is(err, devicetree.ErrBlob) {
			t.Fatalf("a blob cut to %d bytes read as %v", n, err)
		}
	}
	unknown := bytes.Clone(blob)
	structAt := binary.BigEndian.Uint32(unknown[8:])
	binary.BigEndian.PutUint32(unknown[structAt:], 7)
	if _, err := devicetree.Children(unknown); !errors.Is(err, devicetree.ErrBlob) {
		t.Fatal("an unknown token was read past")
	}
	if _, err := devicetree.Children([]byte("dtoverlay=hifiberry-digi\n")); !errors.Is(err, devicetree.ErrBlob) {
		t.Fatal("text read as a blob")
	}
}
