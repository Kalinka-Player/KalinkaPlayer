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

// ReinstallRecord reads what the last reinstall left behind: the result its
// unit writes as it stops, and its output. Both files belong to the unit; the
// record only reads them.
type ReinstallRecord struct {
	ResultFile, LogFile string
}

// NewReinstallRecord reads where kalinka-reinstall.service writes, given the supervisor's state directory.
func NewReinstallRecord(stateDir string) ReinstallRecord {
	return ReinstallRecord{
		ResultFile: filepath.Join(stateDir, "reinstall-result"),
		LogFile:    "/var/log/kalinka-supervisor/reinstall.log",
	}
}

// Outcome is "succeeded" or "failed", with the time the last reinstall
// finished, or "" when none has finished on this box.
func (r ReinstallRecord) Outcome() (string, time.Time) {
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

// Tail returns up to limit bytes from the end of the last reinstall's output,
// starting at a whole line; nothing at all when no reinstall has run.
func (r ReinstallRecord) Tail(limit int64) ([]byte, error) {
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
