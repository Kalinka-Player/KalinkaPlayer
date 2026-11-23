package control

import (
	"archive/zip"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"kalinka/supervisor/internal/soundcard"
)

const (
	// logWindow is how far back a download reaches when it names no since.
	logWindow = 24 * time.Hour
	// logCap bounds each file, counted from its newest entry, and so what a download holds in memory.
	logCap = 4 << 20
	// logSourceTimeout bounds one journalctl run on a slow card; what it printed by then is kept.
	logSourceTimeout = 20 * time.Second
)

// JournalRunner runs journalctl with args, copying its output to stdout
// until journalctl ends or stdout refuses more. A run that ctx cut short
// returns ctx's error. RunJournal is the production one.
type JournalRunner func(ctx context.Context, stdout io.Writer, args ...string) error

// RunJournal runs the system's journalctl, whose warnings on its own
// standard error never stop it.
func RunJournal(ctx context.Context, stdout io.Writer, args ...string) error {
	cmd := exec.CommandContext(ctx, "journalctl", args...)
	stderr := &firstBytes{b: make([]byte, 0, 512)}
	cmd.Stdout, cmd.Stderr = stdout, stderr
	cmd.WaitDelay = time.Second
	err := cmd.Run()
	if err != nil && ctx.Err() != nil {
		return ctx.Err()
	}
	if message := strings.TrimSpace(string(stderr.b)); err != nil && message != "" {
		return fmt.Errorf("%w: %s", err, message)
	}
	return err
}

// logSource is one file of a download and the journal matches that fill it.
type logSource struct {
	file    string
	matches []string
}

func unitMatches(units ...string) []string {
	matches := make([]string, len(units))
	for i, unit := range units {
		matches[i] = "--unit=" + unit
	}
	return matches
}

// logSources name the network units of DietPi's ifupdown and of NetworkManager
// alike: journalctl passes over a unit it holds no entries for.
var logSources = []logSource{
	{"core.log", unitMatches(CoreUnit, "kalinka-server-id.service", "kalinka-upgrade.service", "kalinka-restart.service",
		"kalinka-plugins.service", "kalinka-mounts.service", "kalinka-media-mount@*.service")},
	{"renderer.log", unitMatches("kalinka-renderer.service", "kalinka-renderer-upgrade.service")},
	{"supervisor.log", unitMatches("kalinka-supervisor.service", soundcard.BootUnit, soundcard.ApplyUnit,
		"kalinka-ssh-setup.service", ReinstallUnit, PluginRemovalUnit)},
	{"network.log", unitMatches("NetworkManager.service", "wpa_supplicant.service", "wpa_supplicant@*.service",
		"dhcpcd.service", "systemd-networkd.service", "networking.service", "ifup@*.service", "kalinka-wifi@*.service",
		"kalinka-card-settings.service", "kalinka-card-network-wait.service")},
	// Not -k, which reads only the current boot.
	{"kernel.log", []string{"_TRANSPORT=kernel"}},
}

func (s logSource) describe() string {
	return strings.ReplaceAll(strings.Join(s.matches, " "), "--unit=", "")
}

// logs streams a zip of logSources' files. Headers go out before journalctl
// first runs, and downloads take turns, so one buffer of logCap is all they hold.
func (h *handler) logs(w http.ResponseWriter, r *http.Request) string {
	now := time.Now().UTC()
	since := now.Add(-logWindow)
	if value := r.URL.Query().Get("since"); value != "" {
		parsed, err := time.Parse(time.RFC3339, value)
		if err != nil {
			return fail(w, "invalid_since")
		}
		// journalctl refuses a negative @seconds, and the journal holds nothing older anyway.
		since = time.Unix(max(parsed.Unix(), 0), 0)
	}
	w.Header().Set("Content-Type", "application/zip")
	w.Header().Set("Content-Disposition", `attachment; filename="kalinka-streamer-logs-`+now.Format("20060102T150405Z")+`.zip"`)
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodHead {
		return ""
	}
	response := http.NewResponseController(w)
	_ = response.Flush()
	select {
	case h.journal <- struct{}{}:
		defer func() { <-h.journal }()
	case <-r.Context().Done():
		return ""
	}
	// The server's write timeout is sized for actions, not for every source at its own timeout.
	_ = response.SetWriteDeadline(time.Now().Add(time.Duration(len(logSources))*logSourceTimeout + time.Minute))
	out := &capBuffer{b: make([]byte, 0, logCap)}
	archive := zip.NewWriter(w)
	for _, s := range logSources {
		if h.writeLog(r.Context(), archive, s, since, now, out) != nil {
			return ""
		}
	}
	_ = archive.Close()
	return ""
}

