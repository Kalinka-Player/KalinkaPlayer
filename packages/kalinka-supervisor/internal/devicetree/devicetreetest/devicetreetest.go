// Package devicetreetest builds flattened device trees for tests, as dtc would.
package devicetreetest

import (
	"bytes"
	"encoding/binary"
	"slices"

	"kalinka/supervisor/internal/devicetree"
)

// Blob encodes nodes as the children of a flattened device tree's root.
func Blob(nodes ...devicetree.Node) []byte {
	var structure, strs bytes.Buffer
	offsets := map[string]int{}
	u32 := func(b *bytes.Buffer, v int) { _ = binary.Write(b, binary.BigEndian, uint32(v)) }
	pad := func() {
		for structure.Len()%4 != 0 {
			structure.WriteByte(0)
		}
	}
	begin := func(name string) {
		u32(&structure, 1)
		structure.WriteString(name + "\x00")
		pad()
	}
	begin("")
	for _, n := range nodes {
		begin(n.Name)
		names := make([]string, 0, len(n.Props))
		for name := range n.Props {
			names = append(names, name)
		}
		slices.Sort(names)
		for _, name := range names {
			at, ok := offsets[name]
			if !ok {
				at = strs.Len()
				offsets[name] = at
				strs.WriteString(name + "\x00")
			}
			u32(&structure, 3)
			u32(&structure, len(n.Props[name]))
			u32(&structure, at)
			structure.Write(n.Props[name])
			pad()
		}
		u32(&structure, 2)
	}
	u32(&structure, 2)
	u32(&structure, 9)

	const header, reserved = 40, 16
	var blob bytes.Buffer
	structAt := header + reserved
	stringsAt := structAt + structure.Len()
	for _, v := range []int{0xd00dfeed, stringsAt + strs.Len(), structAt, stringsAt, header, 17, 16, 0, strs.Len(), structure.Len()} {
		u32(&blob, v)
	}
	blob.Write(make([]byte, reserved))
	blob.Write(structure.Bytes())
	blob.Write(strs.Bytes())
	return blob.Bytes()
}

// String encodes a string property.
func String(s string) []byte { return []byte(s + "\x00") }

// Cells encodes a property of 32-bit cells.
func Cells(cells ...uint32) []byte {
	b := make([]byte, 0, 4*len(cells))
	for _, c := range cells {
		b = binary.BigEndian.AppendUint32(b, c)
	}
	return b
}
