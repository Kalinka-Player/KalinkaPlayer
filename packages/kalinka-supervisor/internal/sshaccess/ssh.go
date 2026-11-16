// Package sshaccess provisions the dashboard's administrator login outside the supervisor's sandbox.
package sshaccess

import (
	"context"
	"errors"
	"net/netip"
	"os/exec"
	"strings"

	"kalinka/supervisor/internal/protocol"
)

const Username = "kalinka-admin"

var ErrSetup = errors.New("SSH setup failed")

// ErrPasswordsRefused reports the login made and SSH on, but an OpenSSH policy
// the helper keeps refusing that login a password.
var ErrPasswordsRefused = errors.New("the SSH server refuses the login a password")

// ErrLoginRefused reports the login made and SSH on, but OpenSSH's AllowUsers,
// AllowGroups, DenyUsers or DenyGroups turning that login away.
var ErrLoginRefused = errors.New("the SSH server turns the login away")

// The helper's exit statuses for those errors; systemd-run passes them through.
const (
	helperLoginRefused     = 77
	helperPasswordsRefused = 78
)

// Setup runs the fixed SSH helper, passing credentials only through standard input.
type Setup struct {
	simulated bool
	run       func(*exec.Cmd) error
}

func New(simulated bool) *Setup {
	return &Setup{simulated: simulated, run: func(cmd *exec.Cmd) error { return cmd.Run() }}
}

func ValidPassword(password string) bool {
	if len(password) < 12 || len(password) > 128 {
		return false
	}
	for _, c := range password {
		if c < 32 || c > 126 {
			return false
		}
	}
	return true
}

// Enable sets the login's password and starts SSH; from is the address the
// request came from, which OpenSSH's policy is read for.
func (s *Setup) Enable(ctx context.Context, password protocol.Secret, from netip.Addr) error {
	if !ValidPassword(password.Reveal()) || !from.IsValid() {
		return ErrSetup
	}
	if s.simulated {
		return nil
	}
	cmd := exec.CommandContext(ctx, "systemd-run", "--quiet", "--wait", "--pipe", "--collect",
		"--unit=kalinka-ssh-setup", "--service-type=exec", "--property=RuntimeMaxSec=20s",
		"--property=TimeoutStopSec=2s", "--property=UMask=0077", "--property=LimitCORE=0",
		"/usr/lib/kalinka-supervisor/ssh-setup.sh", from.Unmap().WithZone("").String())
	cmd.Stdin = strings.NewReader(password.Reveal() + "\n")
	// Discard helper output: even a failing account tool must not expose its input.
	err := s.run(cmd)
	if exit := (*exec.ExitError)(nil); errors.As(err, &exit) {
		switch exit.ExitCode() {
		case helperLoginRefused:
			return ErrLoginRefused
		case helperPasswordsRefused:
			return ErrPasswordsRefused
		}
	}
	if err != nil {
		return ErrSetup
	}
	return nil
}
