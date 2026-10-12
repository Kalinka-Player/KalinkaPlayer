package control

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// fakePackageTools stand in for apt, dpkg and systemctl: REMOVAL_TEST_INSTALLED
// lists installed packages, REMOVAL_TEST_PLAN is what apt would remove, and
// REMOVAL_TEST_FAIL makes the removal itself fail.
const fakePackageTools = `#!/bin/bash
printf '%s %s\n' "${0##*/}" "$*" >> "$REMOVAL_TEST_DIR/calls"
case ${0##*/} in
  dpkg-query)
    pkg=${@: -1}
    if [[ -f $REMOVAL_TEST_DIR/removed ]]; then echo config-files; exit 0; fi
    for p in $REMOVAL_TEST_INSTALLED; do [[ $p == "$pkg" ]] && { echo installed; exit 0; }; done
    exit 1 ;;
  apt-get)
    if [[ $1 == -s ]]; then printf '%s\n' "$REMOVAL_TEST_PLAN"; exit 0; fi
    [[ $REMOVAL_TEST_FAIL != apt ]] || exit 100
    touch "$REMOVAL_TEST_DIR/removed" ;;
  systemctl) ;;
esac
`

func TestRemovalScriptTakesOnlyTheRequestedPlugin(t *testing.T) {
	for _, tc := range []struct {
		name, request, installed, plan, fail string
		ok, removed, restarted               bool
	}{
		{name: "removes", request: "kalinka-plugin-qobuz", installed: "kalinka-plugin-qobuz", plan: "Remv kalinka-plugin-qobuz [5.0.1]", ok: true, removed: true, restarted: true},
		{name: "dots and plus", request: "kalinka-plugin-my.thing+", installed: "kalinka-plugin-my.thing+", plan: "Remv kalinka-plugin-my.thing+ [1]", ok: true, removed: true, restarted: true},
		{name: "not installed", request: "kalinka-plugin-qobuz", plan: "Remv kalinka-plugin-qobuz [5.0.1]"},
		{name: "the SDK", request: "kalinka-plugin-sdk", installed: "kalinka-plugin-sdk", plan: "Remv kalinka-plugin-sdk [3.5.0]"},
		{name: "the server", request: "kalinka-server", installed: "kalinka-server", plan: "Remv kalinka-server [5.8.0]"},
		{name: "shell text", request: "kalinka-plugin-x; reboot", installed: "kalinka-plugin-x; reboot", plan: "Remv kalinka-plugin-x [1]"},
		{name: "takes another package along", request: "kalinka-plugin-qobuz", installed: "kalinka-plugin-qobuz", plan: "Remv kalinka-plugin-qobuz [5.0.1]\nRemv kalinka-server [5.8.0]"},
		{name: "apt fails", request: "kalinka-plugin-qobuz", installed: "kalinka-plugin-qobuz", plan: "Remv kalinka-plugin-qobuz [5.0.1]", fail: "apt", restarted: true},
		{name: "no request"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			for _, name := range []string{"apt-get", "dpkg-query", "systemctl"} {
				if err := os.WriteFile(filepath.Join(dir, name), []byte(fakePackageTools), 0700); err != nil {
					t.Fatal(err)
				}
			}
			request := filepath.Join(dir, "request")
			if tc.request != "" {
				if err := os.WriteFile(request, []byte(tc.request+"\n"), 0600); err != nil {
					t.Fatal(err)
				}
			}
			cmd := exec.Command("/bin/bash", "../../remove-plugin.sh", request)
			cmd.Env = []string{"PATH=" + dir + ":/usr/bin:/bin", "REMOVAL_TEST_DIR=" + dir, "REMOVAL_TEST_INSTALLED=" + tc.installed, "REMOVAL_TEST_PLAN=" + tc.plan, "REMOVAL_TEST_FAIL=" + tc.fail}
			output, err := cmd.CombinedOutput()
			if (err == nil) != tc.ok {
				t.Fatalf("success = %v, want %v: %s", err == nil, tc.ok, output)
			}
			calls, _ := os.ReadFile(filepath.Join(dir, "calls"))
			_, removed := os.Stat(filepath.Join(dir, "removed"))
			if (removed == nil) != tc.removed {
				t.Fatalf("removed = %v, want %v: %s", removed == nil, tc.removed, calls)
			}
			stopped := strings.Contains(string(calls), "systemctl stop kalinka.service")
			restarted := strings.Contains(string(calls), "systemctl --no-block restart kalinka.service")
			if stopped != tc.restarted || restarted != tc.restarted {
				t.Fatalf("stopped %v, restarted %v, want %v: %s", stopped, restarted, tc.restarted, calls)
			}
			if strings.Contains(string(calls), "apt-get -o DPkg::Lock::Timeout=300 remove") && !tc.removed && tc.fail == "" {
				t.Fatalf("apt removal ran despite a failed check: %s", calls)
			}
		})
	}
}

func TestRemovablePackage(t *testing.T) {
	for _, name := range []string{"kalinka-plugin-qobuz", "kalinka-plugin-spotify", "kalinka-plugin-my.thing+2"} {
		if !RemovablePackage(name) {
			t.Errorf("%q refused", name)
		}
	}
	for _, name := range []string{"", "kalinka-plugin-sdk", "kalinka-server", "kalinka-renderer", "kalinka-plugin-", "kalinka-plugin-Qobuz", "kalinka-plugin-x y", "kalinka-plugin-x;reboot", "kalinka-plugin-" + strings.Repeat("a", 100), "../kalinka-plugin-x", "kalinka-plugin-x\n"} {
		if RemovablePackage(name) {
			t.Errorf("%q accepted", name)
		}
	}
}
