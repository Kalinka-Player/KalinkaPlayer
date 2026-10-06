package main

import (
	"os"
	"path/filepath"
	"testing"
)

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
