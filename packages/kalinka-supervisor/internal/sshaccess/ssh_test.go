package sshaccess

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/netip"
	"os/exec"
	"strings"
	"testing"

	"kalinka/supervisor/internal/protocol"
)

var lanPeer = netip.MustParseAddr("192.168.1.20")

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
	if err := s.Enable(context.Background(), password, lanPeer); err != ErrSetup {
		t.Fatal("helper failure was not replaced by a fixed error")
	}
}

func TestSetupNamesThePeerAsSSHWouldSeeIt(t *testing.T) {
	for from, want := range map[string]string{"192.168.1.20": "192.168.1.20", "::ffff:192.168.1.20": "192.168.1.20", "fe80::1%wlan0": "fe80::1", "fd00::1": "fd00::1"} {
		s := New(false)
		var ran []string
		s.run = func(cmd *exec.Cmd) error { ran = cmd.Args; return nil }
		if err := s.Enable(context.Background(), protocol.NewSecret("valid-test-password"), netip.MustParseAddr(from)); err != nil || ran[len(ran)-2] != "/usr/lib/kalinka-supervisor/ssh-setup.sh" || ran[len(ran)-1] != want {
			t.Errorf("%s reached the helper as %q: %v", from, ran, err)
		}
	}
}

func TestSetupTellsARefusalFromAFailure(t *testing.T) {
	s := New(false)
	for status, want := range map[int]error{helperPolicyUnknown: ErrPolicyUnknown, helperPasswordsRefused: ErrPasswordsRefused, 1: ErrSetup, 255: ErrSetup} {
		s.run = func(*exec.Cmd) error { return exec.Command("sh", "-c", fmt.Sprintf("exit %d", status)).Run() }
		if err := s.Enable(context.Background(), protocol.NewSecret("valid-test-password"), lanPeer); err != want {
			t.Errorf("helper exit %d read as %v", status, err)
		}
	}
}

func TestSimulationAndInvalidRequestsNeverRunCommands(t *testing.T) {
	for _, simulated := range []bool{false, true} {
		s := New(simulated)
		s.run = func(*exec.Cmd) error { t.Fatal("unexpected command"); return nil }
		if s.Enable(context.Background(), protocol.NewSecret("short"), lanPeer) != ErrSetup {
			t.Fatal("invalid password was accepted")
		}
		if s.Enable(context.Background(), protocol.NewSecret("valid-test-password"), netip.Addr{}) != ErrSetup {
			t.Fatal("a request from no address was accepted")
		}
		if simulated && s.Enable(context.Background(), protocol.NewSecret("valid-test-password"), lanPeer) != nil {
			t.Fatal("simulation failed")
		}
	}
}
