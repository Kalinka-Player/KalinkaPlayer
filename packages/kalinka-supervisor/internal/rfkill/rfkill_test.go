package rfkill

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"testing"

	"kalinka/supervisor/internal/rfkill/rfkilltest"
)

func TestUnblockClearsOnlyTheSelectedRadio(t *testing.T) {
	root := rfkilltest.Stage(t,
		rfkilltest.Radio{Type: "wlan", Name: "phy0", Soft: true},
		rfkilltest.Radio{Type: "bluetooth", Name: "hci0", Soft: true},
		rfkilltest.Radio{Type: "wlan", Name: "phy1", Soft: true},
		rfkilltest.Radio{Type: "bluetooth", Name: "hci1", Soft: true},
	)
	cleared, hard, err := Unblock(root, "wlan", "phy0")
	if err != nil || !slices.Equal(cleared, []string{"phy0"}) || len(hard) != 0 {
		t.Fatalf("%v %v %v", cleared, hard, err)
	}
	if rfkilltest.Soft(t, root, "rfkill0") || !rfkilltest.Soft(t, root, "rfkill2") {
		t.Fatal("changed another Wi-Fi radio")
	}
	cleared, hard, err = Unblock(root, "bluetooth", "hci0")
	if err != nil || !slices.Equal(cleared, []string{"hci0"}) || len(hard) != 0 {
		t.Fatalf("%v %v %v", cleared, hard, err)
	}
	if rfkilltest.Soft(t, root, "rfkill1") || !rfkilltest.Soft(t, root, "rfkill3") {
		t.Fatal("changed another Bluetooth radio")
	}
}

func TestUnblockPreservesHardBlockedRadio(t *testing.T) {
	root := rfkilltest.Stage(t, rfkilltest.Radio{Type: "bluetooth", Name: "hci0", Soft: true, Hard: true})
	cleared, hard, err := Unblock(root, "bluetooth", "hci0")
	if !errors.Is(err, ErrHardBlocked) || len(cleared) != 0 || !slices.Equal(hard, []string{"hci0"}) || !rfkilltest.Soft(t, root, "rfkill0") {
		t.Fatalf("cleared %v, hard %v, error %v", cleared, hard, err)
	}
}

func TestUnblockWithoutRfkill(t *testing.T) {
	cleared, hard, err := Unblock(t.TempDir(), "wlan", "phy0")
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
	cleared, _, err := Unblock(root, "wlan", "phy0")
	if err == nil || len(cleared) != 0 {
		t.Fatalf("cleared %v, error %v", cleared, err)
	}
}
