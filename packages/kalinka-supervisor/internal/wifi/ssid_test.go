package wifi

import (
	"context"
	"errors"
	"reflect"
	"testing"
	"time"
)

func TestConnectedSSID(t *testing.T) {
	for _, tc := range []struct{ name, output, want string }{
		{"associated", "Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n\tSSID: Home Wi-Fi\n\tfreq: 5180\n", "Home Wi-Fi"},
		{"unicode", "Connected to aa:bb:cc:dd:ee:ff\n\tSSID: Caf\\xc3\\xa9\n", "Café"},
		{"spaces", "Connected to aa:bb:cc:dd:ee:ff\n\tSSID:  Home Wi-Fi \n", " Home Wi-Fi "},
		{"literal backslash", "Connected to aa:bb:cc:dd:ee:ff\n\tSSID: Home\\x5cWi-Fi\n", `Home\Wi-Fi`},
		{"disconnected", "Not connected.\n", ""},
		{"absent", "Connected to aa:bb:cc:dd:ee:ff\n\tfreq: 5180\n", ""},
		{"invalid escape", "Connected to aa:bb:cc:dd:ee:ff\n\tSSID: Home\\xzz\n", ""},
		{"control character", "Connected to aa:bb:cc:dd:ee:ff\n\tSSID: Home\\x00\n", ""},
		{"invalid utf8", "Connected to aa:bb:cc:dd:ee:ff\n\tSSID: Home\\xff\n", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			run := func(_ context.Context, timeout time.Duration, name string, args ...string) (string, error) {
				if timeout != time.Second || name != "iw" || !reflect.DeepEqual(args, []string{"dev", "wlan0", "link"}) {
					t.Fatalf("unexpected link query: %s %v, timeout %v", name, args, timeout)
				}
				return tc.output, nil
			}
			got, err := ConnectedSSID(context.Background(), "wlan0", run)
			if err != nil || got != tc.want {
				t.Fatalf("SSID %q, error %v; want %q", got, err, tc.want)
			}
		})
	}
}

func TestConnectedSSIDUnavailable(t *testing.T) {
	called := false
	run := func(context.Context, time.Duration, string, ...string) (string, error) {
		called = true
		return "", context.DeadlineExceeded
	}
	if got, err := ConnectedSSID(context.Background(), "wlan0", run); got != "" || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("SSID %q error %v", got, err)
	}
	called = false
	if _, err := ConnectedSSID(context.Background(), "wlan0; echo unexpected", run); err == nil || called {
		t.Fatal("invalid interface reached the runner")
	}
}
