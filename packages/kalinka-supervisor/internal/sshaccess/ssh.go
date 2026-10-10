// Package sshaccess provisions the dashboard's administrator login outside the supervisor's sandbox.
package sshaccess

import (
	"context"
	"errors"
	"os/exec"
	"strings"

	"kalinka/supervisor/internal/protocol"
)

const Username = "kalinka-admin"

var ErrSetup = errors.New("SSH setup failed")

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

func (s *Setup) Enable(ctx context.Context, password protocol.Secret) error {
	if !ValidPassword(password.Reveal()) {
		return ErrSetup
	}
	if s.simulated {
		return nil
	}
	cmd := exec.CommandContext(ctx, "systemd-run", "--quiet", "--wait", "--pipe", "--collect",
		"--unit=kalinka-ssh-setup", "--service-type=exec", "--property=RuntimeMaxSec=20s",
		"--property=TimeoutStopSec=2s", "--property=UMask=0077", "--property=LimitCORE=0",
		"/usr/lib/kalinka-supervisor/ssh-setup.sh")
	cmd.Stdin = strings.NewReader(password.Reveal() + "\n")
	// Discard helper output: even a failing account tool must not expose its input.
	if s.run(cmd) != nil {
		return ErrSetup
	}
	return nil
}
