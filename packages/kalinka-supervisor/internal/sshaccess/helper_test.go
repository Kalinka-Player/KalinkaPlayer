package sshaccess

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

const fakeAccountTools = `#!/bin/bash
printf '%s %s\n' "${0##*/}" "$*" >> "$SSH_TEST_DIR/calls"
card_policy=$KALINKA_SSH_ROOT/etc/ssh/sshd_config.d/kalinka-card-kalinka-admin.conf
case ${0##*/} in
  systemctl)
    case $1 in
      show) if [[ $4 == "$SSH_TEST_SERVER" || $4 == "$SSH_TEST_ACTIVE" ]]; then echo loaded; else echo not-found; fi ;;
      is-active) [[ $3 == "$SSH_TEST_ACTIVE" || -f $SSH_TEST_DIR/started ]] ;;
      enable) [[ $SSH_TEST_FAIL != start ]] || exit 1; printf '%s' "$3" > "$SSH_TEST_DIR/started" ;;
      reload-or-restart) [[ -f $SSH_TEST_DIR/started && ! -e $card_policy ]] ;;
    esac ;;
  getent)
    if [[ $1 == group ]]; then exit 0; fi
    [[ -n $SSH_TEST_ACCOUNT ]] || exit 2
    printf '%s\n' "$SSH_TEST_ACCOUNT" ;;
  useradd|usermod) [[ $SSH_TEST_FAIL != account ]] ;;
  chpasswd)
    IFS= read -r credential
    printf '%s' "$credential" > "$SSH_TEST_DIR/credential"
    [[ $SSH_TEST_FAIL != password ]] ;;
  sshd)
    [[ $SSH_TEST_FAIL != policy ]] || exit 255
    printf 'port 22\npermitrootlogin without-password\n'
    if [[ -e $card_policy ]]; then echo 'passwordauthentication no'; else echo "passwordauthentication ${SSH_TEST_PASSWORDS:-yes}"; fi
    echo "authenticationmethods ${SSH_TEST_METHODS:-any}"
    echo 'kbdinteractiveauthentication no' ;;
esac
`

const (
	defaultPassword = "test: with ` $() \\"
	policyCheck     = "sshd -T -C user=kalinka-admin,host=localhost,addr=127.0.0.1"
)

// helper is ssh-setup.sh with only fake tools and rm on its PATH, the root it
// writes under, and, once run, what it did to them.
type helper struct {
	root, tools, calls, output string
	status                     int
}

func newHelper(t *testing.T) *helper {
	t.Helper()
	h := &helper{root: t.TempDir(), tools: t.TempDir()}
	for _, name := range []string{"systemctl", "getent", "useradd", "usermod", "chpasswd", "sshd"} {
		put(t, filepath.Join(h.tools, name), fakeAccountTools, 0700)
	}
	rm, err := exec.LookPath("rm")
	if err != nil {
		t.Skip("the helper needs rm")
	}
	if err := os.Symlink(rm, filepath.Join(h.tools, "rm")); err != nil {
		t.Fatal(err)
	}
	return h
}

func put(t *testing.T, path, content string, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), mode); err != nil {
		t.Fatal(err)
	}
}

func (h *helper) read(name string) string {
	b, _ := os.ReadFile(filepath.Join(h.tools, name))
	return string(b)
}

func (h *helper) cardPolicy() string {
	return filepath.Join(h.root, "etc/ssh/sshd_config.d/kalinka-card-kalinka-admin.conf")
}

// putCardPolicy writes the drop-in card-settings.py leaves when Imager gave kalinka-admin keys only.
func (h *helper) putCardPolicy(t *testing.T) {
	put(t, h.cardPolicy(), "Match User kalinka-admin\n\tPasswordAuthentication no\n", 0644)
}

func (h *helper) run(t *testing.T, password string, env ...string) {
	t.Helper()
	cmd := exec.Command("/bin/bash", "../../ssh-setup.sh")
	cmd.Env = append([]string{"PATH=" + h.tools, "SSH_TEST_DIR=" + h.tools, "KALINKA_SSH_ROOT=" + h.root}, env...)
	cmd.Stdin = strings.NewReader(password + "\n")
	output, err := cmd.CombinedOutput()
	if exit := (*exec.ExitError)(nil); err != nil && !errors.As(err, &exit) {
		t.Fatal(err)
	}
	h.output, h.status, h.calls = string(output), cmd.ProcessState.ExitCode(), h.read("calls")
	if strings.Contains(h.calls, password) || strings.Contains(h.output, password) {
		t.Fatal("password leaked")
	}
}

