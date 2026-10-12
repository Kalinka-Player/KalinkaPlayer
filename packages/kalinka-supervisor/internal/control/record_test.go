package control

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestReinstallUnitWritesWhatTheRecordReads(t *testing.T) {
	b, err := os.ReadFile("../../systemd/kalinka-reinstall.service")
	if err != nil {
		t.Fatal(err)
	}
	unit := string(b)
	record := NewReinstallRecord("/var/lib/kalinka-supervisor")
	for _, line := range []string{
		"StandardOutput=truncate:" + record.LogFile,
		`ExecStopPost=/bin/sh -c 'echo "$SERVICE_RESULT" > ` + record.ResultFile + `'`,
		"ExecStart=/usr/lib/kalinka-supervisor/reinstall.sh",
		"TimeoutStartSec=infinity",
		"Type=oneshot",
	} {
		if !strings.Contains(unit, "\n"+line+"\n") {
			t.Errorf("kalinka-reinstall.service lacks %q", line)
		}
	}
	script, err := os.ReadFile("../../reinstall.sh")
	if err != nil || !strings.Contains(string(script), "KALINKA_REINSTALL=1 bash") {
		t.Fatal("reinstall.sh does not run the installer in reinstall mode")
	}
}

func TestPluginRemovalUnitWritesWhatTheRecordReads(t *testing.T) {
	b, err := os.ReadFile("../../systemd/" + PluginRemovalUnit)
	if err != nil {
		t.Fatal(err)
	}
	unit := string(b)
	removals := NewPluginRemovals("/var/lib/kalinka-supervisor")
	for _, line := range []string{
		"StandardOutput=truncate:" + removals.LogFile,
		`ExecStopPost=/bin/sh -c 'echo "$SERVICE_RESULT" > ` + removals.ResultFile + `'`,
		"ExecStart=/usr/lib/kalinka-supervisor/remove-plugin.sh " + removals.RequestFile,
		"TimeoutStartSec=infinity",
		"Type=oneshot",
	} {
		if !strings.Contains(unit, "\n"+line+"\n") {
			t.Errorf("%s lacks %q", PluginRemovalUnit, line)
		}
	}
	deb, err := os.ReadFile("../../build-deb.sh")
	if err != nil || !strings.Contains(string(deb), `"$pkg_dir/remove-plugin.sh"`) {
		t.Fatal("the package does not ship remove-plugin.sh")
	}
}

func TestOutcome(t *testing.T) {
	dir := t.TempDir()
	r := Record{ResultFile: filepath.Join(dir, "reinstall-result")}
	if outcome, at := r.Outcome(); outcome != "" || !at.IsZero() {
		t.Fatalf("no reinstall yet = %q %v", outcome, at)
	}
	for result, want := range map[string]string{"success\n": "succeeded", "exit-code\n": "failed", "signal": "failed", "\n": ""} {
		if err := os.WriteFile(r.ResultFile, []byte(result), 0600); err != nil {
			t.Fatal(err)
		}
		if outcome, _ := r.Outcome(); outcome != want {
			t.Errorf("result %q = %q, want %q", result, outcome, want)
		}
	}
}

func TestTailStartsAtALine(t *testing.T) {
	dir := t.TempDir()
	r := Record{LogFile: filepath.Join(dir, "reinstall.log")}
	if b, err := r.Tail(64); err != nil || b != nil {
		t.Fatalf("missing log = %q, %v", b, err)
	}
	var lines []string
	for i := range 20 {
		lines = append(lines, strings.Repeat(string(rune('a'+i)), 9))
	}
	if err := os.WriteFile(r.LogFile, []byte(strings.Join(lines, "\n")+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	b, err := r.Tail(35)
	if err != nil || string(b) != "rrrrrrrrr\nsssssssss\nttttttttt\n" {
		t.Fatalf("tail = %q, %v", b, err)
	}
	if b, _ := r.Tail(1 << 20); len(b) != 200 {
		t.Fatalf("whole log = %d bytes", len(b))
	}
}

func TestPluginRemovalRequestIsRootOnly(t *testing.T) {
	removals := NewPluginRemovals(t.TempDir())
	if removals.Package() != "" {
		t.Fatal("a package before any request")
	}
	if err := removals.Request("kalinka-plugin-qobuz"); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(removals.RequestFile)
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("request file mode = %v, %v", info.Mode(), err)
	}
	if b, _ := os.ReadFile(removals.RequestFile); string(b) != "kalinka-plugin-qobuz\n" {
		t.Fatalf("request = %q", b)
	}
	if removals.Package() != "kalinka-plugin-qobuz" {
		t.Fatalf("package = %q", removals.Package())
	}
}
