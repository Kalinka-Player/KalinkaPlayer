// Package gatt exposes only the provisioning contract on the shared BlueZ adapter.
package gatt

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/godbus/dbus/v5"
	"github.com/godbus/dbus/v5/introspect"
	"kalinka/supervisor/internal/machine"
	"kalinka/supervisor/internal/protocol"
)

const (
	app        = dbus.ObjectPath("/org/kalinka/supervisor")
	service    = dbus.ObjectPath(string(app) + "/service0")
	advert     = dbus.ObjectPath("/org/kalinka/supervisor_advert")
	agentPath  = dbus.ObjectPath("/org/kalinka/supervisor_agent")
	properties = "org.freedesktop.DBus.Properties"
)

type object struct {
	iface string
	get   func() map[string]dbus.Variant
}

func (o *object) Get(iface, key string) (dbus.Variant, *dbus.Error) {
	if iface != o.iface {
		return dbus.Variant{}, dbus.NewError("org.freedesktop.DBus.Error.UnknownInterface", nil)
	}
	value, ok := o.get()[key]
	if !ok {
		return dbus.Variant{}, dbus.NewError("org.freedesktop.DBus.Error.UnknownProperty", nil)
	}
	return value, nil
}
func (o *object) GetAll(iface string) (map[string]dbus.Variant, *dbus.Error) {
	if iface != o.iface {
		return nil, dbus.NewError("org.freedesktop.DBus.Error.UnknownInterface", nil)
	}
	return o.get(), nil
}
func (o *object) Set(string, string, dbus.Variant) *dbus.Error {
	return dbus.NewError("org.freedesktop.DBus.Error.PropertyReadOnly", nil)
}
func errorCode(name, message string) *dbus.Error {
	return dbus.NewError("org.bluez.Error."+name, []any{message})
}
func denied() *dbus.Error { return errorCode("NotPermitted", "Setup unavailable") }
func option[T any](opts map[string]dbus.Variant, key string, fallback T) T {
	if value, ok := opts[key]; ok {
		if result, ok := value.Value().(T); ok {
			return result
		}
	}
	return fallback
}

// Bluez owns its registrations, never the adapter power or other audio services.
type Bluez struct {
	conn                                     *dbus.Conn
	owner                                    string
	adapter                                  dbus.ObjectPath
	machine                                  *machine.Machine
	objects                                  map[dbus.ObjectPath]*object
	chars                                    []*characteristic
	status                                   *characteristic
	signals                                  chan *dbus.Signal
	name                                     string
	mu                                       sync.Mutex
	registered, advertising, agentRegistered bool
	oldAlias                                 string
	oldPairable                              bool
	changedAdapter                           bool
}

