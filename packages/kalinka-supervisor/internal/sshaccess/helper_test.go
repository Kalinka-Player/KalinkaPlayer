package sshaccess

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

const fakeAccountTools = `#!/bin/bash
printf '%s %s\n' "${0##*/}" "$*" >> "$SSH_TEST_DIR/calls"
case ${0##*/} in
  systemctl)
    case $1 in
      show) if [[ $4 == "$SSH_TEST_SERVER" || $4 == "$SSH_TEST_ACTIVE" ]]; then echo loaded; else echo not-found; fi ;;
      is-active) [[ $3 == "$SSH_TEST_ACTIVE" || -f $SSH_TEST_DIR/started ]] ;;
      enable) [[ $SSH_TEST_FAIL != start ]] || exit 1; printf '%s' "$3" > "$SSH_TEST_DIR/started" ;;
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
esac
`

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
			dir := t.TempDir()
			for _, name := range []string{"systemctl", "getent", "useradd", "usermod", "chpasswd"} {
				if err := os.WriteFile(filepath.Join(dir, name), []byte(fakeAccountTools), 0700); err != nil {
					t.Fatal(err)
				}
			}
			password := tc.password
			if password == "" {
				password = "test: with ` $() \\"
			}
			cmd := exec.Command("/bin/bash", "../../ssh-setup.sh")
			cmd.Env = []string{"PATH=" + dir, "SSH_TEST_DIR=" + dir, "SSH_TEST_SERVER=" + tc.server, "SSH_TEST_ACTIVE=" + tc.active, "SSH_TEST_ACCOUNT=" + tc.account, "SSH_TEST_FAIL=" + tc.fail}
			cmd.Stdin = strings.NewReader(password + "\n")
			output, err := cmd.CombinedOutput()
			if (err == nil) != tc.ok {
				t.Fatalf("helper success = %v, want %v: %s", err == nil, tc.ok, output)
			}
			calls, _ := os.ReadFile(filepath.Join(dir, "calls"))
			if strings.Contains(string(calls), password) || strings.Contains(string(output), password) {
				t.Fatal("password leaked")
			}
			started, _ := os.ReadFile(filepath.Join(dir, "started"))
			if tc.ok {
				want := tc.server
				if tc.active != "" {
					want = tc.active
				}
				if string(started) != want {
					t.Fatalf("started %q, want %q", started, want)
				}
				credential, _ := os.ReadFile(filepath.Join(dir, "credential"))
				if string(credential) != Username+":"+password {
					t.Fatal("password changed in transit")
				}
				if !strings.Contains(string(calls), "usermod --append --groups sudo "+Username) {
					t.Fatal("account cannot administer the box")
				}
				if tc.account == "" && !strings.Contains(string(calls), "useradd --create-home --shell /bin/bash") {
					t.Fatal("account lacks a home or shell")
				}
			} else if len(started) != 0 {
				t.Fatal("started SSH after setup failed")
			}
			if (tc.account != "" || tc.server == "") && strings.Contains(string(calls), "useradd") {
				t.Fatal("created account despite collision or missing SSH")
			}
			if strings.Contains(string(calls), "enable") && !strings.Contains(string(calls), "chpasswd") {
				t.Fatal("enabled SSH before configuring the password")
			}
		})
	}
}
