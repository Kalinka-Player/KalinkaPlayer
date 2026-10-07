package control

import "testing"

func TestRemoteAllowed(t *testing.T) {
	for addr, want := range map[string]bool{
		"127.0.0.1:5000":        true,
		"192.168.1.20:5000":     true,
		"10.1.2.3:5000":         true,
		"172.16.0.9:5000":       true,
		"169.254.10.1:5000":     true,
		"100.101.102.103:5000":  true,
		"[::ffff:10.0.0.1]:500": true,
		"[::1]:5000":            true,
		"203.0.113.5:5000":      false,
		"8.8.8.8:5000":          false,
		"100.128.0.1:5000":      false,
		"garbage":               false,
	} {
		if remoteAllowed(addr) != want {
			t.Errorf("remoteAllowed(%q) != %v", addr, want)
		}
	}
}

func TestHostAllowed(t *testing.T) {
	for host, want := range map[string]bool{
		"192.168.1.5:8001":      true,
		"192.168.1.5":           true,
		"[fe80::1]:8001":        true,
		"[::1]":                 true,
		"localhost:8001":        true,
		"kalinka:8001":          true,
		"DietPi":                true,
		"kalinka.local:8001":    true,
		"Kalinka.Local.:8001":   true,
		"box.lan":               true,
		"player.home.arpa":      true,
		"box.internal":          true,
		"evil.example.com":      false,
		"local.example.com":     false,
		"kalinka.local.evil.io": false,
		"":                      false,
		":8001":                 false,
	} {
		if hostAllowed(host) != want {
			t.Errorf("hostAllowed(%q) != %v", host, want)
		}
	}
}

func TestOriginAllowed(t *testing.T) {
	for _, tc := range []struct {
		origin, host string
		want         bool
	}{
		{"http://kalinka.local:8000", "kalinka.local:8001", true},
		{"http://KALINKA.local.:8000", "kalinka.local:8001", true},
		{"http://192.168.1.5:8000", "192.168.1.5:8001", true},
		{"http://[fe80::1]:8000", "[fe80::1]:8001", true},
		{"http://kalinka.local:8080", "kalinka.local:8001", false},
		{"http://kalinka.local", "kalinka.local:8001", false},
		{"http://other.local:8000", "kalinka.local:8001", false},
		{"https://evil.example.com:8000", "kalinka.local:8001", false},
		{"null", "kalinka.local:8001", false},
		{"null", "", false},
		{"file:///home/user/page.html", "kalinka.local:8001", false},
	} {
		if originAllowed(tc.origin, tc.host, 8000) != tc.want {
			t.Errorf("originAllowed(%q, %q) != %v", tc.origin, tc.host, tc.want)
		}
	}
	if !originAllowed("http://kalinka.local", "kalinka.local:8001", 80) {
		t.Error("default HTTP port not matched")
	}
	if !originAllowed("http://kalinka.local:8001", "kalinka.local:8001", 8000) {
		t.Error("the supervisor's own page refused")
	}
	if !originAllowed("http://kalinka.local", "kalinka.local", 8000) {
		t.Error("the supervisor's own page on the default port refused")
	}
}