func New(ctx context.Context, m *machine.Machine, adapter string) (*Bluez, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	if adapter == "" || strings.ContainsAny(adapter, "/\x00") {
		return nil, protocol.Invalid
	}
	conn, err := dbus.ConnectSystemBus()
	if err != nil {
		return nil, protocol.Unavailable
	}
	b := &Bluez{conn: conn, machine: m, adapter: dbus.ObjectPath("/org/bluez/" + adapter), objects: map[dbus.ObjectPath]*object{}, signals: make(chan *dbus.Signal, 32)}
	fail := func() (*Bluez, error) { _ = conn.Close(); return nil, protocol.Unavailable }
	if conn.BusObject().CallWithContext(ctx, "org.freedesktop.DBus.GetNameOwner", 0, "org.bluez").Store(&b.owner) != nil {
		return fail()
	}
	address, err := b.get(ctx, "Address")
	if err != nil {
		return fail()
	}
	s, ok := address.Value().(string)
	if !ok {
		return fail()
	}
	s = strings.ReplaceAll(s, ":", "")
	if len(s) < 4 {
		return fail()
	}
	b.name = "Kalinka-" + s[len(s)-4:]
	if err = b.export(service, "org.bluez.GattService1", nil, func() map[string]dbus.Variant {
		return map[string]dbus.Variant{"UUID": dbus.MakeVariant(protocol.ServiceUUID), "Primary": dbus.MakeVariant(true)}
	}); err != nil {
		return fail()
	}
	for _, spec := range []struct {
		name, uuid       string
		get              func() []byte
		snapshot, notify bool
	}{{"status", protocol.StatusUUID, m.Status, false, true}, {"identity", protocol.IdentityUUID, m.Identity, false, false}, {"networks", protocol.NetworksUUID, m.Networks, true, false}, {"progress", protocol.ProgressUUID, m.Progress, false, false}} {
		c := &characteristic{bluez: b, uuid: spec.uuid, path: dbus.ObjectPath(string(service) + "/" + spec.name), get: spec.get, snapshot: spec.snapshot, notify: spec.notify, reads: map[string][]byte{}}
		if err = b.export(c.path, "org.bluez.GattCharacteristic1", c, c.props); err != nil {
			return fail()
		}
		b.chars = append(b.chars, c)
		if spec.name == "status" {
			b.status = c
		}
	}
	c := &command{bluez: b}
	if err = b.export(dbus.ObjectPath(string(service)+"/command"), "org.bluez.GattCharacteristic1", c, func() map[string]dbus.Variant {
		return map[string]dbus.Variant{"UUID": dbus.MakeVariant(protocol.CommandUUID), "Service": dbus.MakeVariant(service), "Flags": dbus.MakeVariant([]string{"write", "encrypt-write"})}
	}); err != nil {
		return fail()
	}
	if err = conn.Export(&manager{b}, app, "org.freedesktop.DBus.ObjectManager"); err != nil {
		return fail()
	}
	if err = b.export(advert, "org.bluez.LEAdvertisement1", &advertisement{b}, func() map[string]dbus.Variant {
		return map[string]dbus.Variant{"Type": dbus.MakeVariant("peripheral"), "ServiceUUIDs": dbus.MakeVariant([]string{protocol.ServiceUUID}), "LocalName": dbus.MakeVariant(b.name)}
	}); err != nil {
		return fail()
	}
	if err = conn.Export(&agent{b}, agentPath, "org.bluez.Agent1"); err != nil {
		return fail()
	}
	conn.Signal(b.signals)
	if err = conn.AddMatchSignal(dbus.WithMatchSender("org.bluez"), dbus.WithMatchInterface(properties), dbus.WithMatchMember("PropertiesChanged")); err != nil {
		return fail()
	}
	if err = conn.AddMatchSignal(dbus.WithMatchSender("org.freedesktop.DBus"), dbus.WithMatchInterface("org.freedesktop.DBus"), dbus.WithMatchMember("NameOwnerChanged"), dbus.WithMatchArg(0, "org.bluez")); err != nil {
		return fail()
	}
	return b, nil
}
func (b *Bluez) export(path dbus.ObjectPath, iface string, impl any, get func() map[string]dbus.Variant) error {
	o := &object{iface, get}
	b.objects[path] = o
	if err := b.conn.Export(o, path, properties); err != nil {
		return err
	}
	if impl != nil {
		if err := b.conn.Export(impl, path, iface); err != nil {
			return err
		}
	}
	var methods []introspect.Method
	if impl != nil {
		methods = introspect.Methods(impl)
	}
	return b.conn.Export(introspect.NewIntrospectable(&introspect.Node{Interfaces: []introspect.Interface{{Name: iface, Methods: methods}, {Name: properties, Methods: introspect.Methods(o)}}}), path, "org.freedesktop.DBus.Introspectable")
}
func (b *Bluez) call(parent context.Context, path dbus.ObjectPath, method string, args ...any) error {
	ctx, cancel := context.WithTimeout(parent, 10*time.Second)
	defer cancel()
	if b.conn.Object("org.bluez", path).CallWithContext(ctx, method, 0, args...).Err != nil {
		return protocol.Unavailable
	}
	return nil
}
func (b *Bluez) get(parent context.Context, key string) (dbus.Variant, error) {
	ctx, cancel := context.WithTimeout(parent, 5*time.Second)
	defer cancel()
	var v dbus.Variant
	if b.conn.Object("org.bluez", b.adapter).CallWithContext(ctx, properties+".Get", 0, "org.bluez.Adapter1", key).Store(&v) != nil {
		return v, protocol.Unavailable
	}
	return v, nil
}
func (b *Bluez) set(ctx context.Context, key string, value any) error {
	return b.call(ctx, b.adapter, properties+".Set", "org.bluez.Adapter1", key, dbus.MakeVariant(value))
}

