package system

import (
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestPrivateStateAndExclusiveLock(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "state")
	if err := PrivateDir(dir); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "link")
	_ = os.Symlink(dir, link)
	if PrivateDir(link) == nil {
		t.Fatal("symlink accepted")
	}
	_ = os.Chmod(dir, 0755)
	if PrivateDir(dir) == nil {
		t.Fatal("shared state accepted")
	}
	_ = os.Chmod(dir, 0700)
	lock, err := Lock(filepath.Join(dir, "lock"))
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if f, err := Lock(filepath.Join(dir, "lock")); err == nil {
		f.Close()
		t.Fatal("second process lock accepted")
	}
}
func TestNotify(t *testing.T) {
	path := filepath.Join(t.TempDir(), "notify")
	conn, err := net.ListenUnixgram("unixgram", &net.UnixAddr{Name: path, Net: "unixgram"})
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	t.Setenv("NOTIFY_SOCKET", path)
	if err := Notify("READY=1"); err != nil {
		t.Fatal(err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(time.Second))
	buf := make([]byte, 100)
	n, _, err := conn.ReadFromUnix(buf)
	if err != nil || string(buf[:n]) != "READY=1" {
		t.Fatal("notification not delivered")
	}
	t.Setenv("WATCHDOG_USEC", "60000000")
	t.Setenv("WATCHDOG_PID", "")
	if WatchdogInterval() != 30*time.Second {
		t.Fatal("watchdog cadence")
	}
}
