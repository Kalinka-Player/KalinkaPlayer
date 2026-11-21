package provision

import (
	"context"

	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/wifi"
)

// countryWatch reads the Wi-Fi country again after each scan or join, the only
// times setup sets it, so a status read never has to run iw. The read outlives
// the operation's own deadline or cancellation, since the backend may have set
// the country before a scan timed out or a join was cancelled.
type countryWatch struct {
	wifi.Backend
	read func(context.Context)
}

// Scan reads the country only after a scan under one; an unnamed scan, as the hotspot's first is, sets none.
func (w countryWatch) Scan(ctx context.Context, country string) ([]protocol.Network, error) {
	networks, err := w.Backend.Scan(ctx, country)
	if country != "" {
		w.read(context.WithoutCancel(ctx))
	}
	return networks, err
}

func (w countryWatch) Join(ctx context.Context, c protocol.Command, progress func(byte)) (string, error) {
	address, err := w.Backend.Join(ctx, c, progress)
	w.read(context.WithoutCancel(ctx))
	return address, err
}

// EnableRadio passes the backend's radio switch through, which the hotspot and the machine look for.
func (w countryWatch) EnableRadio(ctx context.Context) error {
	if radio, ok := w.Backend.(interface{ EnableRadio(context.Context) error }); ok {
		return radio.EnableRadio(ctx)
	}
	return protocol.Unavailable
}

// readCountry keeps the country it last read when iw cannot answer.
func (s *Service) readCountry(ctx context.Context) {
	if code, err := wifi.Country(ctx, s.run); err == nil {
		s.country.Store(code)
	}
}
