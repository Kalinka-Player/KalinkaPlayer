package control

import (
	"archive/zip"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// journalAnswer is what journalctl prints, newest entry first, and how it ends.
type journalAnswer struct {
	out string
	err error
}

// fakeJournal answers for the source whose matches include a key of answers,
// and prints nothing, as for units the journal has never seen, for the rest.
type fakeJournal struct {
	answers map[string]journalAnswer
	// before runs as journalctl starts, with the run's context.
	before func(context.Context)

	mu    sync.Mutex
	calls [][]string
}

func (f *fakeJournal) run(ctx context.Context, stdout io.Writer, args ...string) error {
	f.mu.Lock()
	f.calls = append(f.calls, args)
	f.mu.Unlock()
	if f.before != nil {
		f.before(ctx)
	}
	for key, a := range f.answers {
		if slices.Contains(args, key) {
			if _, err := io.WriteString(stdout, a.out); err != nil {
				return err
			}
			return a.err
		}
	}
	return nil
}

func (f *fakeJournal) runs() [][]string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.calls)
}

func logsHandler(t *testing.T, journal *fakeJournal) *handler {
	t.Helper()
	// systemd not answering, as on a streamer whose Core is down with it: the download needs neither.
	h := newTestHandler(t, &fakeCoordinator{refuse: refuse(CodeUnavailable)}, true).(*handler)
	h.cfg.Journal = journal.run
	return h
}

func unzip(t *testing.T, body []byte) ([]string, map[string]string) {
	t.Helper()
	archive, err := zip.NewReader(bytes.NewReader(body), int64(len(body)))
	if err != nil {
		t.Fatalf("not a zip: %v", err)
	}
	var names []string
	files := map[string]string{}
	for _, f := range archive.File {
		r, err := f.Open()
		if err != nil {
			t.Fatal(err)
		}
		b, err := io.ReadAll(r)
		r.Close()
		if err != nil {
			t.Fatalf("%s: %v", f.Name, err)
		}
		names = append(names, f.Name)
		files[f.Name] = string(b)
	}
	return names, files
}

const (
	coreNewestFirst = "2026-11-23T09:59:58+0000 kalinka systemd[1]: Started kalinka.service - Kalinka.\n" +
		"2026-11-23T09:59:40+0000 kalinka kalinka[812]: Traceback (most recent call last):\n" +
		"                                                 File \"/opt/kalinka/server.py\", line 1\n" +
		"2026-11-23T09:50:00+0000 kalinka systemd[1]: kalinka.service: Failed with result 'exit-code'.\n"
	networkNewestFirst = "2026-11-23T09:58:00+0000 kalinka dhclient[700]: DHCPACK of 192.168.1.50 from 192.168.1.1\n" +
		"2026-11-23T09:57:59+0000 kalinka wpa_supplicant[650]: wlan0: CTRL-EVENT-CONNECTED - Connection to 00:11:22:33:44:55 completed\n"
	kernelNewestFirst = "2026-11-23T09:40:00+0000 kalinka kernel: Linux version 6.12.0\n"
)

func everySource() *fakeJournal {
	return &fakeJournal{answers: map[string]journalAnswer{
		"--unit=kalinka.service":            {out: coreNewestFirst},
		"--unit=kalinka-supervisor.service": {err: errors.New("exit status 1: Failed to add filter for units: No data available")},
		"--unit=NetworkManager.service":     {out: networkNewestFirst},
		"_TRANSPORT=kernel":                 {out: kernelNewestFirst},
	}}
}

