package main

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestOptions(t *testing.T) {
	o, err := parse(nil)
	if err != nil || o.listenPort != 8001 || o.corePort != 0 || o.IdentityFile != "/var/lib/kalinka/server_id" || o.config != "/etc/kalinka/kalinka_conf.cfg" {
		t.Fatalf("defaults = %+v, %v", o, err)
	}
	o, err = parse([]string{"--test", "--listen-port", "9010", "--port", "8123"})
	if err != nil || o.listenPort != 9010 || o.corePort != 8123 || !o.Test {
		t.Fatalf("explicit = %+v, %v", o, err)
	}
	for _, args := range [][]string{
		{"--backend", "invalid"}, {"--port", "65536"}, {"--listen-port", "0"}, {"--listen-port", "65536"},
		{"--test-address", "192.0.2.1"}, {"--test", "--test-result", "invalid"}, {"--test", "--test-address", "::1"},
		{"extra"}, {"provision"},
	} {
		if _, err := parse(args); err == nil {
			t.Errorf("accepted invalid options %v", args)
		}
	}
}

func TestEnabled(t *testing.T) {
	dietpi := filepath.Join(t.TempDir(), "dietpi.txt")
	if !enabled("KALINKA_CONTROL_API", dietpi) {
		t.Fatal("missing DietPi configuration disables")
	}
	if err := os.WriteFile(dietpi, []byte("AUTO_SETUP_LOCALE=C.UTF-8\nKALINKA_CONTROL_API='0' # LAN power control off\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if enabled("KALINKA_CONTROL_API", dietpi) {
		t.Fatal("DietPi switch ignored")
	}
	if !enabled("KALINKA_BLE_SETUP", dietpi) {
		t.Fatal("one switch turned off the other")
	}
	t.Setenv("KALINKA_BLE_SETUP", "0")
	if enabled("KALINKA_BLE_SETUP", dietpi) {
		t.Fatal("environment switch ignored")
	}
}

func TestBothSwitchesOffExits(t *testing.T) {
	if os.Getenv("KALINKA_TEST_BOTH_OFF") == "1" {
		os.Args = []string{"kalinka-supervisor"}
		main()
		return
	}
	socket := filepath.Join(t.TempDir(), "notify")
	notify, err := net.ListenUnixgram("unixgram", &net.UnixAddr{Name: socket, Net: "unixgram"})
	if err != nil {
		t.Fatal(err)
	}
	defer notify.Close()
	cmd := exec.Command(os.Args[0], "-test.run=^TestBothSwitchesOffExits$")
	cmd.Env = append(os.Environ(), "KALINKA_TEST_BOTH_OFF=1", "KALINKA_BLE_SETUP=0", "KALINKA_CONTROL_API=0", "NOTIFY_SOCKET="+socket)
	output, err := cmd.CombinedOutput()
	if err != nil || !strings.Contains(string(output), "both disabled") {
		t.Fatalf("exit %v: %s", err, output)
	}
	_ = notify.SetReadDeadline(time.Now().Add(time.Second))
	message := make([]byte, 256)
	n, err := notify.Read(message)
	if err != nil || !strings.HasPrefix(string(message[:n]), "READY=1") {
		t.Fatalf("exited without telling systemd it started: %q, %v", message[:n], err)
	}
}

func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

func TestControlAPIServesAndStops(t *testing.T) {
	dir := t.TempDir()
	port := freePort(t)
	identity := filepath.Join(dir, "server_id")
	if err := os.WriteFile(identity, []byte("ee4d496c-3f6c-4388-a02e-f6a2f17829cd\n"), 0600); err != nil {
		t.Fatal(err)
	}
	o, err := parse([]string{"--test", "--listen-port", strconv.Itoa(port), "--server-config", filepath.Join(dir, "core.cfg"),
		"--server-id-file", identity, "--state-dir", filepath.Join(dir, "state"), "--runtime-dir", filepath.Join(dir, "run")})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	stopped := make(chan error)
	go func() { stopped <- run(ctx, o, false, true) }()
	base := "http://127.0.0.1:" + strconv.Itoa(port)
	var info struct {
		ServerID string `json:"server_id"`
		Protocol int
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		resp, err := http.Get(base + "/info")
		if err == nil {
			err = json.NewDecoder(resp.Body).Decode(&info)
			resp.Body.Close()
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("control API never answered: %v", err)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if info.ServerID != "ee4d496c-3f6c-4388-a02e-f6a2f17829cd" || info.Protocol != 1 {
		t.Fatalf("info = %+v", info)
	}
	resp, err := http.Post(base+"/v1/actions/poweroff", "application/json", strings.NewReader(`{"server_id":"`+info.ServerID+`"}`))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("simulated poweroff = %d %s", resp.StatusCode, body)
	}
	cancel()
	select {
	case err := <-stopped:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("supervisor did not stop")
	}
	if _, err := http.Get(base + "/info"); err == nil {
		t.Fatal("control API still listening after stop")
	}
}

func TestUnitRunsUnconditionally(t *testing.T) {
	b, err := os.ReadFile("../../systemd/kalinka-supervisor.service")
	if err != nil {
		t.Fatal(err)
	}
	unit := string(b)
	for _, absent := range []string{"Condition", "ExecCondition", "RestartPreventExitStatus", " provision "} {
		if strings.Contains(unit, absent) {
			t.Errorf("unit still has %q", absent)
		}
	}
	for _, present := range []string{"\nWantedBy=multi-user.target\n", "\nRestartSec=10\n", "\nExecStart=/usr/lib/kalinka-supervisor/kalinka-supervisor --backend auto\n"} {
		if !strings.Contains(unit, present) {
			t.Errorf("unit lacks %q", strings.TrimSpace(present))
		}
	}
}
