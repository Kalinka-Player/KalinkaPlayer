// Package coreconf reads the Core settings the supervisor follows, without needing Core to run.
package coreconf

import (
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	DefaultPath   = "/etc/kalinka/kalinka_conf.cfg"
	IdentityPath  = "/var/lib/kalinka/server_id"
	AllInterfaces = "all"
)

// Config is the part of Core's configuration that decides where Core listens.
type Config struct {
	Interface string
	Port      int
}

// Default is what Core uses when its configuration sets neither field.
var Default = Config{Interface: AllInterfaces, Port: 8000}

var errInvalid = errors.New("invalid Core configuration")

// Read parses Core's flat JSON configuration. A missing file is Default, not
// an error; an unreadable or malformed one returns Default with an error.
func Read(path string) (Config, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return Default, nil
	}
	if err != nil {
		return Default, err
	}
	var raw map[string]json.RawMessage
	if json.Unmarshal(b, &raw) != nil {
		return Default, errInvalid
	}
	c := Default
	if v, ok := raw["base_config.server.interface"]; ok {
		if json.Unmarshal(v, &c.Interface) != nil || c.Interface == "" {
			return Default, errInvalid
		}
	}
	if v, ok := raw["base_config.server.port"]; ok {
		if json.Unmarshal(v, &c.Port) != nil || c.Port < 1 || c.Port > 65535 {
			return Default, errInvalid
		}
	}
	return c, nil
}

// ServerID returns Core's identity from path, or "" when Core has not created one or it is not plausible.
func ServerID(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, 65))
	if err != nil || len(b) > 64 {
		return ""
	}
	return strings.TrimSpace(string(b))
}

type stamp struct {
	modified time.Time
	size     int64
	exists   bool
}

// Source keeps the current reading of Core's configuration and rereads the
// file only when it changes. Safe for concurrent use.
type Source struct {
	path    string
	mu      sync.Mutex
	stamp   stamp
	current Config
	failing bool
}

func NewSource(path string) *Source {
	s := &Source{path: path, current: Default}
	s.Refresh()
	return s
}

// Refresh rereads the file if its size or modification time changed.
func (s *Source) Refresh() {
	var now stamp
	if info, err := os.Stat(s.path); err == nil {
		now = stamp{info.ModTime(), info.Size(), true}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if now == s.stamp {
		return
	}
	s.stamp = now
	c, err := Read(s.path)
	s.current = c
	// The file holds credentials, so its parse errors are never logged.
	if failing := err != nil; failing != s.failing {
		s.failing = failing
		if failing {
			slog.Warn("Core configuration unreadable; following Core's defaults")
		} else {
			slog.Info("Core configuration readable again")
		}
	}
}

func (s *Source) Current() Config {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.current
}