func TestLogsDownloadEachSourceOldestFirstWithoutCore(t *testing.T) {
	journal := everySource()
	var deadlines []time.Duration
	journal.before = func(ctx context.Context) {
		deadline, _ := ctx.Deadline()
		deadlines = append(deadlines, time.Until(deadline))
	}
	since := time.Date(2026, 11, 22, 10, 0, 0, 0, time.UTC)
	w := serve(logsHandler(t, journal), http.MethodGet, "/v1/logs?since=2026-11-22T11:00:00%2B01:00", "")
	if w.Code != http.StatusOK || w.Header().Get("Content-Type") != "application/zip" || !w.Flushed {
		t.Fatalf("download = %d %q, flushed %v", w.Code, w.Header().Get("Content-Type"), w.Flushed)
	}
	if !regexp.MustCompile(`^attachment; filename="kalinka-streamer-logs-\d{8}T\d{6}Z\.zip"$`).MatchString(w.Header().Get("Content-Disposition")) {
		t.Fatalf("Content-Disposition = %q", w.Header().Get("Content-Disposition"))
	}
	names, files := unzip(t, w.Body.Bytes())
	if want := []string{"core.log", "renderer.log", "supervisor.log", "network.log", "kernel.log"}; !slices.Equal(names, want) {
		t.Fatalf("files = %v, want %v", names, want)
	}
	window := " since 2026-11-22T10:00:00Z --\n"
	for name, want := range map[string]string{
		"core.log": "-- kalinka.service kalinka-upgrade.service kalinka-restart.service kalinka-plugins.service" + window +
			"2026-11-23T09:50:00+0000 kalinka systemd[1]: kalinka.service: Failed with result 'exit-code'.\n" +
			"2026-11-23T09:59:40+0000 kalinka kalinka[812]: Traceback (most recent call last):\n" +
			"                                                 File \"/opt/kalinka/server.py\", line 1\n" +
			"2026-11-23T09:59:58+0000 kalinka systemd[1]: Started kalinka.service - Kalinka.\n",
		"renderer.log": "-- kalinka-renderer.service kalinka-renderer-upgrade.service" + window + "-- No entries --\n",
		"supervisor.log": "-- kalinka-supervisor.service kalinka-soundcard.service kalinka-soundcard-apply.service " +
			"kalinka-ssh-setup.service kalinka-reinstall.service kalinka-plugin-removal.service" + window +
			"-- journalctl failed: exit status 1: Failed to add filter for units: No data available --\n",
		"kernel.log": "-- _TRANSPORT=kernel" + window + "2026-11-23T09:40:00+0000 kalinka kernel: Linux version 6.12.0\n",
	} {
		if files[name] != want {
			t.Errorf("%s =\n%s\nwant\n%s", name, files[name], want)
		}
	}
	if !strings.HasSuffix(files["network.log"], window+
		"2026-11-23T09:57:59+0000 kalinka wpa_supplicant[650]: wlan0: CTRL-EVENT-CONNECTED - Connection to 00:11:22:33:44:55 completed\n"+
		"2026-11-23T09:58:00+0000 kalinka dhclient[700]: DHCPACK of 192.168.1.50 from 192.168.1.1\n") {
		t.Errorf("network.log =\n%s", files["network.log"])
	}

	calls := journal.runs()
	if len(calls) != len(logSources) {
		t.Fatalf("journalctl ran %d times", len(calls))
	}
	for i, args := range calls {
		for _, want := range []string{"--since=@" + strconv.FormatInt(since.Unix(), 10), "--reverse", "--output=short-iso", "--no-pager", "--quiet"} {
			if !slices.Contains(args, want) {
				t.Errorf("run %d lacks %s: %v", i, want, args)
			}
		}
		if deadlines[i] <= 0 || deadlines[i] > logSourceTimeout {
			t.Errorf("run %d has %v to finish", i, deadlines[i])
		}
	}
	for _, unit := range []string{"NetworkManager.service", "wpa_supplicant.service", "dhcpcd.service", "systemd-networkd.service",
		"networking.service", "ifup@*.service", "kalinka-wifi@*.service"} {
		if !slices.Contains(calls[3], "--unit="+unit) {
			t.Errorf("the network file leaves out %s", unit)
		}
	}
	kernel := strings.Join(calls[4], " ")
	if strings.Contains(kernel, "--unit") || slices.Contains(calls[4], "-k") || slices.Contains(calls[4], "--dmesg") {
		t.Errorf("the kernel file is limited to a unit or to this boot: %s", kernel)
	}
}

func TestLogsReachBackADayByDefault(t *testing.T) {
	journal := &fakeJournal{}
	before := time.Now().Add(-logWindow).Unix()
	serve(logsHandler(t, journal), http.MethodGet, "/v1/logs", "")
	after := time.Now().Add(-logWindow).Unix()
	for _, args := range journal.runs() {
		since, err := strconv.ParseInt(strings.TrimPrefix(args[0], "--since=@"), 10, 64)
		if err != nil || since < before || since > after {
			t.Fatalf("since %q, want between %d and %d", args[0], before, after)
		}
	}
}

func TestLogsRefuseASinceThatIsNoTime(t *testing.T) {
	for _, since := range []string{"yesterday", "1700000000", "2026-11-22"} {
		journal := &fakeJournal{}
		if status, code := detail(t, serve(logsHandler(t, journal), http.MethodGet, "/v1/logs?since="+since, "")); status != 400 || code != "invalid_since" {
			t.Errorf("since=%s = %d %s", since, status, code)
		}
		if len(journal.runs()) != 0 {
			t.Errorf("since=%s ran journalctl", since)
		}
	}
}

