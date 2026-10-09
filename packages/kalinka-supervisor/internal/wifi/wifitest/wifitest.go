// Package wifitest stages a box's interfaces, sysfs and IPv4 route table for
// tests of wifi.Host and of what decides on its answer.
package wifitest

import (
	"kalinka/supervisor/internal/wifi"
	"net"
	"os"
	"path/filepath"
	"testing"
)

// Lines of /proc/net/route for a Wi-Fi LAN, a cable to an amplifier and a wired LAN.
const (
	WlanDefault = "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
	WlanSubnet  = "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n"
	CableSubnet = "eth0\t000A0A0A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
	EthDefault  = "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
	EthSubnet   = "eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
	routeHeader = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
)

// Up is an interface that is up with a link.
const Up = net.FlagUp | net.FlagRunning

// Link is one interface of a staged box; Addresses are in CIDR form.
type Link struct {
	Name      string
	Flags     net.Flags
	Physical  bool
	Wireless  bool
	Addresses []string
}

// Box is a staged host. A test may restage its routes and addresses between
// lookups, never during one.
type Box struct {
	wifi.Host
	addresses map[string][]net.Addr
}

// Stage builds a box under a temporary root with routes as its route table.
func Stage(t testing.TB, routes string, links ...Link) *Box {
	t.Helper()
	b := &Box{addresses: map[string][]net.Addr{}}
	var interfaces []net.Interface
	root := t.TempDir()
	for _, l := range links {
		dir := filepath.Join(root, "sys/class/net", l.Name)
		entries := []string{dir}
		if l.Physical {
			entries = append(entries, filepath.Join(dir, "device"))
		}
		if l.Wireless {
			entries = append(entries, filepath.Join(dir, "phy80211"))
		}
		for _, entry := range entries {
			if err := os.MkdirAll(entry, 0700); err != nil {
				t.Fatal(err)
			}
		}
		interfaces = append(interfaces, net.Interface{Name: l.Name, Flags: l.Flags})
		for _, cidr := range l.Addresses {
			ip, network, err := net.ParseCIDR(cidr)
			if err != nil {
				t.Fatal(err)
			}
			b.addresses[l.Name] = append(b.addresses[l.Name], &net.IPNet{IP: ip, Mask: network.Mask})
		}
	}
	b.Host = wifi.Host{
		Root:       root,
		Interfaces: func() ([]net.Interface, error) { return interfaces, nil },
		Addrs:      func(i *net.Interface) ([]net.Addr, error) { return b.addresses[i.Name], nil },
	}
	b.Route(t, routes)
	return b
}

// Route replaces the box's route table.
func (b *Box) Route(t testing.TB, routes string) {
	t.Helper()
	if err := wifi.AtomicWrite(filepath.Join(b.Root, "proc/net/route"), []byte(routeHeader+routes), 0600); err != nil {
		t.Fatal(err)
	}
}

// Unaddress drops every address of the named interface.
func (b *Box) Unaddress(name string) { delete(b.addresses, name) }
