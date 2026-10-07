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

func TestReinstallOutcome(t *testing.T) {
	dir := t.TempDir()
	r := ReinstallRecord{ResultFile: filepath.Join(dir, "reinstall-result")}
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

func TestReinstallTailStartsAtALine(t *testing.T) {
	dir := t.TempDir()
	r := ReinstallRecord{LogFile: filepath.Join(dir, "reinstall.log")}
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