// Name is what setup advertises: Kalinka- and the last four hex digits of the adapter's address.
func (b *Bluez) Name() string { return b.name }
func (b *Bluez) Enable(ctx context.Context) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.advertising {
		return nil
	}
	alias, err := b.get(ctx, "Alias")
	if err != nil {
		return err
	}
	pairable, err := b.get(ctx, "Pairable")
	if err != nil {
		return err
	}
	b.oldAlias, _ = alias.Value().(string)
	b.oldPairable, _ = pairable.Value().(bool)
	b.changedAdapter = true
	if err = b.set(ctx, "Powered", true); err != nil {
		return err
	}
	if err = b.set(ctx, "Alias", b.name); err != nil {
		return err
	}
	if err = b.set(ctx, "Pairable", true); err != nil {
		return err
	}
	if err = b.call(ctx, "/org/bluez", "org.bluez.AgentManager1.RegisterAgent", agentPath, "NoInputNoOutput"); err != nil {
		return err
	}
	b.agentRegistered = true
	if err = b.call(ctx, "/org/bluez", "org.bluez.AgentManager1.RequestDefaultAgent", agentPath); err != nil {
		return err
	}
	if err = b.call(ctx, b.adapter, "org.bluez.GattManager1.RegisterApplication", app, map[string]dbus.Variant{}); err != nil {
		return err
	}
	b.registered = true
	if err = b.call(ctx, b.adapter, "org.bluez.LEAdvertisingManager1.RegisterAdvertisement", advert, map[string]dbus.Variant{}); err != nil {
		return err
	}
	b.advertising = true
	return nil
}
func (b *Bluez) Disable(ctx context.Context) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.advertising {
		_ = b.call(ctx, b.adapter, "org.bluez.LEAdvertisingManager1.UnregisterAdvertisement", advert)
	}
	if b.registered {
		_ = b.call(ctx, b.adapter, "org.bluez.GattManager1.UnregisterApplication", app)
	}
	if b.agentRegistered {
		_ = b.call(ctx, "/org/bluez", "org.bluez.AgentManager1.UnregisterAgent", agentPath)
	}
	if b.changedAdapter {
		alias, err := b.get(ctx, "Alias")
		if err == nil && alias.Value() == b.name {
			_ = b.set(ctx, "Alias", b.oldAlias)
			_ = b.set(ctx, "Pairable", b.oldPairable)
		}
	}
	b.advertising = false
	b.registered = false
	b.agentRegistered = false
	b.changedAdapter = false
	for _, c := range b.chars {
		c.notifying.Store(false)
		c.mu.Lock()
		clear(c.reads)
		c.mu.Unlock()
	}
}
func (b *Bluez) Close() {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	b.Disable(ctx)
	b.conn.RemoveSignal(b.signals)
	_ = b.conn.Close()
}

// Events must be serviced even when provisioning is not advertising.
func (b *Bluez) Events(ctx context.Context) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-b.conn.Context().Done():
			return protocol.Unavailable
		case signal, ok := <-b.signals:
			if !ok {
				return protocol.Unavailable
			}
			if signal.Name == "org.freedesktop.DBus.NameOwnerChanged" {
				if len(signal.Body) == 3 && signal.Body[0] == "org.bluez" && signal.Body[2] != b.owner {
					return protocol.Unavailable
				}
				continue
			}
			if signal.Sender != b.owner || len(signal.Body) != 3 || signal.Body[0] != "org.bluez.Device1" {
				continue
			}
			p, ok := signal.Body[1].(map[string]dbus.Variant)
			if !ok {
				continue
			}
			if v, ok := p["Connected"]; ok && v.Value() == false {
				b.machine.Disconnected(string(signal.Path))
			}
		case <-b.machine.Changes:
			if b.status.notifying.Load() {
				_ = b.conn.Emit(b.status.path, properties+".PropertiesChanged", "org.bluez.GattCharacteristic1", map[string]dbus.Variant{"Value": dbus.MakeVariant([]byte{1})}, []string{})
			}
		}
	}
}

type manager struct{ b *Bluez }

func (m *manager) GetManagedObjects() (map[dbus.ObjectPath]map[string]map[string]dbus.Variant, *dbus.Error) {
	result := map[dbus.ObjectPath]map[string]map[string]dbus.Variant{}
	for p, o := range m.b.objects {
		if strings.HasPrefix(string(p), string(service)) {
			result[p] = map[string]map[string]dbus.Variant{o.iface: o.get()}
		}
	}
	return result, nil
}

type characteristic struct {
	bluez            *Bluez
	uuid             string
	path             dbus.ObjectPath
	get              func() []byte
	snapshot, notify bool
	notifying        atomic.Bool
	mu               sync.Mutex
	reads            map[string][]byte
}

