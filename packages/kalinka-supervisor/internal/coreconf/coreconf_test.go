package coreconf

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestReadID(t *testing.T) {
	dir := t.TempDir()
	write := func(name, body string) string {
		t.Helper()
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	if got := ReadID(write("id", "ee4d496c-3f6c-4388-a02e-f6a2f17829cd\n")); got != "ee4d496c-3f6c-4388-a02e-f6a2f17829cd" {
		t.Fatalf("ReadID = %q", got)
	}
	if got := ReadID(write("oversized", strings.Repeat("a", 65))); got != "" {
		t.Fatalf("oversized identity accepted: %q", got)
	}
	if got := ReadID(filepath.Join(dir, "missing")); got != "" {
		t.Fatalf("missing identity = %q", got)
	}
}

func TestInstalled(t *testing.T) {
	for _, dir := range append([]string{""}, unitDirs...) {
		root := t.TempDir()
		if dir != "" {
			if err := os.MkdirAll(filepath.Join(root, dir), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(root, dir, Unit), []byte("[Unit]\n"), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		if got := Installed(root); got != (dir != "") {
			t.Errorf("Core's unit in %q: installed = %v", dir, got)
		}
	}
}

func TestRead(t *testing.T) {
	dir := t.TempDir()
	for _, tc := range []struct {
		name, body string
		want       Config
		fails      bool
	}{
		{"both set", `{"base_config.server.interface":"eth0","base_config.server.port":8123}`, Config{"eth0", 8123}, false},
		{"unset keys keep defaults", `{"base_config.server.service_name":"Kitchen"}`, Default, false},
		{"malformed", `{"base_config.server.port":`, Default, true},
		{"port out of range", `{"base_config.server.port":65536}`, Default, true},
		{"port not a number", `{"base_config.server.port":"8000"}`, Default, true},
		{"empty interface", `{"base_config.server.interface":""}`, Default, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(dir, tc.name+".cfg")
			if err := os.WriteFile(path, []byte(tc.body), 0600); err != nil {
				t.Fatal(err)
			}
			got, err := Read(path)
			if got != tc.want || (err != nil) != tc.fails {
				t.Fatalf("Read = %+v, %v", got, err)
			}
		})
	}
	if got, err := Read(filepath.Join(dir, "missing.cfg")); got != Default || err != nil {
		t.Fatalf("missing file = %+v, %v", got, err)
	}
}

func TestSourceFollowsChanges(t *testing.T) {
	path := filepath.Join(t.TempDir(), "core.cfg")
	s := NewSource(path)
	if s.Current() != Default {
		t.Fatal("missing file is not Default")
	}
	write := func(body string, at time.Time) {
		t.Helper()
		if err := os.WriteFile(path, []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.Chtimes(path, at, at); err != nil {
			t.Fatal(err)
		}
		s.Refresh()
	}
	start := time.Now()
	write(`{"base_config.server.port":8123}`, start)
	if s.Current().Port != 8123 {
		t.Fatal("new file not read")
	}
	write(`{"base_config.server.port":8124}`, start.Add(time.Second))
	if s.Current().Port != 8124 {
		t.Fatal("change not followed")
	}
	write(`{"base_config.server.port":`, start.Add(2*time.Second))
	if s.Current() != Default {
		t.Fatal("broken file not Default")
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	s.Refresh()
	if s.Current() != Default {
		t.Fatal("removed file not Default")
	}
}