// outcome is "" for a usable password, "refused" for the status the supervisor reports, "failed" for any other.
func (h *helper) outcome() string {
	switch {
	case h.status == helperPasswordsRefused:
		return "refused"
	case h.status != 0:
		return "failed"
	}
	return ""
}

func TestHelperSetsOnlyItsOwnLoginBeforeStartingSSH(t *testing.T) {
	for _, tc := range []struct {
		name, server, active, account, fail, password string
		ok                                            bool
	}{
		{name: "DietPi", server: "dropbear.service", ok: true},
		{name: "Debian", server: "ssh.service", ok: true},
		{name: "sshd", server: "sshd.service", ok: true},
		{name: "prefer running server", server: "dropbear.service", active: "ssh.service", ok: true},
		{name: "reset managed account", server: "ssh.service", account: "kalinka-admin:x:1001:1001:Kalinka dashboard administrator:/home/kalinka-admin:/bin/bash", ok: true},
		{name: "unrelated account", server: "ssh.service", account: "kalinka-admin:x:1001:1001:Someone else:/home/kalinka-admin:/bin/bash"},
		{name: "root alias", server: "ssh.service", account: "kalinka-admin:x:0:0:Kalinka dashboard administrator:/home/kalinka-admin:/bin/bash"},
		{name: "no SSH server"},
		{name: "account failure", server: "ssh.service", fail: "account"},
		{name: "password failure", server: "ssh.service", fail: "password"},
		{name: "start failure", server: "ssh.service", fail: "start"},
		{name: "invalid password", server: "ssh.service", password: "short"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			password := tc.password
			if password == "" {
				password = defaultPassword
			}
			h := newHelper(t)
			h.run(t, password, "SSH_TEST_SERVER="+tc.server, "SSH_TEST_ACTIVE="+tc.active, "SSH_TEST_ACCOUNT="+tc.account, "SSH_TEST_FAIL="+tc.fail)
			if (h.status == 0) != tc.ok {
				t.Fatalf("helper success = %v, want %v: %s", h.status == 0, tc.ok, h.output)
			}
			if h.status == helperPasswordsRefused {
				t.Fatal("a failed setup reads as an SSH server refusing passwords")
			}
			started := h.read("started")
			if tc.ok {
				want := tc.server
				if tc.active != "" {
					want = tc.active
				}
				if started != want {
					t.Fatalf("started %q, want %q", started, want)
				}
				if h.read("credential") != Username+":"+password {
					t.Fatal("password changed in transit")
				}
				if !strings.Contains(h.calls, "usermod --append --groups sudo "+Username) {
					t.Fatal("account cannot administer the box")
				}
				if tc.account == "" && !strings.Contains(h.calls, "useradd --create-home --shell /bin/bash") {
					t.Fatal("account lacks a home or shell")
				}
			} else if len(started) != 0 {
				t.Fatal("started SSH after setup failed")
			}
			if (tc.account != "" || tc.server == "") && strings.Contains(h.calls, "useradd") {
				t.Fatal("created account despite collision or missing SSH")
			}
			if strings.Contains(h.calls, "enable") && !strings.Contains(h.calls, "chpasswd") {
				t.Fatal("enabled SSH before configuring the password")
			}
		})
	}
}

