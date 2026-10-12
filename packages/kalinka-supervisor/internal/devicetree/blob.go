package devicetree

import (
	"bytes"
	"encoding/binary"
	"errors"
)

// Node is one node of a flattened device tree: its name and its properties' raw values.
type Node struct {
	Name  string
	Props map[string][]byte
}

// ErrBlob reports a blob that is not a well-formed flattened device tree.
var ErrBlob = errors.New("not a flattened device tree")

const (
	blobMagic  = 0xd00dfeed
	headerSize = 40
	beginNode  = 1
	endNode    = 2
	prop       = 3
	nop        = 4
	end        = 9
)

// Children returns the children of a flattened device tree's root node, such
// as the entries of a map the firmware ships beside its overlays. Deeper
// nodes are skipped.
func Children(blob []byte) ([]Node, error) {
	u32 := func(at int) (int, bool) {
		if at < 0 || at+4 > len(blob) {
			return 0, false
		}
		return int(binary.BigEndian.Uint32(blob[at:])), true
	}
	cstring := func(at int) (string, int, bool) {
		if at < 0 || at > len(blob) {
			return "", 0, false
		}
		n := bytes.IndexByte(blob[at:], 0)
		if n < 0 {
			return "", 0, false
		}
		return string(blob[at : at+n]), n, true
	}
	align := func(at int) int { return (at + 3) &^ 3 }

	if magic, ok := u32(0); !ok || magic != blobMagic || len(blob) < headerSize {
		return nil, ErrBlob
	}
	at, _ := u32(8)
	stringsAt, _ := u32(12)
	var children []Node
	depth := 0
	for {
		token, ok := u32(at)
		if !ok {
			return nil, ErrBlob
		}
		at += 4
		switch token {
		case beginNode:
			name, n, ok := cstring(at)
			if !ok {
				return nil, ErrBlob
			}
			at = align(at + n + 1)
			if depth++; depth == 2 {
				children = append(children, Node{Name: name, Props: map[string][]byte{}})
			}
		case endNode:
			if depth--; depth < 0 {
				return nil, ErrBlob
			}
		case prop:
			size, ok1 := u32(at)
			nameAt, ok2 := u32(at + 4)
			at += 8
			if !ok1 || !ok2 || at+size > len(blob) {
				return nil, ErrBlob
			}
			name, _, ok := cstring(stringsAt + nameAt)
			if !ok {
				return nil, ErrBlob
			}
			if depth == 2 {
				children[len(children)-1].Props[name] = blob[at : at+size]
			}
			at = align(at + size)
		case nop:
		case end:
			if depth != 0 {
				return nil, ErrBlob
			}
			return children, nil
		default:
			return nil, ErrBlob
		}
	}
}

// String reads a string property, the first of a list.
func String(value []byte) string {
	s, _, _ := bytes.Cut(value, []byte{0})
	return string(s)
}
