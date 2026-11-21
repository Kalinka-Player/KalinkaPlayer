package provision

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"kalinka/supervisor/internal/control"
	"kalinka/supervisor/internal/machine"
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
		{"set before the supervisor started", regulatory("DE"), nil, "DE"},
		{"the world domain", regulatory("00"), nil, ""},
		{"iw failing", "", protocol.Unavailable, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := New(Options{}, nil)
			s.run = answering(tc.reg, tc.err)
			s.readCountry(context.Background())
			if got := s.Snapshot()["wifi_country"]; got != tc.want {
				t.Fatalf("reported %q, want %q", got, tc.want)
			}
		})
	}
}

func TestRunReadsTheWiFiCountryOnceThenTakesTheBackendsReports(t *testing.T) {
	reads := make(chan string, 8)
	s := New(Options{Test: true, TestAddress: "192.0.2.1", DisableBLE: true}, nil)
	s.run = func(_ context.Context, _ time.Duration, name string, args ...string) (string, error) {
		reads <- strings.Join(append([]string{name}, args...), " ")
		return regulatory("DE"), nil
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
	select {
	case command := <-reads:
		if command != "iw reg get" {
			t.Fatalf("ran %q at start", command)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the country was not read at start")
	}
	waitFor(t, "setup did not start", func() bool { return s.Snapshot()["available"] == true })
	for range 3 {
		if got := s.Snapshot()["wifi_country"]; got != "DE" {
			t.Fatalf("reported %q, want DE", got)
		}
	}
	if len(reads) != 0 {
		t.Fatal("a status read ran iw")
	}
	if err := s.Open(); err != nil {
		t.Fatal(err)
	}
	if err := s.Command("http:0123456789abcdef", []byte(`{"v":1,"op":"scan","country":"GB"}`)); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the scan's country was not reported", func() bool { return s.Snapshot()["wifi_country"] == "GB" })
	for deadline := time.Now().Add(5 * time.Second); s.Snapshot()["setup"].(machine.Snapshot).ScanState != "ready"; time.Sleep(10 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("the scan did not finish")
		}
	}
	if len(reads) != 0 {
		t.Fatalf("iw ran %d times after start", len(reads))
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
