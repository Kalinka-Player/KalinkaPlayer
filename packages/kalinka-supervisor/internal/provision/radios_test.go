package provision

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"kalinka/supervisor/internal/machine"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/rfkill/rfkilltest"
	"kalinka/supervisor/internal/wifi"
)

type radioBackend struct {
	address string
	enables atomic.Int32
}

func (b *radioBackend) Address(context.Context) (string, error)                  { return b.address, nil }
func (b *radioBackend) Scan(context.Context, string) ([]protocol.Network, error) { return nil, nil }
func (b *radioBackend) Join(context.Context, protocol.Command, func(byte)) (string, error) {
	return "", protocol.Unavailable
}
func (b *radioBackend) Recover(context.Context) error         { return nil }
func (b *radioBackend) Close() error                          { return nil }
func (b *radioBackend) EnableRadio(context.Context) error     { b.enables.Add(1); return nil }
func (b *radioBackend) StartAP(context.Context, string) error { return nil }
func (b *radioBackend) StopAP(context.Context) error          { return nil }
func (b *radioBackend) APInterface() string                   { return "wlan0" }

func waitRecovery(t *testing.T, r *radioRecovery) {
	t.Helper()
	if r.done == nil {
		t.Fatal("recovery did not start")
	}
	select {
	case <-r.done:
	case <-time.After(time.Second):
		t.Fatal("recovery did not finish")
	}
}

func TestRadioRecoveryPolicy(t *testing.T) {
	for _, tc := range []struct {
		name, policy                 string
		known, test, alwaysAdvertise bool
		want                         bool
	}{
		{"default on a new install", "", false, false, false, false},
		{"off on a new install", "off", false, false, false, false},
		{"appliance first run", "first-run", false, false, false, true},
		{"configured appliance after reboot", "first-run", true, false, false, false},
		{"opted-in recovery", "always", true, false, false, true},
		{"simulated Wi-Fi", "always", false, true, false, false},
		{"live laptop test", "always", false, false, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			now := time.Unix(1700000000, 0)
			marker := filepath.Join(t.TempDir(), "networked")
			if tc.known {
				if err := os.WriteFile(marker, nil, 0600); err != nil {
					t.Fatal(err)
				}
			}
			b := &radioBackend{}
			m := machine.New(context.Background(), b, machine.Config{Marker: marker, Now: func() time.Time { return now }, Test: tc.test, AlwaysAdvertise: tc.alwaysAdvertise})
			defer m.Close()
			s := New(Options{RadioRecovery: tc.policy, Test: tc.test, AlwaysAdvertise: tc.alwaysAdvertise, DisableBLE: true}, nil)
			r := newRadioRecovery(s.radioSteps(m)...)
			r.settle, r.retry = 0, 0
			defer r.close()
			// A short outage/boot wait is never permission, even with recovery enabled.
			for i := 0; i < 10; i++ {
				if err := s.tick(context.Background(), m, b, r); err != nil {
					t.Fatal(err)
				}
				now = now.Add(2 * time.Second)
			}
			if b.enables.Load() != 0 {
				t.Fatal("enabled before the setup grace elapsed")
			}
			now = now.Add(10 * time.Minute)
			if err := s.tick(context.Background(), m, b, r); err != nil {
				t.Fatal(err)
			}
			if tc.want {
				waitRecovery(t, r)
				if b.enables.Load() != 1 {
					t.Fatal("Wi-Fi not enabled")
				}
			} else if r.done != nil || b.enables.Load() != 0 {
				t.Fatal("changed a radio without permission")
			}
		})
	}
}

func TestFirstRunEndsWhenEthernetWorks(t *testing.T) {
	now := time.Unix(1700000000, 0)
	b := &radioBackend{}
	m := machine.New(context.Background(), b, machine.Config{Now: func() time.Time { return now }})
	defer m.Close()
	s := New(Options{RadioRecovery: "first-run", DisableBLE: true}, nil)
	r := newRadioRecovery(s.radioSteps(m)...)
	defer r.close()
	// A slow wired DHCP lease, then a short outage, then a prolonged outage.
	for _, sample := range []struct {
		at      time.Duration
		address string
	}{
		{0, ""}, {10 * time.Second, ""}, {12 * time.Second, "192.0.2.10"},
		{time.Minute, ""}, {80 * time.Second, "192.0.2.10"},
		{2 * time.Minute, ""}, {8 * time.Minute, ""},
	} {
		now = time.Unix(1700000000, 0).Add(sample.at)
		b.address = sample.address
		if err := s.tick(context.Background(), m, b, r); err != nil {
			t.Fatal(err)
		}
	}
	if r.done != nil || b.enables.Load() != 0 {
		t.Fatal("a wired player's block was overridden")
	}
}

