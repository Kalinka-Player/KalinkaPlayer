package wifi_test

import (
	"context"
	"kalinka/supervisor/internal/wifi"
	"kalinka/supervisor/internal/wifi/wifitest"
	"net"
	"os"
	"path/filepath"
	"testing"
)

var (
	loopback    = wifitest.Link{Name: "lo", Flags: wifitest.Up | net.FlagLoopback, Addresses: []string{"127.0.0.1/8"}}
	cable       = wifitest.Link{Name: "eth0", Flags: wifitest.Up, Physical: true, Addresses: []string{"10.10.10.1/24"}}
	wlan        = wifitest.Link{Name: "wlan0", Flags: wifitest.Up, Physical: true, Wireless: true, Addresses: []string{"192.168.1.20/24"}}
	unaddressed = wifitest.Link{Name: "wlan0", Flags: wifitest.Up, Physical: true, Wireless: true}
)

func TestAddressCountsOnlyARoutedPhysicalInterface(t *testing.T) {
	for _, c := range []struct {
		name, routes, device, want string
		links                      []wifitest.Link
	}{
		{"Wi-Fi beside a cable to an amplifier", wifitest.WlanDefault + wifitest.CableSubnet + wifitest.WlanSubnet, "", "192.168.1.20", []wifitest.Link{loopback, cable, wlan}},
		{"routed Wi-Fi without an address beside the cable", wifitest.WlanDefault + wifitest.CableSubnet, "", "", []wifitest.Link{loopback, cable, unaddressed}},
		{"only the cable once Wi-Fi breaks", wifitest.CableSubnet, "", "", []wifitest.Link{loopback, cable, unaddressed}},
		{"single interface", wifitest.WlanDefault + wifitest.WlanSubnet, "", "192.168.1.20", []wifitest.Link{loopback, wlan}},
		{"Wi-Fi on a LAN without a gateway", wifitest.CableSubnet + wifitest.WlanSubnet, "", "192.168.1.20", []wifitest.Link{loopback, cable, wlan}},
		{"Wi-Fi under a VPN holding the default route", "tun0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n" + wifitest.WlanSubnet, "", "192.168.1.20", []wifitest.Link{loopback, {Name: "tun0", Flags: wifitest.Up, Addresses: []string{"10.8.0.2/24"}}, wlan}},
		{"default route on loopback", "lo\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n", "", "", []wifitest.Link{{Name: "lo", Flags: wifitest.Up | net.FlagLoopback, Physical: true, Addresses: []string{"192.0.2.9/32"}}}},
		{"default route on a virtual interface", "tun0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n", "", "", []wifitest.Link{loopback, {Name: "tun0", Flags: wifitest.Up, Addresses: []string{"10.8.0.2/24"}}}},
		{"half of the address space is no default route", "eth0\t00000000\t010A0A0A\t0003\t0\t0\t0\t00000080\t0\t0\t0\n" + wifitest.CableSubnet, "", "", []wifitest.Link{loopback, cable}},
		{"named device without a default route", wifitest.WlanDefault + wifitest.CableSubnet + wifitest.WlanSubnet, "eth0", "10.10.10.1", []wifitest.Link{loopback, cable, wlan}},
	} {
		t.Run(c.name, func(t *testing.T) {
			box := wifitest.Stage(t, c.routes, c.links...)
			if got, err := box.Address(context.Background(), c.device); err != nil || got != c.want {
				t.Fatalf("address %q error %v, want %q", got, err, c.want)
			}
		})
	}
}
func TestUnreadableRoutesLeaveWiFiToDecide(t *testing.T) {
	for _, c := range []struct {
		name, want string
		links      []wifitest.Link
	}{
		{"cable alone", "", []wifitest.Link{cable}},
		{"Wi-Fi beside the cable", "192.168.1.20", []wifitest.Link{cable, wlan}},
	} {
		t.Run(c.name, func(t *testing.T) {
			box := wifitest.Stage(t, "", c.links...)
			if err := os.Remove(filepath.Join(box.Root, "proc/net/route")); err != nil {
				t.Fatal(err)
			}
			if got, err := box.Address(context.Background(), ""); err != nil || got != c.want {
				t.Fatalf("address %q error %v, want %q", got, err, c.want)
			}
			if got, err := box.Address(context.Background(), "eth0"); err != nil || got != "10.10.10.1" {
				t.Fatalf("named device %q error %v", got, err)
			}
		})
	}
}
func TestBackendsCountAnyRoutedInterface(t *testing.T) {
	box := wifitest.Stage(t, wifitest.EthDefault+wifitest.EthSubnet,
		wifitest.Link{Name: "eth0", Flags: wifitest.Up, Physical: true, Addresses: []string{"192.168.1.30/24"}},
		wifitest.Link{Name: "wlan0", Flags: net.FlagUp, Physical: true, Wireless: true})
	d, err := wifi.NewDietPi("wlan0", t.TempDir(), t.TempDir(), t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	n := wifi.NewNetworkManager("wlan0", t.TempDir())
	d.AddressFor, n.AddressFor = box.Address, box.Address
	for _, b := range []wifi.Backend{d, n} {
		if got, err := b.Address(context.Background()); err != nil || got != "192.168.1.30" {
			t.Fatalf("%T address %q error %v", b, got, err)
		}
	}
}
