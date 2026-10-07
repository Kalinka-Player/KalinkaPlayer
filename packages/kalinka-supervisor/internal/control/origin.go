package control

import (
	"net"
	"net/netip"
	"net/url"
	"strconv"
	"strings"
)

// sharedAddressSpace (RFC 6598) is where VPN overlays such as Tailscale put LAN peers.
var sharedAddressSpace = netip.MustParsePrefix("100.64.0.0/10")

// lanSuffixes are names public DNS cannot answer for, so a rebinding attacker cannot point them at this box.
var lanSuffixes = []string{".local", ".lan", ".home.arpa", ".internal"}

// remoteAllowed keeps the API on the trusted LAN even when someone forwards its port from the internet.
func remoteAllowed(remoteAddr string) bool {
	peer, err := netip.ParseAddrPort(remoteAddr)
	if err != nil {
		return false
	}
	ip := peer.Addr().Unmap()
	return ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || sharedAddressSpace.Contains(ip)
}

func hostname(hostport string) string {
	h := strings.ToLower(hostport)
	if host, _, err := net.SplitHostPort(h); err == nil {
		h = host
	} else if strings.HasPrefix(h, "[") && strings.HasSuffix(h, "]") {
		h = h[1 : len(h)-1]
	}
	return strings.TrimSuffix(h, ".")
}

// hostAllowed refuses names a DNS-rebinding page could have resolved to this box.
func hostAllowed(hostport string) bool {
	h := hostname(hostport)
	if h == "" {
		return false
	}
	if _, err := netip.ParseAddr(h); err == nil {
		return true
	}
	if h == "localhost" || !strings.Contains(h, ".") {
		return true
	}
	for _, suffix := range lanSuffixes {
		if strings.HasSuffix(h, suffix) {
			return true
		}
	}
	return false
}

// originAllowed admits only pages this box serves: the supervisor's own, and Core's on the same host.
func originAllowed(origin, host string, corePort int) bool {
	u, err := url.Parse(origin)
	if err != nil || u.Scheme != "http" && u.Scheme != "https" {
		return false
	}
	name := strings.TrimSuffix(strings.ToLower(u.Hostname()), ".")
	if name == "" || name != hostname(host) {
		return false
	}
	port := u.Port()
	if port == "" {
		port = map[string]string{"http": "80", "https": "443"}[u.Scheme]
	}
	own := "80"
	if _, p, err := net.SplitHostPort(host); err == nil {
		own = p
	}
	return port == strconv.Itoa(corePort) || port == own
}
