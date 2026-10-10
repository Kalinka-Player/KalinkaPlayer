package control

import (
	"context"
	"net"
	"net/http"
	"strconv"
	"time"

	"kalinka/supervisor/internal/coreconf"
)

// Binding keeps the control API listening on Core's interface at its own
// port, and never on the port Core is configured to use. Reconcile and Close
// are called from one goroutine.
type Binding struct {
	port    int
	handler http.Handler
	resolve func(iface string) (string, error)
	listen  func(addr string) (net.Listener, error)

	addr   string
	server *http.Server
	served chan struct{}
}

func NewBinding(port int, h http.Handler) *Binding {
	return &Binding{port: port, handler: h, resolve: InterfaceIPv4, listen: func(addr string) (net.Listener, error) {
		return net.Listen("tcp4", addr)
	}}
}

// InterfaceIPv4 returns the first IPv4 address of the named interface, as Core binds it, or "" while it has none.
func InterfaceIPv4(name string) (string, error) {
	iface, err := net.InterfaceByName(name)
	if err != nil {
		return "", err
	}
	addrs, err := iface.Addrs()
	if err != nil {
		return "", err
	}
	for _, a := range addrs {
		if n, ok := a.(*net.IPNet); ok && n.IP.To4() != nil {
			return n.IP.String(), nil
		}
	}
	return "", nil
}

// Reconcile moves the listener to where core says it belongs, and describes the result in a few words.
func (b *Binding) Reconcile(core coreconf.Config) string {
	want, status := b.desired(core)
	if want == b.addr {
		return status
	}
	b.stop()
	if want == "" {
		return status
	}
	ln, err := b.listen(want)
	if err != nil {
		return "cannot listen on " + want
	}
	srv := &http.Server{
		Handler:           b.handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      sshSetupTimeout + 2*phaseTimeout,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    8 << 10,
	}
	served := make(chan struct{})
	go func() {
		defer close(served)
		_ = srv.Serve(ln)
	}()
	b.addr, b.server, b.served = want, srv, served
	return status
}

func (b *Binding) desired(core coreconf.Config) (addr, status string) {
	if core.Port == b.port {
		return "", "leaving port " + strconv.Itoa(b.port) + " to Core"
	}
	host := "0.0.0.0"
	if core.Interface != coreconf.AllInterfaces {
		ip, err := b.resolve(core.Interface)
		if err != nil || ip == "" {
			return "", "waiting for an address on " + core.Interface
		}
		host = ip
	}
	addr = net.JoinHostPort(host, strconv.Itoa(b.port))
	return addr, "listening on " + addr
}

// Close lets requests in flight finish for up to five seconds, then closes the listener.
func (b *Binding) Close() { b.stop() }

func (b *Binding) stop() {
	if b.server == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if b.server.Shutdown(ctx) != nil {
		_ = b.server.Close()
	}
	<-b.served
	b.addr, b.server, b.served = "", nil, nil
}
