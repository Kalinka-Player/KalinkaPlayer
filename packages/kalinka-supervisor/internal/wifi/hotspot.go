package wifi

import (
	"context"
	"net"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"kalinka/supervisor/internal/protocol"
)

const APAddress = "192.168.4.1"
const APPassword = "kalinka-setup"

// AccessPoint temporarily borrows the client radio, preserving its saved configuration.
type AccessPoint interface {
	StartAP(context.Context, string) error
	StopAP(context.Context) error
	APInterface() string
}

// Hotspot serializes radio changes with scans and joins. The AP address never counts as a LAN connection.
type Hotspot struct {
	Backend
	AP               AccessPoint
	Name             string
	mu               sync.Mutex
	active           atomic.Bool
	cache            []protocol.Network
	cacheCountry     string
	AddressWithoutAP func(context.Context, string) (string, error)
	// HasClients reports whether anyone is on the AP; true when it cannot tell.
	HasClients    func(context.Context, string) bool
	NearbyCountry func(context.Context, string) string
	JoinDelay     time.Duration
	retryAt       time.Time
}

func NewHotspot(b Backend, ap AccessPoint, name string) *Hotspot {
	return &Hotspot{Backend: b, AP: ap, Name: name, JoinDelay: time.Second,
		AddressWithoutAP: func(ctx context.Context, iface string) (string, error) {
			return (Host{Root: "/", Interfaces: net.Interfaces, Addrs: (*net.Interface).Addrs, Exclude: iface}).Address(ctx, "")
		},
		HasClients: func(ctx context.Context, iface string) bool {
			out, err := Run(ctx, 5*time.Second, "iw", "dev", iface, "station", "dump")
			return err != nil || strings.Contains(out, "Station ")
		},
		NearbyCountry: func(ctx context.Context, iface string) string { return nearbyCountry(ctx, Run, iface) }}
}

func (h *Hotspot) Active() bool { return h.active.Load() }

func (h *Hotspot) Address(ctx context.Context) (string, error) {
	if h.Active() {
		return h.AddressWithoutAP(ctx, h.AP.APInterface())
	}
	return h.Backend.Address(ctx)
}

// Reconcile skips a busy radio; the worker holding it will finish its rollback first.
func (h *Hotspot) Reconcile(ctx context.Context, enabled bool) (result error) {
	if !h.mu.TryLock() {
		return nil
	}
	defer h.mu.Unlock()
	if enabled && h.Active() {
		if health, ok := h.AP.(interface{ APHealthy(context.Context) bool }); ok && !health.APHealthy(ctx) {
			if err := h.stop(ctx); err != nil {
				return err
			}
		}
	}
	if enabled == h.Active() {
		return nil
	}
	if !enabled {
		return h.stop(ctx)
	}
	if time.Now().Before(h.retryAt) {
		return nil
	}
	defer func() {
		if result != nil {
			h.retryAt = time.Now().Add(30 * time.Second)
		}
	}()
	address, err := h.Backend.Address(ctx)
	if err != nil || address != "" {
		return err
	}
	ctx, stop := context.WithTimeout(ctx, 40*time.Second)
	defer stop()
	scan, cancel := context.WithTimeout(ctx, 20*time.Second)
	_, _ = h.scan(scan, h.cacheCountry)
	if h.cacheCountry == "" {
		// A Pi's radio without a country skips channels 12 and 13.
		if country := h.NearbyCountry(scan, h.AP.APInterface()); country != "" {
			_, _ = h.scan(scan, country)
		}
	}
	cancel()
	if err = h.AP.StartAP(ctx, h.Name); err != nil {
		return err
	}
	h.active.Store(true)
	return nil
}

func (h *Hotspot) stop(ctx context.Context) error {
	if err := h.AP.StopAP(ctx); err != nil {
		return err
	}
	h.active.Store(false)
	return nil
}

func (h *Hotspot) Scan(ctx context.Context, country string) ([]protocol.Network, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	// Single-radio boards cannot scan all channels while serving an AP.
	if h.Active() {
		// A list scanned without the country misses channels 12 and 13 on a Pi; with nobody on the AP, rescan.
		if country == h.cacheCountry || h.HasClients(ctx, h.AP.APInterface()) {
			return slices.Clone(h.cache), nil
		}
		// Reconcile reopens the AP.
		if err := h.stop(ctx); err != nil {
			return nil, err
		}
	}
	return h.scan(ctx, country)
}

func (h *Hotspot) scan(ctx context.Context, country string) ([]protocol.Network, error) {
	networks, err := h.Backend.Scan(ctx, country)
	if err == nil {
		h.cache = slices.Clone(networks)
		h.cacheCountry = country
	}
	return networks, err
}

func (h *Hotspot) Join(ctx context.Context, c protocol.Command, progress func(byte)) (string, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return "", err
	}
	wasActive := h.Active()
	if wasActive {
		// Let the HTTP acknowledgement reach the phone before its link disappears.
		if err := Pause(ctx, h.JoinDelay); err != nil {
			return "", err
		}
		if err := h.stop(ctx); err != nil {
			return "", err
		}
	}
	address, err := h.Backend.Join(ctx, c, progress)
	if err != nil && wasActive && ctx.Err() == nil {
		restore, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		if e := h.AP.StartAP(restore, h.Name); e != nil {
			return "", protocol.Unavailable
		}
		h.active.Store(true)
	}
	return address, err
}

func (h *Hotspot) Close() error {
	h.mu.Lock()
	defer h.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	err := h.stop(ctx)
	if closeErr := h.Backend.Close(); err == nil {
		err = closeErr
	}
	return err
}
