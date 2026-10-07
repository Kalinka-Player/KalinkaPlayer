package control

import (
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"kalinka/supervisor/internal/coreconf"
)

type recordingListener struct {
	requested []string
	fail      bool
}

func (r *recordingListener) listen(addr string) (net.Listener, error) {
	r.requested = append(r.requested, addr)
	if r.fail {
		return nil, errors.New("address in use")
	}
	return net.Listen("tcp4", "127.0.0.1:0")
}

func newTestBinding(h http.Handler, addresses map[string]string) (*Binding, *recordingListener) {
	rec := &recordingListener{}
	b := NewBinding(8001, h)
	b.listen = rec.listen
	b.resolve = func(iface string) (string, error) {
		ip, ok := addresses[iface]
		if !ok {
			return "", errors.New("no such interface")
		}
		return ip, nil
	}
	return b, rec
}

func TestBindingFollowsCore(t *testing.T) {
	addresses := map[string]string{"eth0": "192.168.1.5", "wlan0": ""}
	b, rec := newTestBinding(http.NotFoundHandler(), addresses)
	defer b.Close()
	steps := []struct {
		core   coreconf.Config
		status string
		bound  string
		listen []string
	}{
		{coreconf.Default, "listening on 0.0.0.0:8001", "0.0.0.0:8001", []string{"0.0.0.0:8001"}},
		{coreconf.Default, "listening on 0.0.0.0:8001", "0.0.0.0:8001", nil},
		{coreconf.Config{Interface: "eth0", Port: 8000}, "listening on 192.168.1.5:8001", "192.168.1.5:8001", []string{"192.168.1.5:8001"}},
		{coreconf.Config{Interface: "wlan0", Port: 8000}, "waiting for an address on wlan0", "", nil},
		{coreconf.Config{Interface: "usb0", Port: 8000}, "waiting for an address on usb0", "", nil},
		{coreconf.Config{Interface: "eth0", Port: 8001}, "leaving port 8001 to Core", "", nil},
		{coreconf.Config{Interface: "eth0", Port: 8000}, "listening on 192.168.1.5:8001", "192.168.1.5:8001", []string{"192.168.1.5:8001"}},
	}
	for i, step := range steps {
		before := len(rec.requested)
		if got := b.Reconcile(step.core); got != step.status {
			t.Fatalf("step %d: status %q", i, got)
		}
		if b.addr != step.bound {
			t.Fatalf("step %d: bound to %q", i, b.addr)
		}
		if got := rec.requested[before:]; strings.Join(got, ",") != strings.Join(step.listen, ",") {
			t.Fatalf("step %d: listened on %v", i, got)
		}
	}
}

func TestBindingRetriesFailedListen(t *testing.T) {
	b, rec := newTestBinding(http.NotFoundHandler(), nil)
	defer b.Close()
	rec.fail = true
	if got := b.Reconcile(coreconf.Default); got != "cannot listen on 0.0.0.0:8001" {
		t.Fatalf("status %q", got)
	}
	rec.fail = false
	if got := b.Reconcile(coreconf.Default); got != "listening on 0.0.0.0:8001" || len(rec.requested) != 2 {
		t.Fatalf("status %q after %v", got, rec.requested)
	}
}

func TestCloseDrainsRequestsInFlight(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	h := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		close(entered)
		<-release
		_, _ = io.WriteString(w, "done")
	})
	b, _ := newTestBinding(h, nil)
	var ln net.Listener
	b.listen = func(string) (net.Listener, error) {
		var err error
		ln, err = net.Listen("tcp4", "127.0.0.1:0")
		return ln, err
	}
	b.Reconcile(coreconf.Default)
	got := make(chan string, 1)
	go func() {
		resp, err := http.Get("http://" + ln.Addr().String() + "/")
		if err != nil {
			got <- err.Error()
			return
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		got <- string(body)
	}()
	<-entered
	closed := make(chan struct{})
	go func() {
		b.Close()
		close(closed)
	}()
	select {
	case <-closed:
		t.Fatal("Close did not wait for the request in flight")
	case <-time.After(100 * time.Millisecond):
	}
	close(release)
	if body := <-got; body != "done" {
		t.Fatalf("request in flight lost: %q", body)
	}
	<-closed
}

func TestInterfaceIPv4(t *testing.T) {
	if ip, err := InterfaceIPv4("lo"); err != nil || ip != "127.0.0.1" {
		t.Fatalf("lo = %q, %v", ip, err)
	}
	if _, err := InterfaceIPv4("kalinka-missing0"); err == nil {
		t.Fatal("missing interface resolved")
	}
}