func TestLogsHeadReadsNoJournal(t *testing.T) {
	journal := everySource()
	w := serve(logsHandler(t, journal), http.MethodHead, "/v1/logs", "")
	if w.Code != http.StatusOK || w.Header().Get("Content-Type") != "application/zip" || w.Body.Len() != 0 || len(journal.runs()) != 0 {
		t.Fatalf("HEAD = %d %q, %d bytes, %d runs", w.Code, w.Header().Get("Content-Type"), w.Body.Len(), len(journal.runs()))
	}
}

func TestLogsWithoutAJournalAreNotOffered(t *testing.T) {
	h := newTestHandler(t, &fakeCoordinator{}, true)
	if status, code := detail(t, serve(h, http.MethodGet, "/v1/logs", "")); status != 404 || code != "not_found" {
		t.Fatalf("logs without a journal = %d %s", status, code)
	}
}

// pipe is how journalctl's output reaches the runner: in pieces, until a write fails.
func pipe(w io.Writer, text []byte) (int, error) {
	written := 0
	for len(text) > 0 {
		n, err := w.Write(text[:min(len(text), 64<<10)])
		written += n
		if err != nil {
			return written, err
		}
		text = text[n:]
	}
	return written, nil
}

func TestLogsKeepTheNewestEntriesUnderTheCap(t *testing.T) {
	var newestFirst bytes.Buffer
	for i := 0; newestFirst.Len() <= logCap+logCap/4; i++ {
		fmt.Fprintf(&newestFirst, "2026-11-23T09:00:00+0000 kalinka kernel: entry %07d\n", i)
		if i%10 == 5 {
			newestFirst.WriteString("                                   a continuation line\n")
		}
	}
	var accepted int
	journal := &fakeJournal{}
	h := logsHandler(t, journal)
	h.cfg.Journal = func(ctx context.Context, stdout io.Writer, args ...string) error {
		if !slices.Contains(args, "_TRANSPORT=kernel") {
			return nil
		}
		n, err := pipe(stdout, newestFirst.Bytes())
		accepted = n
		return err
	}
	_, files := unzip(t, serve(h, http.MethodGet, "/v1/logs", "").Body.Bytes())
	if accepted > logCap {
		t.Fatalf("%d bytes were taken from journalctl, more than the %d cap", accepted, logCap)
	}
	lines := strings.Split(strings.TrimSuffix(files["kernel.log"], "\n"), "\n")
	if lines[1] != "-- Older entries left out: this file keeps the newest 4 MiB --" {
		t.Fatalf("the file does not say it was cut: %q", lines[:2])
	}
	if last := lines[len(lines)-1]; last != "2026-11-23T09:00:00+0000 kalinka kernel: entry 0000000" {
		t.Fatalf("the newest entry is not last: %q", last)
	}
	previous := -1
	for i, line := range lines[2:] {
		var n int
		if _, err := fmt.Sscanf(line, "2026-11-23T09:00:00+0000 kalinka kernel: entry %d", &n); err != nil {
			if line != "                                   a continuation line" || i == 0 {
				t.Fatalf("line %d is not a whole entry: %q", i+2, line)
			}
			if previous%10 != 5 {
				t.Fatalf("a continuation line follows entry %d", previous)
			}
			continue
		}
		if previous != -1 && n != previous-1 {
			t.Fatalf("entry %d follows entry %d", n, previous)
		}
		previous = n
	}
	if body := len(files["kernel.log"]) - len(lines[0]) - len(lines[1]) - 2; body > logCap {
		t.Fatalf("the file holds %d bytes of entries", body)
	}
}

func TestLogsKeepWhatJournalctlPrintedBeforeItWasStopped(t *testing.T) {
	journal := &fakeJournal{answers: map[string]journalAnswer{"_TRANSPORT=kernel": {out: coreNewestFirst, err: context.DeadlineExceeded}}}
	_, files := unzip(t, serve(logsHandler(t, journal), http.MethodGet, "/v1/logs", "").Body.Bytes())
	want := "-- journalctl was stopped after 20s; older entries left out --\n" +
		"2026-11-23T09:59:40+0000 kalinka kalinka[812]: Traceback (most recent call last):\n" +
		"                                                 File \"/opt/kalinka/server.py\", line 1\n" +
		"2026-11-23T09:59:58+0000 kalinka systemd[1]: Started kalinka.service - Kalinka.\n"
	if !strings.HasSuffix(files["kernel.log"], want) {
		t.Fatalf("kernel.log =\n%s", files["kernel.log"])
	}
}

func TestWriteOldestFirstKeepsEachEntryWhole(t *testing.T) {
	for _, tc := range []struct{ newestFirst, want string }{
		{"", ""},
		{"b\na\n", "a\nb\n"},
		{"b\n  b2\n\n  b3\na\n", "a\nb\n  b2\n\n  b3\n"},
		{"b\na", "a\nb\n"},
		{"  orphan\nb\n", "b\n  orphan\n"},
	} {
		var out bytes.Buffer
		if err := writeOldestFirst(&out, []byte(tc.newestFirst)); err != nil || out.String() != tc.want {
			t.Errorf("%q = %q, want %q", tc.newestFirst, out.String(), tc.want)
		}
	}
	if got := string(whole([]byte("b\na\n  a2\n"))); got != "b\n" {
		t.Errorf("whole = %q", got)
	}
}

