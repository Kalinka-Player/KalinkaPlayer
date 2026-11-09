// Package bluetoothlease serializes Kalinka's adapter-global pairing changes.
// The audio plugin takes the same flock. Media capture does not take a lease.
package bluetoothlease

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"syscall"
)

type Lease struct{ file *os.File }

func Directory() string {
	prefix := os.Getenv("KALINKA_PREFIX")
	if prefix == "" {
		prefix = "/"
	}
	return filepath.Join(prefix, "run/lock/kalinka-bluetooth")
}

func Acquire(directory, adapter string) (*Lease, error) {
	if !regexp.MustCompile(`^hci[0-9]+$`).MatchString(adapter) {
		return nil, fmt.Errorf("invalid Bluetooth adapter")
	}
	if err := os.MkdirAll(directory, 0755); err != nil {
		return nil, err
	}
	// Read-only suffices for flock and lets kalusr lock a file made by root.
	file, err := os.OpenFile(filepath.Join(directory, "bluetooth-"+adapter+".lock"), os.O_CREATE|os.O_RDONLY, 0644)
	if err != nil {
		return nil, err
	}
	// The service runs with UMask=0077. The audio plugin runs as kalusr,
	// so explicitly permit it to open this shared inode for a read-only flock.
	// Also repair lock files created by older supervisor versions.
	if err = file.Chmod(0644); err != nil {
		file.Close()
		return nil, err
	}
	if err = syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		file.Close()
		return nil, fmt.Errorf("Bluetooth audio pairing is using %s: %w", adapter, err)
	}
	return &Lease{file}, nil
}

func (l *Lease) Close() {
	if l != nil && l.file != nil {
		l.file.Close()
		l.file = nil
	}
	// Never unlink: another process may still have this inode open.
}
