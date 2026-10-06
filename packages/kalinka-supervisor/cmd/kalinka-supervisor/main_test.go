package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestBackendSupport(t *testing.T) {
	for _, tc := range []struct {
		name, backend, installed string
		test, supported          bool
	}{
		{"NetworkManager", "nm", "NetworkManager", false, true},
		{"DietPi", "dietpi", "/boot/dietpi/dietpi-network", false, true},
		{"generic ifupdown", "nm", "ifup", false, false},
		{"explicit missing DietPi", "dietpi", "NetworkManager", false, false},
		{"simulation", "nm", "", true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			problem := backendProblem(options{backend: tc.backend, test: tc.test}, func(name string) (string, error) {
				if name == tc.installed {
					return name, nil
				}
				return "", exec.ErrNotFound
			})
			if (problem == "") != tc.supported {
				t.Fatalf("backend support: %q", problem)
			}
		})
	}
}

func TestUnsupportedBackendDoesNotRestart(t *testing.T) {
	if os.Getenv("KALINKA_TEST_UNSUPPORTED_BACKEND") == "1" {
		os.Args = []string{"kalinka-supervisor", "provision", "--backend", "nm", "--always-advertise"}
		main()
		return
	}
	t.Setenv("PATH", t.TempDir())
	t.Setenv("KALINKA_TEST_UNSUPPORTED_BACKEND", "1")
	cmd := exec.Command(os.Args[0], "-test.run=^TestUnsupportedBackendDoesNotRestart$")
	output, err := cmd.CombinedOutput()
	if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != 78 {
		t.Fatalf("expected permanent configuration failure, got %v", err)
	}
	if !strings.Contains(string(output), "install network-manager") {
		t.Fatalf("missing actionable diagnostic: %s", output)
	}
	unit, err := os.ReadFile("../../systemd/kalinka-supervisor.service")
	if err != nil || !strings.Contains(string(unit), "RestartPreventExitStatus=78\n") {
		t.Fatal("systemd would retry an unsupported backend")
	}
}

func TestOptions(t *testing.T) {
	conf := filepath.Join(t.TempDir(), "core.json")
	if err := os.WriteFile(conf, []byte(`{"base_config.server.port":8123}`), 0600); err != nil {
		t.Fatal(err)
	}
	o, err := parse([]string{"provision", "--test", "--server-config", conf})
	if err != nil || o.port != 8123 || !o.test {
		t.Fatal("Core configuration not respected")
	}
	for _, args := range [][]string{{"--backend", "invalid"}, {"--port", "65536"}, {"--test-address", "192.0.2.1"}, {"--test", "--test-result", "invalid"}, {"--test", "--test-address", "::1"}, {"extra"}} {
		if _, err := parse(args); err == nil {
			t.Errorf("accepted invalid options %v", args)
		}
	}
	if _, err := parse([]string{"--check-enabled"}); err != nil {
		t.Fatal(err)
	}
	t.Setenv("KALINKA_BLE_SETUP", "0")
	if enabled() {
		t.Fatal("disable override ignored")
	}
}
