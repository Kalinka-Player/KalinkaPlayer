package wifi

import (
	"context"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"time"

	"github.com/godbus/dbus/v5"
	"kalinka/supervisor/internal/protocol"
)

const (
	nm           = "org.freedesktop.NetworkManager"
	nmRoot       = dbus.ObjectPath("/org/freedesktop/NetworkManager")
	nmSettings   = dbus.ObjectPath("/org/freedesktop/NetworkManager/Settings")
	nmDevice     = nm + ".Device"
	nmWireless   = nmDevice + ".Wireless"
	nmConnection = nm + ".Settings.Connection"
)

// Bus limits the D-Bus surface used by NetworkManager and makes it testable.
type Bus interface {
	Call(context.Context, dbus.ObjectPath, string, ...any) ([]any, error)
	Close() error
}
type systemBus struct{ conn *dbus.Conn }

func (b *systemBus) Call(ctx context.Context, path dbus.ObjectPath, method string, args ...any) ([]any, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	c := b.conn.Object(nm, path).CallWithContext(ctx, method, 0, args...)
	if c.Err != nil {
		return nil, protocol.Unavailable
	}
	return c.Body, nil
}
func (b *systemBus) Close() error { return b.conn.Close() }

type settings map[string]map[string]dbus.Variant

func v(x any) dbus.Variant { return dbus.MakeVariant(x) }
func value[T any](props map[string]dbus.Variant, key string) T {
	r, _ := props[key].Value().(T)
	return r
}

// NetworkManager stages a new profile and never edits a pre-existing profile.
type NetworkManager struct {
	Interface, Journal string
	Bus                Bus
	device             dbus.ObjectPath
	Timeout, Poll      time.Duration
}

