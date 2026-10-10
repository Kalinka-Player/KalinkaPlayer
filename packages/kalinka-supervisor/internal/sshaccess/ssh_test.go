package sshaccess

import (
	"context"
	"errors"
	"io"
	"os/exec"
	"strings"
	"testing"

	"kalinka/supervisor/internal/protocol"
)

func TestPasswordValidation(t *testing.T) {
	for _, password := range []string{"", "short", strings.Repeat("a", 129), "long-password\nroot:hijack", "long-password\r", "long-password\x00", "long-password\x7f", "long-passwordé"} {
		if ValidPassword(password) {
			t.Error("accepted an invalid password")
		}
	}
	for _, password := range []string{strings.Repeat("a", 12), strings.Repeat("z", 128), " spaces : $() ` \\"} {
		if !ValidPassword(password) {
			t.Error("rejected a valid password")
		}
	}
}

func TestSetupKeepsPasswordOffCommandLineAndOutOfErrors(t *testing.T) {
	password := protocol.NewSecret("secret with $() ` : \\")
	defer password.Clear()
	s := New(false)
	s.run = func(cmd *exec.Cmd) error {
		if strings.Contains(strings.Join(cmd.Args, " "), password.Reveal()) || cmd.Stdout != nil || cmd.Stderr != nil {
			t.Fatal("credentials or helper output could be exposed")
		}
		input, err := io.ReadAll(cmd.Stdin)
		if err != nil || string(input) != password.Reveal()+"\n" {
			t.Fatal("password was not supplied on stdin")
		}
		for _, arg := range []string{"--wait", "--pipe", "--collect", "--unit=kalinka-ssh-setup", "--property=RuntimeMaxSec=20s", "/usr/lib/kalinka-supervisor/ssh-setup.sh"} {
			if !strings.Contains(strings.Join(cmd.Args, " "), arg) {
				t.Errorf("missing helper argument %s", arg)
			}
		}
		return errors.New(password.Reveal())
	}
	if err := s.Enable(context.Background(), password); err != ErrSetup {
		t.Fatal("helper failure was not replaced by a fixed error")
	}
}

func TestSimulationAndInvalidPasswordsNeverRunCommands(t *testing.T) {
	for _, simulated := range []bool{false, true} {
		s := New(simulated)
		s.run = func(*exec.Cmd) error { t.Fatal("unexpected command"); return nil }
		if s.Enable(context.Background(), protocol.NewSecret("short")) != ErrSetup {
			t.Fatal("invalid password was accepted")
		}
		if simulated && s.Enable(context.Background(), protocol.NewSecret("valid-test-password")) != nil {
			t.Fatal("simulation failed")
		}
	}
}
