package bluetoothlease

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func TestServiceUmaskDoesNotHideLeaseFromAudioPlugin(t *testing.T) {
	previous := syscall.Umask(0077)
	defer syscall.Umask(previous)
	directory := t.TempDir()
	path := filepath.Join(directory, "bluetooth-hci0.lock")
	for _, existing := range []bool{false, true} {
		if existing {
			if err := os.Chmod(path, 0600); err != nil {
				t.Fatal(err)
			}
		}
		lease, err := Acquire(directory, "hci0")
		if err != nil {
			t.Fatal(err)
		}
		lease.Close()
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != 0644 {
			t.Fatalf("existing=%v: audio plugin cannot read lock: %o", existing, info.Mode().Perm())
		}
	}
}

func TestLeaseExcludesOtherPairingAndSurvivesRelease(t *testing.T) {
	directory := t.TempDir()
	first, err := Acquire(directory, "hci0")
	if err != nil {
		t.Fatal(err)
	}
	if second, err := Acquire(directory, "hci0"); err == nil {
		second.Close()
		t.Fatal("two agents may not own the same adapter")
	}
	other, err := Acquire(directory, "hci1")
	if err != nil {
		t.Fatal(err)
	}
	other.Close()
	first.Close()
	again, err := Acquire(directory, "hci0")
	if err != nil {
		t.Fatal(err)
	}
	again.Close()
	again.Close()
}

func TestAdapterCannotEscapeLockDirectory(t *testing.T) {
	if lease, err := Acquire(t.TempDir(), "../hci0"); err == nil {
		lease.Close()
		t.Fatal("unsafe adapter accepted")
	}
}