func (c *characteristic) props() map[string]dbus.Variant {
	flags := []string{"read", "encrypt-read"}
	if c.notify {
		flags = append(flags, "notify")
	}
	return map[string]dbus.Variant{"UUID": dbus.MakeVariant(c.uuid), "Service": dbus.MakeVariant(service), "Flags": dbus.MakeVariant(flags), "Value": dbus.MakeVariant([]byte{1}), "Notifying": dbus.MakeVariant(c.notifying.Load())}
}
func (c *characteristic) ReadValue(sender dbus.Sender, opts map[string]dbus.Variant) ([]byte, *dbus.Error) {
	if string(sender) != c.bluez.owner {
		return nil, denied()
	}
	device := string(option(opts, "device", dbus.ObjectPath("")))
	if c.bluez.machine.Authorize(device) != nil {
		return nil, denied()
	}
	offset := int(option(opts, "offset", uint16(0)))
	value := c.get()
	if c.snapshot {
		c.mu.Lock()
		defer c.mu.Unlock()
		if offset == 0 {
			if len(c.reads) >= 16 {
				clear(c.reads)
			}
			c.reads[device] = value
		}
		var ok bool
		value, ok = c.reads[device]
		if !ok {
			return nil, errorCode("InvalidOffset", "Read from offset zero")
		}
	}
	if offset > len(value) {
		return nil, errorCode("InvalidOffset", "Invalid offset")
	}
	return value[offset:], nil
}
func (c *characteristic) StartNotify(sender dbus.Sender) *dbus.Error {
	if string(sender) != c.bluez.owner || !c.notify {
		return denied()
	}
	c.notifying.Store(true)
	_ = c.bluez.conn.Emit(c.path, properties+".PropertiesChanged", "org.bluez.GattCharacteristic1", map[string]dbus.Variant{"Notifying": dbus.MakeVariant(true)}, []string{})
	return nil
}
func (c *characteristic) StopNotify(sender dbus.Sender) *dbus.Error {
	if string(sender) != c.bluez.owner {
		return denied()
	}
	c.notifying.Store(false)
	return nil
}

type command struct{ bluez *Bluez }

func (c *command) WriteValue(sender dbus.Sender, value []byte, opts map[string]dbus.Variant) *dbus.Error {
	if string(sender) != c.bluez.owner {
		return denied()
	}
	if option(opts, "offset", uint16(0)) != 0 || option(opts, "prepare-authorize", false) {
		return errorCode("InvalidOffset", "Use framed writes")
	}
	if option(opts, "link", "LE") != "LE" {
		return denied()
	}
	err := c.bluez.machine.Write(string(option(opts, "device", dbus.ObjectPath(""))), value)
	if err == nil {
		return nil
	}
	if err == protocol.Busy {
		return errorCode("InProgress", "busy")
	}
	return errorCode("NotPermitted", fmt.Sprint(err))
}

type advertisement struct{ b *Bluez }

func (a *advertisement) Release(sender dbus.Sender) *dbus.Error {
	if string(sender) != a.b.owner {
		return denied()
	}
	return nil
}

type agent struct{ b *Bluez }

func (a *agent) authorize(sender dbus.Sender, device dbus.ObjectPath) *dbus.Error {
	if string(sender) != a.b.owner || a.b.machine.Authorize(string(device)) != nil {
		return errorCode("Rejected", "Setup unavailable")
	}
	return nil
}
func (a *agent) Release(sender dbus.Sender) *dbus.Error {
	if string(sender) != a.b.owner {
		return denied()
	}
	return nil
}
func (a *agent) Cancel(sender dbus.Sender) *dbus.Error {
	if string(sender) != a.b.owner {
		return denied()
	}
	return nil
}
func (a *agent) RequestAuthorization(sender dbus.Sender, device dbus.ObjectPath) *dbus.Error {
	return a.authorize(sender, device)
}
func (a *agent) RequestConfirmation(sender dbus.Sender, device dbus.ObjectPath, _ uint32) *dbus.Error {
	return a.authorize(sender, device)
}
func (a *agent) AuthorizeService(sender dbus.Sender, device dbus.ObjectPath, uuid string) *dbus.Error {
	if strings.ToLower(uuid) != protocol.ServiceUUID {
		return errorCode("Rejected", "Provisioning only")
	}
	return a.authorize(sender, device)
}