func TestHelperSaysWhenTheSSHServerRefusesThePassword(t *testing.T) {
	for _, tc := range []struct {
		name, server, active, passwords, methods, fail, outcome string
		cardPolicy, reload, checked                             bool
	}{
		{name: "the card made the login keys only", server: "ssh.service", cardPolicy: true, reload: true, checked: true},
		{name: "the card's policy under a running server", server: "ssh.service", active: "ssh.service", cardPolicy: true, reload: true, checked: true},
		{name: "keys only beyond the card's policy", server: "ssh.service", cardPolicy: true, passwords: "no", reload: true, checked: true, outcome: "refused"},
		{name: "keys only for every login", server: "ssh.service", passwords: "no", checked: true, outcome: "refused"},
		{name: "a public key required", server: "sshd.service", methods: "publickey", checked: true, outcome: "refused"},
		{name: "a key and then a password", server: "ssh.service", methods: "publickey,password", checked: true, outcome: "refused"},
		{name: "a key or a password", server: "ssh.service", methods: "publickey password", checked: true},
		{name: "an unreadable policy", server: "ssh.service", fail: "policy", checked: true, outcome: "failed"},
		{name: "Dropbear", server: "dropbear.service", cardPolicy: true, passwords: "no"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newHelper(t)
			if tc.cardPolicy {
				h.putCardPolicy(t)
			}
			h.run(t, defaultPassword, "SSH_TEST_SERVER="+tc.server, "SSH_TEST_ACTIVE="+tc.active,
				"SSH_TEST_PASSWORDS="+tc.passwords, "SSH_TEST_METHODS="+tc.methods, "SSH_TEST_FAIL="+tc.fail)
			if outcome := h.outcome(); outcome != tc.outcome {
				t.Fatalf("helper exited %d, read as %q, want %q: %s", h.status, outcome, tc.outcome, h.output)
			}
			if _, err := os.Lstat(h.cardPolicy()); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("the card's keys-only policy outlived the password")
			}
			if reloaded := strings.Contains(h.calls, "systemctl reload-or-restart "+tc.server+"\n"); reloaded != tc.reload {
				t.Fatalf("reloaded the SSH server = %v, want %v:\n%s", reloaded, tc.reload, h.calls)
			}
			if checked := strings.Contains(h.calls, policyCheck+"\n"); checked != tc.checked {
				t.Fatalf("read the SSH server's policy = %v, want %v:\n%s", checked, tc.checked, h.calls)
			}
			unit := tc.server
			if tc.active != "" {
				unit = tc.active
			}
			if h.read("started") != unit || h.read("credential") != Username+":"+defaultPassword {
				t.Fatal("the login or SSH was not left set up")
			}
		})
	}
}

// realSSHD stands in for sshd with OpenSSH's own, reading a config and host key under the test's root.
const realSSHD = `#!/bin/bash
printf '%s %s\n' "${0##*/}" "$*" >> "$SSH_TEST_DIR/calls"
exec /usr/sbin/sshd -f "$KALINKA_SSH_ROOT/etc/ssh/sshd_config" -h "$KALINKA_SSH_ROOT/etc/ssh/ssh_host_ed25519_key" "$@"
`

func TestHelperReadsOpenSSHsOwnAnswer(t *testing.T) {
	keygen, err := exec.LookPath("ssh-keygen")
	if _, missing := os.Stat("/usr/sbin/sshd"); err != nil || missing != nil {
		t.Skip("needs OpenSSH's sshd and ssh-keygen")
	}
	if os.Geteuid() == 0 {
		t.Skip("as root, sshd -T also wants the host's privilege separation directory")
	}
	for _, tc := range []struct {
		name, policy, outcome string
		cardPolicy            bool
	}{
		{name: "the card made the login keys only", cardPolicy: true},
		{name: "keys only for another login", policy: "Match User pi\n\tPasswordAuthentication no\n"},
		{name: "keys only for every login", policy: "PasswordAuthentication no\n", outcome: "refused"},
		{name: "keys only for kalinka-admin by hand", policy: "Match User kalinka-admin\n\tPasswordAuthentication no\n", outcome: "refused"},
		{name: "a public key required", policy: "AuthenticationMethods publickey\n", outcome: "refused"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newHelper(t)
			put(t, filepath.Join(h.tools, "sshd"), realSSHD, 0700)
			conf := filepath.Join(h.root, "etc/ssh")
			put(t, filepath.Join(conf, "sshd_config"), "Include "+conf+"/sshd_config.d/*.conf\n", 0644)
			if tc.cardPolicy {
				h.putCardPolicy(t)
			}
			if tc.policy != "" {
				put(t, filepath.Join(conf, "sshd_config.d/01-test.conf"), tc.policy, 0644)
			}
			if out, err := exec.Command(keygen, "-q", "-t", "ed25519", "-N", "", "-f", filepath.Join(conf, "ssh_host_ed25519_key")).CombinedOutput(); err != nil {
				t.Fatalf("ssh-keygen: %v: %s", err, out)
			}
			h.run(t, defaultPassword, "SSH_TEST_SERVER=ssh.service")
			if outcome := h.outcome(); outcome != tc.outcome || !strings.Contains(h.calls, policyCheck+"\n") {
				t.Fatalf("helper exited %d, read as %q, want %q: %s\n%s", h.status, outcome, tc.outcome, h.output, h.calls)
			}
		})
	}
}