func NewNetworkManager(iface, state string) *NetworkManager {
	return &NetworkManager{Interface: iface, Journal: filepath.Join(state, "networkmanager-rollback.json"), Timeout: 75 * time.Second, Poll: 500 * time.Millisecond}
}
func (n *NetworkManager) call(ctx context.Context, path dbus.ObjectPath, method string, args ...any) ([]any, error) {
	body, err := n.Bus.Call(ctx, path, method, args...)
	if err != nil {
		return nil, protocol.Unavailable
	}
	return body, nil
}
func (n *NetworkManager) props(ctx context.Context, path dbus.ObjectPath, iface string) (map[string]dbus.Variant, error) {
	body, err := n.call(ctx, path, "org.freedesktop.DBus.Properties.GetAll", iface)
	var p map[string]dbus.Variant
	if err != nil || dbus.Store(body, &p) != nil {
		return nil, protocol.Unavailable
	}
	return p, nil
}
func (n *NetworkManager) Recover(ctx context.Context) error {
	if n.Bus == nil {
		conn, err := dbus.ConnectSystemBus()
		if err != nil {
			return protocol.Unavailable
		}
		n.Bus = &systemBus{conn}
	}
	if n.Interface == "" {
		body, err := n.call(ctx, nmRoot, nm+".GetDevices")
		var paths []dbus.ObjectPath
		if err != nil || dbus.Store(body, &paths) != nil {
			return protocol.Unavailable
		}
		for _, p := range paths {
			props, err := n.props(ctx, p, nmDevice)
			if err == nil && value[uint32](props, "DeviceType") == 2 && value[bool](props, "Managed") {
				n.Interface = value[string](props, "Interface")
				break
			}
		}
	}
	if !interfacePattern.MatchString(n.Interface) {
		return protocol.Unavailable
	}
	body, err := n.call(ctx, nmRoot, nm+".GetDeviceByIpIface", n.Interface)
	if err != nil || dbus.Store(body, &n.device) != nil {
		return protocol.Unavailable
	}
	p, err := n.props(ctx, n.device, nmDevice)
	if err != nil || value[uint32](p, "DeviceType") != 2 || !value[bool](p, "Managed") {
		return protocol.Unavailable
	}
	return n.rollback(ctx)
}
func (n *NetworkManager) Address(ctx context.Context) (string, error) { return LANAddress(ctx, "") }
func (n *NetworkManager) deviceAddress(ctx context.Context) (string, error) {
	p, err := n.props(ctx, n.device, nmDevice)
	if err != nil {
		return "", err
	}
	path := value[dbus.ObjectPath](p, "Ip4Config")
	if value[uint32](p, "State") != 100 || path == "/" || path == "" {
		return "", nil
	}
	p, err = n.props(ctx, path, nm+".IP4Config")
	if err != nil {
		return "", err
	}
	for _, a := range value[[]map[string]dbus.Variant](p, "AddressData") {
		ip := net.ParseIP(value[string](a, "address"))
		if ip.To4() != nil && ip.IsGlobalUnicast() {
			return ip.String(), nil
		}
	}
	return "", nil
}
func (n *NetworkManager) Scan(ctx context.Context, _ string) ([]protocol.Network, error) {
	p, err := n.props(ctx, n.device, nmWireless)
	if err != nil {
		return nil, err
	}
	previous := value[int64](p, "LastScan")
	if _, err = n.call(ctx, n.device, nmWireless+".RequestScan", map[string]dbus.Variant{}); err == nil {
		deadline := time.Now().Add(12 * time.Second)
		for time.Now().Before(deadline) {
			p, err = n.props(ctx, n.device, nmWireless)
			if err != nil {
				return nil, err
			}
			if value[int64](p, "LastScan") != previous {
				break
			}
			if err = Pause(ctx, n.Poll); err != nil {
				return nil, err
			}
		}
	}
	body, err := n.call(ctx, n.device, nmWireless+".GetAllAccessPoints")
	var paths []dbus.ObjectPath
	if err != nil || dbus.Store(body, &paths) != nil {
		return nil, protocol.Unavailable
	}
	var result []protocol.Network
	for _, path := range paths {
		p, err = n.props(ctx, path, nm+".AccessPoint")
		if err != nil {
			continue
		}
		security := "open"
		if value[uint32](p, "RsnFlags")&0x100 != 0 {
			security = "wpa2"
		} else if value[uint32](p, "Flags")&1 != 0 || value[uint32](p, "WpaFlags") != 0 || value[uint32](p, "RsnFlags") != 0 {
			security = "unsupported"
		}
		result = append(result, protocol.Network{SSID: string(value[[]byte](p, "Ssid")), Signal: int(value[byte](p, "Strength"))/2 - 100, Security: security})
	}
	return strongest(result), nil
}
func (n *NetworkManager) profile(ctx context.Context, id string) (dbus.ObjectPath, error) {
	body, err := n.call(ctx, nmSettings, nm+".Settings.ListConnections")
	var paths []dbus.ObjectPath
	if err != nil || dbus.Store(body, &paths) != nil {
		return "", protocol.Unavailable
	}
	for _, path := range paths {
		body, err = n.call(ctx, path, nmConnection+".GetSettings")
		var s settings
		if err != nil || dbus.Store(body, &s) != nil {
			return "", protocol.Unavailable
		}
		if value[string](s["connection"], "uuid") == id {
			return path, nil
		}
	}
	return "", nil
}

type nmRecord struct {
	Candidate string `json:"candidate"`
	Previous  string `json:"previous"`
}

