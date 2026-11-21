package provision

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"kalinka/supervisor/internal/control"
	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/wifi"
)

func regulatory(code string) string {
	return "global\ncountry " + code + ": DFS-ETSI\n\t(2400 - 2483 @ 40), (N/A, 20), (N/A)\n"
}

func answering(reg string, err error) wifi.Runner {
	return func(context.Context, time.Duration, string, ...string) (string, error) { return reg, err }
}

func TestSnapshotReportsTheWiFiCountry(t *testing.T) {
	s := New(Options{}, nil)
	if got := s.Snapshot()["wifi_country"]; got != "" {
		t.Fatalf("reported %q before reading one", got)
	}
	for _, tc := range []struct {
		name, reg string
		err       error
		want      string
	}{
		{"set by setup", regulatory("DE"), nil, "DE"},
		{"the world domain", regulatory("00"), nil, ""},
		{"iw failing keeps the last one read", "", protocol.Unavailable, "DE"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := New(Options{}, nil)
			s.run = answering(regulatory("DE"), nil)
			s.readCountry(context.Background())
			s.run = answering(tc.reg, tc.err)
			s.readCountry(context.Background())
			if got := s.Snapshot()["wifi_country"]; got != tc.want {
				t.Fatalf("reported %q, want %q", got, tc.want)
			}
		})
	}
}

// radioless hides the backend's radio switch.
type radioless struct{ wifi.Backend }

func TestCountryWatchReadsAfterEachScanOrJoin(t *testing.T) {
	ctx := context.Background()
	b := &radioBackend{}
	reads := 0
	w := countryWatch{b, func(context.Context) { reads++ }}
	_, _ = w.Scan(ctx, "GB")
	_, _ = w.Join(ctx, protocol.Command{}, func(byte) {})
	_, _ = w.Address(ctx)
	if reads != 2 {
		t.Fatalf("read the country %d times, want once after the scan and once after the join", reads)
	}
	if err := w.EnableRadio(ctx); err != nil || b.enables.Load() != 1 {
		t.Fatalf("the radio switch did not reach the backend: %v", err)
	}
	if err := (countryWatch{radioless{b}, w.read}).EnableRadio(ctx); err != protocol.Unavailable {
		t.Fatalf("a backend without a radio switch answered %v", err)
	}
}

func TestRunReadsTheWiFiCountryAtStartAndAfterAScan(t *testing.T) {
	reads := make(chan string, 8)
	var reg atomic.Value
	reg.Store(regulatory("DE"))
	s := New(Options{Test: true, TestAddress: "192.0.2.1", DisableBLE: true}, nil)
	s.run = func(_ context.Context, _ time.Duration, name string, args ...string) (string, error) {
		reads <- strings.Join(append([]string{name}, args...), " ")
		return reg.Load().(string), nil
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- s.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		if err := <-done; err != nil {
			t.Error(err)
		}
	})
	read := func(when string) {
		t.Helper()
		select {
		case command := <-reads:
			if command != "iw reg get" {
				t.Fatalf("ran %q %s", command, when)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("the country was not read %s", when)
		}
	}
	read("at start")
	waitFor(t, "setup did not start", func() bool { return s.Snapshot()["available"] == true })
	for range 3 {
		if got := s.Snapshot()["wifi_country"]; got != "DE" {
			t.Fatalf("reported %q, want DE", got)
		}
	}
	if len(reads) != 0 {
		t.Fatal("a status read ran iw")
	}
	reg.Store(regulatory("GB"))
	if err := s.Open(); err != nil {
		t.Fatal(err)
	}
	if err := s.Command("http:0123456789abcdef", []byte(`{"v":1,"op":"scan","country":"GB"}`)); err != nil {
		t.Fatal(err)
	}
	read("after the scan")
	waitFor(t, "the scan's country was not reported", func() bool { return s.Snapshot()["wifi_country"] == "GB" })
	if len(reads) != 0 {
		t.Fatalf("iw ran %d more times than the scan needed", len(reads))
	}
}

func TestSetupStatusCarriesTheWiFiCountry(t *testing.T) {
	s := New(Options{}, nil)
	s.run = answering(regulatory("DE"), nil)
	s.readCountry(context.Background())
	h := control.NewHandler(nil, control.HandlerConfig{CorePort: func() int { return 8000 }, NetworkSetup: s})
	r := httptest.NewRequest(http.MethodGet, "/v1/setup", nil)
	r.RemoteAddr = "192.168.1.20:50000"
	r.Host = "kalinka.local:8001"
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	var status map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &status); w.Code != http.StatusOK || err != nil {
		t.Fatalf("HTTP %d, %v: %s", w.Code, err, w.Body.String())
	}
	if status["wifi_country"] != "DE" {
		t.Fatalf("GET /v1/setup = %s, want wifi_country DE", w.Body.String())
	}
}
