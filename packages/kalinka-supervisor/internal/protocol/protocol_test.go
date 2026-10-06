package protocol

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"
)

func TestPythonConformance(t *testing.T) {
	var gold struct {
		Commands []struct {
			Raw   string
			Valid bool
		}
		Statuses []struct {
			State, Reason byte
			Test          bool
			Hex           string
		}
	}
	b, err := os.ReadFile("../../testdata/python-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(b, &gold); err != nil {
		t.Fatal(err)
	}
	for _, c := range gold.Commands {
		got, err := Decode([]byte(c.Raw))
		if (err == nil) != c.Valid {
			t.Errorf("validity differs: %s", c.Raw)
		}
		got.Password.Clear()
	}
	for _, s := range gold.Statuses {
		if got := hex.EncodeToString(Status(s.State, s.Reason, "192.0.2.5", 8000, s.Test)); got != s.Hex {
			t.Errorf("status %s != %s", got, s.Hex)
		}
	}
}
func TestSecretRedaction(t *testing.T) {
	secret := "never-log-this-password"
	c := Command{Op: "join", Password: NewSecret(secret)}
	var out bytes.Buffer
	for _, format := range []string{"%v", "%+v", "%#v", "%s", "%q", "%x"} {
		fmt.Fprintf(&out, format, c)
		fmt.Fprintf(&out, format, c.Password)
	}
	_ = json.NewEncoder(&out).Encode(c)
	slog.New(slog.NewJSONHandler(&out, nil)).Info("test", "command", c, "password", c.Password)
	slog.New(slog.NewTextHandler(&out, nil)).Info("test", "command", c, "password", c.Password)
	if strings.Contains(out.String(), secret) || strings.Contains(out.String(), hex.EncodeToString([]byte(secret))) {
		t.Fatal("secret leaked")
	}
	if c.Password.Reveal() != secret {
		t.Fatal("backend cannot access secret")
	}
	c.Password.Clear()
	if strings.Trim(c.Password.Reveal(), "\x00") != "" {
		t.Fatal("clear failed")
	}
}
func TestFramesOwnershipExpiryAndLimits(t *testing.T) {
	var f Frames
	now := time.Unix(100, 0)
	raw := []byte(`{"v":1,"op":"scan","country":"GB"}`)
	frames := Frame(raw)
	if _, err := f.Feed("a", frames[0], now); err != nil {
		t.Fatal(err)
	}
	if _, err := f.Feed("b", frames[0], now); err != Busy {
		t.Fatal("second owner accepted")
	}
	got, err := f.Feed("a", frames[1], now)
	if err != nil || !bytes.Equal(got, raw) {
		t.Fatal("framing mismatch")
	}
	_, _ = f.Feed("a", frames[0], now)
	if _, err = f.Feed("a", frames[1], now.Add(11*time.Second)); err != Invalid {
		t.Fatal("expired frame accepted")
	}
	big := Frame(bytes.Repeat([]byte("x"), 513))
	for _, frame := range big {
		_, err = f.Feed("a", frame, now)
	}
	if err != Invalid || f.Owner != "" {
		t.Fatal("oversize retained")
	}
}
func FuzzDecode(f *testing.F) {
	f.Add([]byte(`{"v":1,"op":"complete"}`))
	f.Add([]byte(`{"v":1,"op":"join","ssid":"home","password":"dummy-password","country":"GB"}`))
	f.Fuzz(func(t *testing.T, b []byte) {
		c, err := Decode(b)
		if err != nil && err != Invalid {
			t.Fatal("unbounded error")
		}
		c.Password.Clear()
	})
}