func (n *NetworkManager) rollback(ctx context.Context) error {
	b, err := os.ReadFile(n.Journal)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return protocol.Storage
	}
	var record nmRecord
	if json.Unmarshal(b, &record) != nil || record.Candidate == "" {
		return protocol.Storage
	}
	candidate, err := n.profile(ctx, record.Candidate)
	if err != nil {
		return err
	}
	if candidate != "" {
		if _, err = n.call(ctx, candidate, nmConnection+".Delete"); err != nil {
			return err
		}
	}
	if record.Previous != "" {
		previous, err := n.profile(ctx, record.Previous)
		if err != nil || previous == "" {
			return protocol.Unavailable
		}
		if _, err = n.call(ctx, nmRoot, nm+".ActivateConnection", previous, n.device, dbus.ObjectPath("/")); err != nil {
			return err
		}
	}
	return removeDurable(n.Journal)
}
func (n *NetworkManager) Join(ctx context.Context, c protocol.Command, progress func(byte)) (address string, result error) {
	if err := n.rollback(ctx); err != nil {
		return "", err
	}
	p, err := n.props(ctx, n.device, nmDevice)
	if err != nil {
		return "", err
	}
	var previous string
	if path := value[dbus.ObjectPath](p, "ActiveConnection"); path != "" && path != "/" {
		p, err = n.props(ctx, path, nm+".Connection.Active")
		if err != nil {
			return "", err
		}
		previous = value[string](p, "Uuid")
	}
	id := UUID()
	s := settings{
		"connection":               {"id": v("Kalinka setup " + id[:8]), "uuid": v(id), "type": v("802-11-wireless"), "interface-name": v(n.Interface), "autoconnect": v(false), "permissions": v([]string{})},
		"802-11-wireless":          {"ssid": v([]byte(c.SSID)), "mode": v("infrastructure"), "hidden": v(true)},
		"802-11-wireless-security": {"key-mgmt": v("wpa-psk"), "proto": v([]string{"rsn"}), "psk": v(c.Password.Reveal()), "psk-flags": v(uint32(0))},
		"ipv4":                     {"method": v("auto"), "dhcp-timeout": v(int32(30))}, "ipv6": {"method": v("auto"), "may-fail": v(true)}}
	defer clear(s["802-11-wireless-security"])
	if err = writeJSON(n.Journal, nmRecord{id, previous}); err != nil {
		return "", err
	}
	defer func() {
		if result != nil {
			rollback, cancel := context.WithTimeout(context.Background(), 45*time.Second)
			defer cancel()
			if n.rollback(rollback) != nil {
				result = protocol.Storage
			}
		}
	}()
	body, err := n.call(ctx, nmSettings, nm+".Settings.AddConnection2", s, uint32(2), map[string]dbus.Variant{})
	var candidate dbus.ObjectPath
	var ignored map[string]dbus.Variant
	if err != nil || dbus.Store(body, &candidate, &ignored) != nil {
		return "", protocol.Unavailable
	}
	body, err = n.call(ctx, nmRoot, nm+".ActivateConnection", candidate, n.device, dbus.ObjectPath("/"))
	var active dbus.ObjectPath
	if err != nil || dbus.Store(body, &active) != nil {
		return "", protocol.Unavailable
	}
	progress(protocol.Authenticating)
	deadline := time.Now().Add(n.Timeout)
	associated := false
	var state uint32
	for time.Now().Before(deadline) {
		p, err = n.props(ctx, n.device, nmDevice)
		if err != nil {
			return "", err
		}
		var reason struct{ State, Reason uint32 }
		if p["StateReason"].Store(&reason) != nil {
			return "", protocol.Unavailable
		}
		state = reason.State
		if state == 120 {
			switch reason.Reason {
			case 7, 9, 10, 11:
				return "", protocol.WrongPassword
			case 5, 6, 15, 16, 17:
				return "", protocol.NoAddress
			default:
				return "", protocol.Timeout
			}
		}
		if value[dbus.ObjectPath](p, "ActiveConnection") == active {
			if state >= 70 && state <= 100 {
				associated = true
				progress(protocol.GettingAddress)
			}
			if state == 100 {
				address, err = n.deviceAddress(ctx)
				if err != nil {
					return "", err
				}
				if address != "" {
					break
				}
			}
		}
		if err = Pause(ctx, n.Poll); err != nil {
			return "", err
		}
	}
	if address == "" {
		if associated {
			return "", protocol.NoAddress
		}
		if state == 60 {
			return "", protocol.WrongPassword
		}
		return "", protocol.Timeout
	}
	progress(protocol.Saving)
	s["connection"]["autoconnect"] = v(true)
	if _, err = n.call(ctx, candidate, nmConnection+".Update2", s, uint32(1), map[string]dbus.Variant{}); err != nil {
		return "", protocol.Storage
	}
	if err = removeDurable(n.Journal); err != nil {
		return "", err
	}
	return address, nil
}
func (n *NetworkManager) Close() error {
	if n.Bus != nil {
		return n.Bus.Close()
	}
	return nil
}
