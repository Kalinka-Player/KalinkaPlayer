package soundcard

import (
	"context"
	"errors"
	"log/slog"
	"os/exec"
	"strings"
)

const (
	// BootUnit applies the card named in DietPi's dietpi.txt while the box starts.
	BootUnit = "kalinka-soundcard.service"
	// ApplyUnit runs a choice made from the control API; while it exists, one is being applied.
	ApplyUnit = "kalinka-soundcard-apply.service"
	// HelperPath is the packaged script both units run.
	HelperPath = "/usr/lib/kalinka-supervisor/soundcard.sh"
)

var ErrApply = errors.New("the sound card could not be applied")

// keptOutput bounds how much of the helper's diagnostics reaches the journal.
const keptOutput = 4096

// Applier makes a catalogued card the box's own through the packaged helper,
// which systemd runs outside the supervisor's sandbox: it edits the boot
// partition, /etc/modprobe.d and /etc/asound.conf, all read-only in there.
type Applier struct {
	simulated bool
	hatCard   func() string
	run       func(*exec.Cmd) error
}

// NewApplier applies cards for real unless simulated; hatCard names the card
// the firmware sets up from the fitted HAT's ID chip, which no other may load
// on top of. Detector.HATCard is the production one.
func NewApplier(simulated bool, hatCard func() string) *Applier {
	return &Applier{simulated: simulated, hatCard: hatCard, run: func(cmd *exec.Cmd) error { return cmd.Run() }}
}

// Apply reports whether the boot configuration changed, in which case the card loads only after a restart.
func (a *Applier) Apply(ctx context.Context, name string) (bool, error) {
	card, ok := Lookup(name)
	if !ok {
		return false, ErrApply
	}
	if a.simulated {
		slog.Info("Simulated sound card", "name", name)
		return false, nil
	}
	args := []string{"--quiet", "--wait", "--pipe", "--collect",
		"--unit=" + strings.TrimSuffix(ApplyUnit, ".service"), "--service-type=exec",
		"--property=RuntimeMaxSec=80s", "--property=TimeoutStopSec=5s", HelperPath, "apply", card.Name}
	if chip := a.hatCard(); chip != "" {
		args = append(args, chip)
	}
	cmd := exec.CommandContext(ctx, "systemd-run", args...)
	verdict, diagnostics := &tail{}, &tail{}
	cmd.Stdout, cmd.Stderr = verdict, diagnostics
	err := a.run(cmd)
	switch result := verdict.lastLine(); {
	case err == nil && result == "changed":
		slog.Info("Sound card applied; the box restarts to load it", "name", name)
		return true, nil
	case err == nil && result == "unchanged":
		slog.Info("Sound card applied", "name", name)
		return false, nil
	}
	slog.Warn("Sound card helper failed", "name", name, "output", diagnostics.String())
	return false, ErrApply
}

// tail keeps the last keptOutput bytes written to it.
type tail struct{ b []byte }

func (t *tail) Write(p []byte) (int, error) {
	t.b = append(t.b, p...)
	if len(t.b) > keptOutput {
		t.b = t.b[len(t.b)-keptOutput:]
	}
	return len(p), nil
}

func (t *tail) String() string { return strings.TrimSpace(string(t.b)) }

// lastLine is the helper's verdict, whatever a tool it ran printed before it.
func (t *tail) lastLine() string {
	s := t.String()
	return strings.TrimSpace(s[strings.LastIndexByte(s, '\n')+1:])
}