func TestExplicitWiFiEnableOverEthernetBehindHotspot(t *testing.T) {
	b := &radioBackend{address: "192.0.2.10"}
	h := wifi.NewHotspot(b, b, "Kalinka-Setup-Test")
	m := machine.New(context.Background(), h, machine.Config{})
	defer m.Close()
	s := New(Options{}, nil)
	s.machine.Store(m)
	if _, err := m.Tick(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := s.Open(); err != nil {
		t.Fatal(err)
	}
	if b.enables.Load() != 0 {
		t.Fatal("opening setup implied permission")
	}
	if err := s.EnableWiFi(context.Background()); err != nil {
		t.Fatal(err)
	}
	if b.enables.Load() != 1 {
		t.Fatal("explicit enable did not reach the backend")
	}
}

func TestRecoveryRetriesOnlyFailedRadiosAndStops(t *testing.T) {
	var wifiCalls, bleCalls atomic.Int32
	r := newRadioRecovery(func(context.Context) error {
		if wifiCalls.Add(1) == 1 {
			return errors.New("temporary D-Bus failure")
		}
		return nil
	}, func(context.Context) error { bleCalls.Add(1); return nil })
	r.retry, r.settle = 0, 0
	defer r.close()
	r.reconcile(context.Background(), true)
	waitRecovery(t, r)
	for i := 0; i < 20; i++ {
		r.reconcile(context.Background(), true)
	}
	if wifiCalls.Load() != 2 || bleCalls.Load() != 1 {
		t.Fatalf("Wi-Fi %d, Bluetooth %d", wifiCalls.Load(), bleCalls.Load())
	}
	// Another recovery requires connectivity to return and then be lost again.
	r.reconcile(context.Background(), false)
	r.reconcile(context.Background(), true)
	waitRecovery(t, r)
	if wifiCalls.Load() != 3 || bleCalls.Load() != 2 {
		t.Fatal("new offline window did not get its own recovery")
	}
}

func TestRecoveryFailureHasABoundedNumberOfAttempts(t *testing.T) {
	var calls atomic.Int32
	r := newRadioRecovery(func(context.Context) error { calls.Add(1); return errors.New("unavailable") })
	r.retry, r.settle = 0, 0
	defer r.close()
	r.reconcile(context.Background(), true)
	waitRecovery(t, r)
	if calls.Load() != 3 || !r.reconcile(context.Background(), true) {
		t.Fatalf("calls %d", calls.Load())
	}
}

func TestRecoveryDoesNotBlockHeartbeatAndCancelsWhenOnline(t *testing.T) {
	started := make(chan struct{})
	r := newRadioRecovery(func(ctx context.Context) error { close(started); <-ctx.Done(); return ctx.Err() })
	defer r.close()
	if r.reconcile(context.Background(), true) {
		t.Fatal("hotspot may start before recovery finishes")
	}
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("worker never started")
	}
	r.reconcile(context.Background(), false)
	waitRecovery(t, r)
}

func TestBluetoothRecoveryHonorsAdapterAndDisabledTransports(t *testing.T) {
	for _, disabled := range []bool{false, true} {
		s := New(Options{Adapter: "hci1", DisableBLE: disabled, DisableAP: true}, nil)
		s.root = rfkilltest.Stage(t, rfkilltest.Radio{Type: "bluetooth", Name: "hci0", Soft: true}, rfkilltest.Radio{Type: "bluetooth", Name: "hci1", Soft: true})
		b := &radioBackend{}
		m := machine.New(context.Background(), b, machine.Config{})
		defer m.Close()
		for _, step := range s.radioSteps(m) {
			if err := step(context.Background()); err != nil {
				t.Fatal(err)
			}
		}
		if !rfkilltest.Soft(t, s.root, "rfkill0") || rfkilltest.Soft(t, s.root, "rfkill1") != disabled || b.enables.Load() != 0 {
			t.Fatal("changed a disabled/unselected radio")
		}
	}
}