func TestLogsStartBeforeJournalctlAnswersAndTakeTurns(t *testing.T) {
	var mu sync.Mutex
	reading, most := 0, 0
	release := make(chan struct{})
	journal := everySource()
	journal.before = func(context.Context) {
		mu.Lock()
		reading++
		most = max(most, reading)
		mu.Unlock()
		<-release
		mu.Lock()
		reading--
		mu.Unlock()
	}
	server := httptest.NewServer(logsHandler(t, journal))
	defer server.Close()
	// Released late rather than never, so a download that waits for journalctl fails instead of hanging.
	stopper := time.AfterFunc(3*time.Second, func() { close(release) })
	responses := make(chan *http.Response, 2)
	for range 2 {
		go func() {
			response, err := http.Get(server.URL + "/v1/logs")
			if err != nil {
				t.Error(err)
			}
			responses <- response
		}()
	}
	started := time.Now()
	var got []*http.Response
	for range 2 {
		if r := <-responses; r != nil {
			got = append(got, r)
		}
	}
	if !stopper.Stop() {
		t.Fatalf("the responses waited for journalctl: they started after %v", time.Since(started))
	}
	close(release)
	for _, response := range got {
		body, err := io.ReadAll(response.Body)
		response.Body.Close()
		if err != nil || response.StatusCode != http.StatusOK {
			t.Fatalf("download = %d: %v", response.StatusCode, err)
		}
		if names, _ := unzip(t, body); len(names) != len(logSources) {
			t.Fatalf("files = %v", names)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if most != 1 {
		t.Fatalf("%d downloads read the journal at once", most)
	}
}

func TestLogsOutliveTheServersWriteTimeout(t *testing.T) {
	journal := everySource()
	journal.before = func(context.Context) { time.Sleep(150 * time.Millisecond) }
	server := httptest.NewUnstartedServer(logsHandler(t, journal))
	server.Config.WriteTimeout = 500 * time.Millisecond
	server.Start()
	defer server.Close()
	response, err := http.Get(server.URL + "/v1/logs")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatalf("the download broke off: %v", err)
	}
	if names, _ := unzip(t, body); len(names) != len(logSources) {
		t.Fatalf("files = %v", names)
	}
}

// journalctlOnPath puts a shell script named journalctl first on PATH.
func journalctlOnPath(t *testing.T, script string) {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "journalctl"), []byte("#!/bin/sh\n"+script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func TestRunJournalStopsAtTheCap(t *testing.T) {
	journalctlOnPath(t, "exec yes '2026-11-23T09:00:00+0000 kalinka kernel: entry'\n")
	out := &capBuffer{b: make([]byte, 0, 64<<10)}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	RunJournal(ctx, out, "--reverse")
	if !out.cut || len(out.b) != cap(out.b) || ctx.Err() != nil {
		t.Fatalf("cut %v at %d bytes, %v", out.cut, len(out.b), ctx.Err())
	}
}

func TestRunJournalSaysWhyItFailed(t *testing.T) {
	journalctlOnPath(t, "echo 'Failed to add filter for units: No data available' >&2\nexit 1\n")
	err := RunJournal(context.Background(), io.Discard, "--unit=nothing@*.service")
	if err == nil || err.Error() != "exit status 1: Failed to add filter for units: No data available" {
		t.Fatalf("err = %v", err)
	}
}

func TestRunJournalIsNotStoppedByItsWarnings(t *testing.T) {
	journalctlOnPath(t, `i=0; while [ $i -lt 200 ]; do echo "Journal file /var/log/journal/x/system@$i.journal~ is truncated, ignoring file." >&2; i=$((i+1)); done; echo "$@"`+"\n")
	var out bytes.Buffer
	if err := RunJournal(context.Background(), &out, "--reverse", "_TRANSPORT=kernel"); err != nil || out.String() != "--reverse _TRANSPORT=kernel\n" {
		t.Fatalf("%q, %v", out.String(), err)
	}
}

func TestRunJournalEndsWithItsContext(t *testing.T) {
	journalctlOnPath(t, "exec sleep 30\n")
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	started := time.Now()
	if err := RunJournal(ctx, io.Discard); !errors.Is(err, context.DeadlineExceeded) || time.Since(started) > 5*time.Second {
		t.Fatalf("%v after %v", err, time.Since(started))
	}
}
