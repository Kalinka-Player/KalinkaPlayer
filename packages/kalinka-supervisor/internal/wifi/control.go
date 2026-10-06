package wifi

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"

	"kalinka/supervisor/internal/protocol"
)

// Control confines supplicant secrets to its Unix datagram socket.
type Control interface {
	Request(context.Context, string) (string, error)
	Events(context.Context) ([]string, error)
	Close() error
}
type socketControl struct {
	conn  *net.UnixConn
	local string
}

func OpenControl(iface, runtime string) (Control, error) {
	if os.MkdirAll(runtime, 0700) != nil {
		return nil, protocol.Unavailable
	}
	local := filepath.Join(runtime, "ctrl-"+UUID()[:12])
	c, err := net.DialUnix("unixgram", &net.UnixAddr{Name: local, Net: "unixgram"}, &net.UnixAddr{Name: "/run/wpa_supplicant/" + iface, Net: "unixgram"})
	if err != nil {
		return nil, protocol.Unavailable
	}
	return &socketControl{c, local}, nil
}
func (c *socketControl) Request(ctx context.Context, command string) (string, error) {
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	_ = c.conn.SetDeadline(time.Now().Add(3 * time.Second))
	stop := context.AfterFunc(ctx, func() { _ = c.conn.SetDeadline(time.Now()) })
	defer stop()
	if _, err := c.conn.Write([]byte(command)); err != nil {
		return "", protocol.Unavailable
	}
	buffer := make([]byte, 65536)
	n, err := c.conn.Read(buffer)
	if err != nil {
		return "", protocol.Unavailable
	}
	response := strings.TrimSpace(string(buffer[:n]))
	if strings.HasPrefix(response, "FAIL") {
		return "", protocol.Unavailable
	}
	return response, nil
}
func (c *socketControl) Events(ctx context.Context) ([]string, error) {
	var events []string
	for i := 0; i < 128; i++ {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		_ = c.conn.SetReadDeadline(time.Now().Add(time.Millisecond))
		b := make([]byte, 65536)
		n, err := c.conn.Read(b)
		if err != nil {
			if e, ok := err.(net.Error); ok && e.Timeout() {
				return events, nil
			}
			return nil, protocol.Unavailable
		}
		events = append(events, string(b[:n]))
	}
	return events, nil
}
func (c *socketControl) Close() error { err := c.conn.Close(); _ = os.Remove(c.local); return err }
