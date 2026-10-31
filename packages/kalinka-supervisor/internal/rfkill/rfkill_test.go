package rfkill

import (
	"os"
	"path/filepath"
	"slices"
	"testing"

	"kalinka/supervisor/internal/rfkill/rfkilltest"
)

func TestUnblockClearsOnlyTheKindAsked(t *testing.T) {
	root := rfkilltest.Stage(t,
		rfkilltest.Radio{Type: "wlan", Name: "phy0", Soft: true},
		rfkilltest.Radio{Type: "bluetooth", Name: "hci0", Soft: true},
		rfkilltest.Radio{Type: "wlan", Name: "phy1", Hard: true},
		rfkilltest.Radio{Type: "nfc", Name: "nfc0", Soft: true},
	)
	cleared, hard, err := Unblock(root, "wlan")
	if err != nil || !slices.Equal(cleared, []string{"phy0"}) || !slices.Equal(hard, []string{"phy1"}) {
		t.Fatalf("wlan: cleared %v, hard %v, error %v", cleared, hard, err)
	}
	for entry, want := range map[string]bool{"rfkill0": false, "rfkill1": true, "rfkill2": false, "rfkill3": true} {
		if got := rfkilltest.Soft(t, root, entry); got != want {
			t.Fatalf("after wlan, %s soft-blocked %v, want %v", entry, got, want)
		}
	}
	cleared, hard, err = Unblock(root, "bluetooth")
	if err != nil || !slices.Equal(cleared, []string{"hci0"}) || hard != nil {
		t.Fatalf("bluetooth: cleared %v, hard %v, error %v", cleared, hard, err)
	}
	if rfkilltest.Soft(t, root, "rfkill1") || !rfkilltest.Soft(t, root, "rfkill3") {
		t.Fatal("bluetooth cleared the wrong radio")
	}
}

func TestUnblockClearsTheSoftBlockOfAHardBlockedRadio(t *testing.T) {
	root := rfkilltest.Stage(t, rfkilltest.Radio{Type: "bluetooth", Name: "hci0", Soft: true, Hard: true})
	cleared, hard, err := Unblock(root, "bluetooth")
	if err != nil || !slices.Equal(cleared, []string{"hci0"}) || !slices.Equal(hard, []string{"hci0"}) {
		t.Fatalf("cleared %v, hard %v, error %v", cleared, hard, err)
	}
}

func TestUnblockWithoutRfkill(t *testing.T) {
	cleared, hard, err := Unblock(t.TempDir(), "wlan")
	if cleared != nil || hard != nil || err != nil {
		t.Fatalf("cleared %v, hard %v, error %v", cleared, hard, err)
	}
}

func TestUnblockReportsASoftBlockItCannotClear(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root writes a read-only file")
	}
	root := rfkilltest.Stage(t, rfkilltest.Radio{Type: "wlan", Name: "phy0", Soft: true}, rfkilltest.Radio{Type: "wlan", Name: "phy1", Soft: true})
	if err := os.Chmod(filepath.Join(rfkilltest.Entry(root, "rfkill0"), "soft"), 0444); err != nil {
		t.Fatal(err)
	}
	cleared, _, err := Unblock(root, "wlan")
	if err == nil || !slices.Equal(cleared, []string{"phy1"}) {
		t.Fatalf("cleared %v, error %v", cleared, err)
	}
}
