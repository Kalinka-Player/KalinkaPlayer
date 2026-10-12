package soundcard

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"os/exec"
	"slices"
	"strings"
	"testing"
)

// helperRun stands in for systemd-run, printing what the helper would and failing as told.
func helperRun(stdout, stderr string, fail bool, ran *[]string) func(*exec.Cmd) error {
	return func(cmd *exec.Cmd) error {
		*ran = cmd.Args
		_, _ = io.WriteString(cmd.Stdout, stdout)
		_, _ = io.WriteString(cmd.Stderr, stderr)
		if fail {
			return errors.New("exit status 1")
		}
		return nil
	}
}

func noChip() string { return "" }

func TestApply(t *testing.T) {
	for _, tc := range []struct {
		name, stdout string
		fail         bool
		reboot       bool
		err          error
	}{
		{name: "the boot configuration changed", stdout: "changed\n", reboot: true},
		{name: "nothing to restart for", stdout: "unchanged\n"},
		{name: "a tool's output before the verdict", stdout: "[ OK ] DietPi-Set_hardware\nchanged\n", reboot: true},
		{name: "helper failed", stdout: "", fail: true, err: ErrApply},
		{name: "a verdict from a failed helper", stdout: "changed\n", fail: true, err: ErrApply},
		{name: "no verdict", stdout: "done\n", err: ErrApply},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var ran []string
			a := &Applier{hatCard: noChip, run: helperRun(tc.stdout, "", tc.fail, &ran)}
			reboot, err := a.Apply(context.Background(), "hifiberry-digi")
			if reboot != tc.reboot || !errors.Is(err, tc.err) {
				t.Fatalf("Apply = %v, %v", reboot, err)
			}
		})
	}
}

func TestApplyWhileAnotherChangeHoldsTheHelper(t *testing.T) {
	a := &Applier{hatCard: noChip, run: func(*exec.Cmd) error {
		return exec.Command("sh", "-c", "exit 75").Run()
	}}
	if reboot, err := a.Apply(context.Background(), "hifiberry-digi"); reboot || !errors.Is(err, ErrBusy) {
		t.Fatalf("Apply = %v, %v", reboot, err)
	}
	a.run = func(*exec.Cmd) error { return exec.Command("sh", "-c", "exit 1").Run() }
	if _, err := a.Apply(context.Background(), "hifiberry-digi"); !errors.Is(err, ErrApply) {
		t.Fatalf("a failed helper read as %v", err)
	}
}

func TestApplyCommand(t *testing.T) {
	var ran []string
	a := &Applier{hatCard: noChip, run: helperRun("changed\n", "", false, &ran)}
	if _, err := a.Apply(context.Background(), "hifiberry-digi"); err != nil {
		t.Fatal(err)
	}
	if ran[0] != "systemd-run" || !slices.Contains(ran, "--unit=kalinka-soundcard-apply") || !slices.Contains(ran, "--wait") || !slices.Contains(ran, "--pipe") {
		t.Fatalf("command = %q", ran)
	}
	if tailArgs := ran[len(ran)-3:]; !slices.Equal(tailArgs, []string{HelperPath, "apply", "hifiberry-digi"}) {
		t.Fatalf("helper arguments = %q", tailArgs)
	}
}

func TestApplyNamesTheCardTheHATsChipSetsUp(t *testing.T) {
	var ran []string
	a := &Applier{hatCard: func() string { return "hifiberry-digi-pro" }, run: helperRun("unchanged\n", "", false, &ran)}
	if _, err := a.Apply(context.Background(), "hifiberry-dacplus-std"); err != nil {
		t.Fatal(err)
	}
	if tailArgs := ran[len(ran)-4:]; !slices.Equal(tailArgs, []string{HelperPath, "apply", "hifiberry-dacplus-std", "hifiberry-digi-pro"}) {
		t.Fatalf("helper arguments = %q", tailArgs)
	}
}

func TestApplyRefusesWhatIsNotCatalogued(t *testing.T) {
	ran := []string{}
	a := &Applier{hatCard: noChip, run: helperRun("changed\n", "", false, &ran)}
	for _, name := range []string{"hifiberry-dacplus", "--help", "none;reboot"} {
		if _, err := a.Apply(context.Background(), name); !errors.Is(err, ErrApply) || len(ran) != 0 {
			t.Fatalf("%q ran %q", name, ran)
		}
	}
	if reboot, err := NewApplier(true, noChip).Apply(context.Background(), "hifiberry-digi"); reboot || err != nil {
		t.Fatalf("simulated apply = %v, %v", reboot, err)
	}
	if _, err := NewApplier(true, noChip).Apply(context.Background(), "unknown"); !errors.Is(err, ErrApply) {
		t.Fatal("a simulated run accepted an unknown card")
	}
}

func TestApplyReportsTheHelpersOutputTail(t *testing.T) {
	var logs bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
	defer slog.SetDefault(previous)
	var ran []string
	noise := strings.Repeat("x", 2*keptOutput)
	a := &Applier{hatCard: noChip, run: helperRun("", noise+"dietpi-set_hardware: no such card", true, &ran)}
	if _, err := a.Apply(context.Background(), "hifiberry-digi"); err == nil {
		t.Fatal("failure lost")
	}
	if !strings.Contains(logs.String(), "no such card") || strings.Count(logs.String(), "x") > keptOutput+20 {
		t.Fatalf("journal = %d bytes", logs.Len())
	}
}