// writeLog adds s's file to archive: its newest entries since since, oldest
// first, under a line saying what was read and why anything is missing.
func (h *handler) writeLog(ctx context.Context, archive *zip.Writer, s logSource, since, now time.Time, out *capBuffer) error {
	out.reset()
	reading, stop := context.WithTimeout(ctx, logSourceTimeout)
	defer stop()
	// Newest first, so the cap and the timeout cut the oldest entries.
	args := append([]string{"--since=@" + strconv.FormatInt(since.Unix(), 10), "--reverse", "--output=short-iso", "--no-pager", "--quiet"}, s.matches...)
	err := h.cfg.Journal(reading, out, args...)
	if ctx.Err() != nil {
		return ctx.Err()
	}
	text, note := out.b, ""
	switch {
	case out.cut:
		note = fmt.Sprintf("Older entries left out: this file keeps the newest %d MiB", logCap>>20)
	case errors.Is(err, context.DeadlineExceeded):
		note = fmt.Sprintf("journalctl was stopped after %s; older entries left out", logSourceTimeout)
	case err != nil:
		note = "journalctl failed: " + err.Error()
	case len(text) == 0:
		note = "No entries"
	}
	if out.cut || err != nil {
		text = whole(text)
	}
	header := "-- " + s.describe() + " since " + since.UTC().Format(time.RFC3339) + " --\n"
	if note != "" {
		header += "-- " + note + " --\n"
	}
	file, err := archive.CreateHeader(&zip.FileHeader{Name: s.file, Method: zip.Deflate, Modified: now})
	if err != nil {
		return err
	}
	if _, err := io.WriteString(file, header); err != nil {
		return err
	}
	return writeOldestFirst(file, text)
}

// entryStart is where the entry ending at end begins: the nearest line at or
// before end that is not one of a message's indented continuation lines.
func entryStart(text []byte, end int) int {
	start := end
	for start > 0 {
		start = bytes.LastIndexByte(text[:start-1], '\n') + 1
		if c := text[start]; c != ' ' && c != '\t' && c != '\n' {
			return start
		}
	}
	return 0
}

// whole drops the oldest entry of journalctl's newest-first text, which it
// may have been printing when its output stopped.
func whole(text []byte) []byte {
	return text[:entryStart(text, len(text))]
}

// writeOldestFirst writes journalctl's newest-first text in time order, each
// entry with its continuation lines.
func writeOldestFirst(w io.Writer, text []byte) error {
	for end := len(text); end > 0; {
		start := entryStart(text, end)
		entry := text[start:end]
		if end == len(text) && text[end-1] != '\n' {
			entry = append(entry[:len(entry):len(entry)], '\n')
		}
		if _, err := w.Write(entry); err != nil {
			return err
		}
		end = start
	}
	return nil
}

var errCapped = errors.New("the log cap is reached")

// capBuffer holds what it is given up to its capacity and refuses the rest,
// which ends journalctl at a broken pipe; cut says something was refused.
type capBuffer struct {
	b   []byte
	cut bool
}

func (c *capBuffer) Write(p []byte) (int, error) {
	room := cap(c.b) - len(c.b)
	if len(p) > room {
		c.b, c.cut = append(c.b, p[:room]...), true
		return room, errCapped
	}
	c.b = append(c.b, p...)
	return len(p), nil
}

func (c *capBuffer) reset() { c.b, c.cut = c.b[:0], false }

// firstBytes keeps the first bytes written to it and drops the rest without complaint.
type firstBytes struct{ b []byte }

func (f *firstBytes) Write(p []byte) (int, error) {
	f.b = append(f.b, p[:min(len(p), cap(f.b)-len(f.b))]...)
	return len(p), nil
}
