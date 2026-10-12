package control

import (
	"bytes"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Record reads what the last run of a oneshot unit left behind: the result
// the unit writes as it stops, and its output. Both files belong to the unit;
// the record only reads them.
type Record struct {
	ResultFile, LogFile string
}

const logDir = "/var/log/kalinka-supervisor"

// NewReinstallRecord reads where kalinka-reinstall.service writes, given the supervisor's state directory.
func NewReinstallRecord(stateDir string) Record {
	return Record{
		ResultFile: filepath.Join(stateDir, "reinstall-result"),
		LogFile:    filepath.Join(logDir, "reinstall.log"),
	}
}

// Outcome is "succeeded" or "failed", with the time the last run finished,
// or "" when none has finished on this box.
func (r Record) Outcome() (string, time.Time) {
	f, err := os.Open(r.ResultFile)
	if err != nil {
		return "", time.Time{}
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return "", time.Time{}
	}
	b, _ := io.ReadAll(io.LimitReader(f, 64))
	switch strings.TrimSpace(string(b)) {
	case "":
		return "", time.Time{}
	case "success":
		return "succeeded", info.ModTime()
	}
	return "failed", info.ModTime()
}

// Tail returns up to limit bytes from the end of the last run's output,
// starting at a whole line; nothing at all when the unit has never run.
func (r Record) Tail(limit int64) ([]byte, error) {
	f, err := os.Open(r.LogFile)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	start := max(info.Size()-limit, 0)
	b, err := io.ReadAll(io.NewSectionReader(f, start, info.Size()-start))
	if err != nil {
		return nil, err
	}
	if start > 0 {
		if i := bytes.IndexByte(b, '\n'); i >= 0 {
			b = b[i+1:]
		}
	}
	return b, nil
}

// PluginRemovals is kalinka-plugin-removal.service's record plus the request
// the supervisor hands it: one package name in a file only root writes and
// reads, never a command line a client could reach.
type PluginRemovals struct {
	Record
	RequestFile string
}

func NewPluginRemovals(stateDir string) PluginRemovals {
	return PluginRemovals{
		Record: Record{
			ResultFile: filepath.Join(stateDir, "plugin-removal-result"),
			LogFile:    filepath.Join(logDir, "plugin-removal.log"),
		},
		RequestFile: filepath.Join(stateDir, "plugin-removal-request"),
	}
}

// Request names the package the next run of the unit removes.
func (r PluginRemovals) Request(pkg string) error {
	return os.WriteFile(r.RequestFile, []byte(pkg+"\n"), 0600)
}

// Package is the package the last request named, "" when none has been made.
func (r PluginRemovals) Package() string {
	b, err := os.ReadFile(r.RequestFile)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}
