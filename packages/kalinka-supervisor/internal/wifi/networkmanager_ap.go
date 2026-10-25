package wifi

import (
	"context"
	"os"
	"path/filepath"
	"time"

	"github.com/godbus/dbus/v5"
	"kalinka/supervisor/internal/protocol"
)

func (n *NetworkManager) APInterface() string { return n.Interface }
func (n *NetworkManager) apRecord() string {
	return filepath.Join(filepath.Dir(n.Journal), "hotspot-uuid")
}

func (n *NetworkManager) StartAP(ctx context.Context, name string) (result error) {
	if !protocol.ValidSSID(name) {
		return protocol.Invalid
	}
	if err := n.StopAP(ctx); err != nil {
		return err
	}
	id := UUID()
	if err := AtomicWrite(n.apRecord(), []byte(id), 0600); err != nil {
		return err
	}
	defer func() {
		if result != nil {
			cleanup, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			_ = n.StopAP(cleanup)
		}
	}()
	// Not the box's own address: a router that saw it serve this AP can stop bridging it between bands.
	s := settings{
		"connection":               {"id": v("Kalinka temporary setup"), "uuid": v(id), "stable-id": v("kalinka-setup-hotspot"), "type": v("802-11-wireless"), "interface-name": v(n.Interface), "autoconnect": v(false)},
		"802-11-wireless":          {"ssid": v([]byte(name)), "mode": v("ap"), "band": v("bg"), "channel": v(uint32(1)), "assigned-mac-address": v("stable")},
		"802-11-wireless-security": {"key-mgmt": v("wpa-psk"), "proto": v([]string{"rsn"}), "psk": v(APPassword)},
		"ipv4":                     {"method": v("manual"), "address-data": v([]map[string]dbus.Variant{{"address": v(APAddress), "prefix": v(uint32(24))}}), "never-default": v(true)},
		"ipv6":                     {"method": v("disabled")},
	}
	body, err := n.call(ctx, nmSettings, nm+".Settings.AddConnection2", s, uint32(2), map[string]dbus.Variant{})
	var candidate dbus.ObjectPath
	var ignored map[string]dbus.Variant
	if err != nil || dbus.Store(body, &candidate, &ignored) != nil {
		return protocol.Unavailable
	}
	if _, err = n.call(ctx, nmRoot, nm+".ActivateConnection", candidate, n.device, dbus.ObjectPath("/")); err != nil {
		return err
	}
	wait, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	for {
		address, err := n.deviceAddress(wait)
		if err != nil {
			return err
		}
		if address == APAddress {
			n.apDNS, err = startAPDNS(ctx, n.Interface, n.Runtime, n.StartAPProcess)
			return err
		}
		if err = Pause(wait, n.Poll); err != nil {
			return protocol.Unavailable
		}
	}
}

func (n *NetworkManager) StopAP(ctx context.Context) error {
	if n.apDNS != nil {
		n.apDNS.Stop()
		n.apDNS = nil
	}
	id, err := os.ReadFile(n.apRecord())
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil || len(id) != 36 {
		return protocol.Storage
	}
	path, err := n.profile(ctx, string(id))
	if err != nil {
		return err
	}
	if path != "" {
		p, err := n.props(ctx, n.device, nmDevice)
		if err != nil {
			return err
		}
		active := value[dbus.ObjectPath](p, "ActiveConnection")
		if active != "" && active != "/" {
			props, err := n.props(ctx, active, nm+".Connection.Active")
			if err != nil {
				return err
			}
			if value[string](props, "Uuid") == string(id) {
				if _, err = n.call(ctx, nmRoot, nm+".DeactivateConnection", active); err != nil {
					return err
				}
				wait, cancel := context.WithTimeout(ctx, 10*time.Second)
				defer cancel()
				for {
					props, err := n.props(wait, n.device, nmDevice)
					if err != nil {
						return err
					}
					if value[dbus.ObjectPath](props, "ActiveConnection") != active {
						break
					}
					if err := Pause(wait, n.Poll); err != nil {
						return protocol.Unavailable
					}
				}
			}
		}
		if _, err = n.call(ctx, path, nmConnection+".Delete"); err != nil {
			return err
		}
	}
	return removeDurable(n.apRecord())
}

func (n *NetworkManager) APHealthy(ctx context.Context) bool {
	if n.apDNS == nil || !n.apDNS.Alive() {
		return false
	}
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	address, err := n.deviceAddress(ctx)
	return err == nil && address == APAddress
}
