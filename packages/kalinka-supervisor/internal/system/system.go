// Package system contains the supervisor's small Linux process boundary.
package system

import (
	"kalinka/supervisor/internal/protocol"
	"net"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// PrivateDir rejects shared or redirected state directories before root writes.
func PrivateDir(path string) error {
	if err := os.MkdirAll(path, 0700); err != nil {
		return protocol.Storage
	}
	info, err := os.Lstat(path)
	if err != nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return protocol.Storage
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Geteuid()) {
		return protocol.Storage
	}
	return nil
}
func Lock(path string) (*os.File, error) {
	fd, err := syscall.Open(path, syscall.O_CREAT|syscall.O_RDWR|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, protocol.Storage
	}
	f := os.NewFile(uintptr(fd), path)
	if syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		f.Close()
		return nil, protocol.Busy
	}
	return f, nil
}

// Notify needs no library or helper process, including when Python is damaged.
func Notify(message string) error {
	address := os.Getenv("NOTIFY_SOCKET")
	if address == "" {
		return nil
	}
	if strings.HasPrefix(address, "@") {
		address = "\x00" + address[1:]
	}
	c, err := net.DialUnix("unixgram", nil, &net.UnixAddr{Name: address, Net: "unixgram"})
	if err != nil {
		return protocol.Unavailable
	}
	defer c.Close()
	_ = c.SetWriteDeadline(time.Now().Add(time.Second))
	if _, err = c.Write([]byte(message)); err != nil {
		return protocol.Unavailable
	}
	return nil
}
func WatchdogInterval() time.Duration {
	if pid := os.Getenv("WATCHDOG_PID"); pid != "" && pid != strconv.Itoa(os.Getpid()) {
		return 0
	}
	us, err := strconv.ParseInt(os.Getenv("WATCHDOG_USEC"), 10, 64)
	if err != nil || us <= 0 {
		return 0
	}
	return time.Duration(us) * time.Microsecond / 2
}
