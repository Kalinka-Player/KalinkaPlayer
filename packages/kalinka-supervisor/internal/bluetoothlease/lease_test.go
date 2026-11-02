package bluetoothlease

import "testing"

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
