// Package protocol defines the app's version 1 BLE contract.
package protocol

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"regexp"
	"time"
	"unicode/utf8"
)

const (
	ServiceUUID  = "7c8e0001-1b2f-4e6a-9d3c-4b616c696e6b"
	StatusUUID   = "7c8e0002-1b2f-4e6a-9d3c-4b616c696e6b"
	IdentityUUID = "7c8e0003-1b2f-4e6a-9d3c-4b616c696e6b"
	CommandUUID  = "7c8e0004-1b2f-4e6a-9d3c-4b616c696e6b"
	NetworksUUID = "7c8e0005-1b2f-4e6a-9d3c-4b616c696e6b"
	ProgressUUID = "7c8e0006-1b2f-4e6a-9d3c-4b616c696e6b"
	MaxMessage   = 512
)

// Error is a fixed, credential-free failure code.
type Error string

const (
	Invalid       Error = "invalid_request"
	WrongPassword Error = "wrong_password"
	NoAddress     Error = "no_address"
	Timeout       Error = "timeout"
	Unavailable   Error = "unavailable"
	Storage       Error = "storage_error"
	Busy          Error = "busy"
)

func (e Error) Error() string { return string(e) }
func Reason(err error) byte {
	switch err {
	case Invalid:
		return 1
	case WrongPassword:
		return 2
	case NoAddress:
		return 3
	case Timeout:
		return 4
	case Storage:
		return 6
	case Busy:
		return 7
	default:
		return 5
	}
}

// Secret redacts formatting and serialization. Reveal is only for the backend.
type Secret struct{ value []byte }

func NewSecret(v string) Secret             { return Secret{[]byte(v)} }
func (s Secret) Reveal() string             { return string(s.value) }
func (s Secret) Clear()                     { clear(s.value) }
func (Secret) String() string               { return "<secret>" }
func (Secret) GoString() string             { return "<secret>" }
func (Secret) Format(s fmt.State, _ rune)   { _, _ = io.WriteString(s, "<secret>") }
func (Secret) LogValue() slog.Value         { return slog.StringValue("<secret>") }
func (Secret) MarshalJSON() ([]byte, error) { return []byte(`"<secret>"`), nil }
func (Secret) MarshalText() ([]byte, error) { return []byte("<secret>"), nil }

// Command contains validated input only; unknown JSON fields are ignored.
type Command struct {
	Op, SSID, Country string
	Password          Secret
	Page              int
}

var CountryPattern = regexp.MustCompile(`^[A-Z]{2}$`)

func ValidSSID(s string) bool {
	if !utf8.ValidString(s) || len(s) < 1 || len(s) > 32 {
		return false
	}
	for _, r := range s {
		if r < 32 || r == 127 {
			return false
		}
	}
	return true
}
func Decode(raw []byte) (Command, error) {
	var c Command
	if len(raw) > MaxMessage || !utf8.Valid(raw) {
		return c, Invalid
	}
	var obj map[string]json.RawMessage
	if json.Unmarshal(raw, &obj) != nil || obj == nil {
		return c, Invalid
	}
	var v int
	if json.Unmarshal(obj["v"], &v) != nil || v != 1 || json.Unmarshal(obj["op"], &c.Op) != nil {
		return c, Invalid
	}
	str := func(key string, dst *string) bool {
		b, ok := obj[key]
		return ok && !bytes.Equal(b, []byte("null")) && json.Unmarshal(b, dst) == nil
	}
	switch c.Op {
	case "complete", "change_network":
		return c, nil
	case "networks":
		b, ok := obj["page"]
		if !ok || bytes.Equal(b, []byte("null")) || json.Unmarshal(b, &c.Page) != nil || c.Page < 0 || c.Page >= 10 {
			return c, Invalid
		}
		return c, nil
	case "scan", "join":
		if !str("country", &c.Country) || !CountryPattern.MatchString(c.Country) {
			return c, Invalid
		}
		if c.Op == "scan" {
			return c, nil
		}
		var password string
		if !str("ssid", &c.SSID) || !ValidSSID(c.SSID) || !str("password", &password) || len(password) < 8 || len(password) > 63 {
			return c, Invalid
		}
		for _, r := range password {
			if r < 32 || r > 126 {
				return c, Invalid
			}
		}
		c.Password = NewSecret(password)
		return c, nil
	default:
		return c, Invalid
	}
}

// Frames reassembles bounded, per-phone ATT writes with an idle expiry.
type Frames struct {
	Owner    string
	data     []byte
	deadline time.Time
}

func (f *Frames) Clear() { clear(f.data); f.data = nil; f.Owner = ""; f.deadline = time.Time{} }
func (f *Frames) Feed(device string, data []byte, now time.Time) ([]byte, error) {
	if !now.Before(f.deadline) {
		f.Clear()
	}
	if f.Owner != "" && f.Owner != device {
		return nil, Busy
	}
	if device == "" || len(data) < 2 || len(data) > 20 || data[0]&^byte(3) != 0 {
		f.Clear()
		return nil, Invalid
	}
	if data[0]&1 != 0 {
		f.Clear()
		f.Owner = device
	}
	if f.Owner != device {
		return nil, Invalid
	}
	f.data = append(f.data, data[1:]...)
	f.deadline = now.Add(10 * time.Second)
	if len(f.data) > MaxMessage {
		f.Clear()
		return nil, Invalid
	}
	if data[0]&2 != 0 {
		out := bytes.Clone(f.data)
		f.Clear()
		return out, nil
	}
	return nil, nil
}
func Frame(raw []byte) [][]byte {
	var out [][]byte
	for i := 0; i < len(raw); i += 19 {
		end := min(i+19, len(raw))
		var flags byte
		if i == 0 {
			flags |= 1
		}
		if end == len(raw) {
			flags |= 2
		}
		out = append(out, append([]byte{flags}, raw[i:end]...))
	}
	return out
}

const (
	Idle byte = iota
	Joining
	Joined
	Failed
	Online
)
const (
	Preparing byte = iota
	Authenticating
	GettingAddress
	Saving
	Connected
)

func Status(state, reason byte, address string, port uint16, test bool) []byte {
	out := []byte{1, 29, state, reason, 0, 0, 0, 0, 0, 0}
	if test {
		out[1] |= 2
	}
	copy(out[4:8], net.ParseIP(address).To4())
	binary.BigEndian.PutUint16(out[8:], port)
	return out
}

// Network is a public scan result, without credentials.
type Network struct {
	SSID     string `json:"ssid"`
	Signal   int    `json:"signal"`
	Security string `json:"security"`
}

// NetworkPage preserves the original JSON field order and empty-array encoding.
type NetworkPage struct {
	Version  int       `json:"v"`
	ID       int       `json:"id"`
	State    string    `json:"state"`
	Reason   *string   `json:"reason"`
	Page     int       `json:"page"`
	Pages    int       `json:"pages"`
	Networks []Network `json:"networks"`
}

func JSON(v any) []byte {
	var b bytes.Buffer
	e := json.NewEncoder(&b)
	e.SetEscapeHTML(false)
	_ = e.Encode(v)
	return bytes.TrimSuffix(b.Bytes(), []byte("\n"))
}
