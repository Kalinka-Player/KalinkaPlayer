"""make install-test, and what tests/install is built from, without a container.

The install cases need a throwaway Debian system, so only `make install-test`
runs them. These check the launcher, the stand-ins each container gets and the
helpers the cases rely on.
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
INSTALL_TESTS = REPO / "tests" / "install"
MAKE = shutil.which("make")
BASE = "example.invalid/debian:trixie"


def _load_cases():
    spec = importlib.util.spec_from_file_location(
        "install_cases", INSTALL_TESTS / "cases.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cases = _load_cases()


def _executable(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return path


def _lines(path):
    return path.read_text().splitlines() if path.exists() else []


class TemporaryDirectoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()


@unittest.skipIf(MAKE is None, "needs make")
class MakeInstallTestTests(TemporaryDirectoryTest):
    def setUp(self):
        super().setUp()
        self.logs = self.root / "logs"
        self.calls = self.root / "calls"

    def make(self, path):
        return subprocess.run(
            [MAKE, "-s", "-C", str(REPO), "install-test", f"INSTALL_TEST_BASE={BASE}"],
            env=dict(
                os.environ,
                PATH=path,
                INSTALL_TEST_LOGS=str(self.logs),
                CALLS=str(self.calls),
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def fake_engine(self, failing=""):
        _executable(
            self.bin / "podman",
            f"""#!{sys.executable}
import json, os, sys
with open(os.environ["CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "run":
    print("container output for", sys.argv[-1])
    sys.exit(1 if sys.argv[-1] == {failing!r} else 0)
""",
        )

    def engine_calls(self):
        return [json.loads(line) for line in _lines(self.calls)]

    def test_without_podman_or_docker_says_so_and_runs_nothing(self):
        for name in ("bash", "python3", "curl"):
            _executable(self.bin / name, f'#!/bin/sh\necho "{name} $*" >> "$CALLS"\n')
        result = self.make(str(self.bin))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No podman or docker", result.stdout)
        self.assertIn("nothing ran", result.stdout)
        self.assertEqual(_lines(self.calls), [])
        self.assertFalse(self.logs.exists())

    def test_runs_each_system_in_a_throwaway_container_of_the_base(self):
        self.fake_engine()
        result = self.make(f"{self.bin}:{os.environ['PATH']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.engine_calls()
        self.assertEqual(calls[0], ["image", "inspect", BASE])
        runs = calls[1:]
        self.assertEqual([run[-1] for run in runs], ["debian", "dietpi"])
        for run in runs:
            self.assertEqual(run[:3], ["run", "--pull=never", "--rm"])
            self.assertIn(f"{REPO}/scripts:/kalinka/scripts:ro,z", run)
            self.assertIn(f"{REPO}/tests/install:/kalinka/tests/install:ro,z", run)
            self.assertIn("GITHUB_TOKEN", run)
            self.assertEqual(
                run[-4:-1], [BASE, "bash", "/kalinka/tests/install/system.sh"]
            )
        for system in ("debian", "dietpi"):
            self.assertIn(
                f"container output for {system}",
                (self.logs / f"{system}.log").read_text(),
            )
        self.assertIn("install-test passed", result.stdout)

    def test_a_failed_system_fails_the_target_and_names_its_log(self):
        self.fake_engine(failing="debian")
        result = self.make(f"{self.bin}:{os.environ['PATH']}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"debian: FAILED, its log is {self.logs}/debian.log", result.stdout
        )
        self.assertIn("dietpi: passed", result.stdout)
        self.assertIn("install-test failed on: debian", result.stdout)


class GuideTests(unittest.TestCase):
    def test_each_system_installs_what_the_guide_asks_for_first(self):
        system = (INSTALL_TESTS / "system.sh").read_text()
        for doc in ("README.md", "docs/installation.md"):
            with self.subTest(doc=doc):
                asked = re.search(
                    r"^\s*sudo apt install -y (.+)$", (REPO / doc).read_text(), re.M
                )
                self.assertIsNotNone(asked)
                self.assertIn(f"\napt-get install -y -q {asked[1]}\n", system)


class StandInTests(TemporaryDirectoryTest):
    def setUp(self):
        super().setUp()
        self.apt_calls = self.root / "apt-calls"
        real = _executable(
            self.root / "real-apt-get",
            f'#!/bin/sh\necho "$*" >> "{self.apt_calls}"\n',
        )
        self.apt_get = _executable(
            self.bin / "apt-get",
            (INSTALL_TESTS / "apt-get-failing-once")
            .read_text()
            .replace("/usr/bin/apt-get", str(real)),
        )

    def apt(self, *args):
        return subprocess.run(
            [str(self.apt_get), *args], capture_output=True, text=True, timeout=15
        )

    def test_apt_get_fails_the_first_install_of_downloaded_packages_only(self):
        install = (
            "-o",
            "DPkg::Lock::Timeout=300",
            "install",
            "-y",
            "--no-install-recommends",
            "/tmp/dl/kalinka-server_5.18.0_all.deb",
            "/tmp/dl/kalinka-plugin-sdk_3.21.0_all.deb",
        )
        self.assertEqual(self.apt("update").returncode, 0)
        refused = self.apt(*install)
        self.assertEqual(refused.returncode, 100)
        self.assertIn("on purpose", refused.stderr)
        self.assertEqual(_lines(self.apt_calls), ["update"])
        self.assertEqual(self.apt(*install).returncode, 0)
        self.assertEqual(_lines(self.apt_calls), ["update", " ".join(install)])

    def test_apt_get_leaves_repository_installs_and_repairs_to_apt(self):
        for args in (
            ("-s", "install", "--no-install-recommends", "--no-upgrade", "nfs-common"),
            ("install", "-y", "--no-install-recommends", "libchromaprint-tools"),
            ("install", "-y", "--no-install-recommends", "-f"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.apt(*args).returncode, 0)
        self.assertEqual(len(_lines(self.apt_calls)), 3)
        self.assertFalse(Path(f"{self.apt_get}.fired").exists())

    def test_systemctl_records_what_it_is_asked_and_succeeds(self):
        systemctl = _executable(
            self.bin / "systemctl",
            (INSTALL_TESTS / "systemctl-recorder").read_text(),
        )
        asked = [
            ["daemon-reload"],
            ["restart", "kalinka.service"],
            ["--no-pager", "--full", "status", "kalinka.service"],
        ]
        for args in asked:
            result = subprocess.run([str(systemctl), *args], timeout=15)
            self.assertEqual(result.returncode, 0)
        self.assertEqual(
            _lines(Path(f"{systemctl}.calls")), [" ".join(args) for args in asked]
        )


class ReleaseTests(unittest.TestCase):
    @staticmethod
    def release(tag, *names, **flags):
        assets = [
            {"name": name, "browser_download_url": f"https://example.test/{name}"}
            for name in names
        ]
        return {"tag_name": tag, "assets": assets, **flags}

    def test_picks_published_bundle_releases_newest_first(self):
        feed = [
            self.release("kalinka-v6.0.0", draft=True),
            self.release("kalinka-v5.19.0", prerelease=True),
            self.release("kalinka-renderer-v0.30.0"),
            self.release("kalinka-v5.18.0"),
            self.release("kalinka-supervisor-v0.4.0"),
            self.release("jamendo-ai-v2"),
            self.release("kalinka-v5.17.0"),
        ]
        self.assertEqual(
            [release["tag_name"] for release in cases.kalinka_releases(feed)],
            ["kalinka-v5.18.0", "kalinka-v5.17.0"],
        )

    def test_bundle_is_the_arch_independent_debs_by_version(self):
        release = self.release(
            "kalinka-v5.18.0",
            "kalinka-server_5.18.0_all.deb",
            "kalinka-plugin-sdk_3.21.0_all.deb",
            "kalinka-plugin-bluetooth_5.18.0_all.deb",
            "kalinka-server-5.18.0-1.noarch.rpm",
            "kalinka-renderer_0.30.0.debian-13.amd64.deb",
            "SHA256SUMS",
        )
        self.assertEqual(
            cases.bundle(release),
            {
                "kalinka-server": "5.18.0",
                "kalinka-plugin-sdk": "3.21.0",
                "kalinka-plugin-bluetooth": "5.18.0",
            },
        )


class ServiceTests(unittest.TestCase):
    def test_starts_the_server_as_its_shipped_unit_says(self):
        unit = REPO / "packages" / "kalinka-server" / "scripts" / "kalinka.service"
        service = cases.Service(unit.read_text())
        self.assertEqual((service.user, service.group), ("kalusr", "kalusr"))
        self.assertEqual(service.working_directory, "/opt/kalinka")
        self.assertEqual(
            service.environment, {"NUMBA_CACHE_DIR": "/var/cache/kalinka/numba"}
        )
        self.assertEqual(
            service.directories,
            [
                (Path("/var/lib/kalinka"), 0o750),
                (Path("/var/log/kalinka"), 0o750),
                (Path("/run/kalinka"), 0o755),
                (Path("/var/cache/kalinka"), 0o755),
            ],
        )
        self.assertEqual(service.pre, [(True, ["/opt/kalinka/bootstrap.sh"])])
        self.assertEqual(
            service.start,
            [
                "/opt/kalinka/venv/bin/kalinka-server",
                "--config",
                "/etc/kalinka/kalinka_conf.cfg",
                "--state",
                "/var/lib/kalinka/kalinka_state.json",
            ],
        )

    def test_reads_only_the_service_section_and_follows_its_syntax(self):
        service = cases.Service(
            "[Unit]\nUser=nobody\nExecStart=/bin/false\n\n"
            "[Service]\n# User=root\n; ExecStart=/bin/true\nUser=app\n"
            'Environment=A=1 "B=two words"\nEnvironment=C=3\n'
            "RuntimeDirectory=app app/sockets\n"
            "ExecStartPre=/opt/app/check \\\n  --quick\n"
            "ExecStartPre=+/opt/app/prepare\n"
            "ExecStart=/opt/app/run \\\n  --flag\n\n"
            "[Install]\nUser=other\n"
        )
        self.assertEqual((service.user, service.group), ("app", "app"))
        self.assertEqual(service.working_directory, "/")
        self.assertEqual(service.environment, {"A": "1", "B": "two words", "C": "3"})
        self.assertEqual(
            service.directories,
            [(Path("/run/app"), 0o755), (Path("/run/app/sockets"), 0o755)],
        )
        self.assertEqual(
            service.pre,
            [(False, ["/opt/app/check", "--quick"]), (True, ["/opt/app/prepare"])],
        )
        self.assertEqual(service.start, ["/opt/app/run", "--flag"])


def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class AnswerTests(unittest.TestCase):
    def start(self, code):
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            text=True,
            process_group=0,
        )
        self.addCleanup(process.stdout.close)
        self.addCleanup(cases.stop, process, 5)
        return process

    def test_returns_what_the_server_answers_once_it_listens(self):
        process = self.start(
            "import http.server, json, socket, time\n"
            "finder = socket.socket(); finder.bind(('127.0.0.1', 0))\n"
            "port = finder.getsockname()[1]; finder.close()\n"
            "print(port, flush=True); time.sleep(0.5)\n"
            "class Version(http.server.BaseHTTPRequestHandler):\n"
            "    def log_message(self, *args):\n"
            "        pass\n"
            "    def do_GET(self):\n"
            "        body = json.dumps({'server_version': '5.18.0'}).encode()\n"
            "        self.send_response(200); self.end_headers()\n"
            "        self.wfile.write(body)\n"
            "http.server.HTTPServer(('127.0.0.1', port), Version).serve_forever()\n"
        )
        port = int(process.stdout.readline())
        answer = cases.wait_for_answer(
            process, f"http://127.0.0.1:{port}/server/version", timeout=20, poll=0.05
        )
        self.assertEqual(answer, {"server_version": "5.18.0"})

    def test_fails_as_soon_as_the_server_exits(self):
        process = self.start("raise SystemExit(3)")
        started = time.monotonic()
        with self.assertRaisesRegex(cases.CheckFailed, "exited with status 3"):
            cases.wait_for_answer(
                process, f"http://127.0.0.1:{_closed_port()}/", timeout=60, poll=0.05
            )
        self.assertLess(time.monotonic() - started, 30)

    def test_fails_when_no_answer_comes_in_time(self):
        process = self.start("import time; time.sleep(60)")
        with self.assertRaisesRegex(cases.CheckFailed, "did not answer within"):
            cases.wait_for_answer(
                process, f"http://127.0.0.1:{_closed_port()}/", timeout=0.3, poll=0.05
            )

    def test_stop_kills_a_server_that_ignores_sigterm(self):
        process = self.start(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True); time.sleep(60)\n"
        )
        self.assertEqual(process.stdout.readline().strip(), "ready")
        cases.stop(process, grace=0.5)
        self.assertEqual(process.returncode, -signal.SIGKILL)

    def test_stop_ends_what_outlives_the_server_in_its_group(self):
        child = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True); time.sleep(60)\n"
        )
        process = self.start(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            "time.sleep(60)\n"
        )
        self.assertEqual(process.stdout.readline().strip(), "ready")
        cases.stop(process, grace=5)
        self.assertEqual(process.returncode, -signal.SIGTERM)
        closed, _, _ = select.select([process.stdout], [], [], 10)
        self.assertTrue(closed, "the child still holds the server's output")
        self.assertEqual(process.stdout.read(), "")


class StepTests(unittest.TestCase):
    def test_a_missing_file_fails_the_step_by_name(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(
                cases.CheckFailed, r"^start it: .*No such file"
            ):
                with cases.step("start it"):
                    Path("/nonexistent/kalinka.service").read_text()


class ApiQuotaTests(unittest.TestCase):
    def test_no_wait_while_the_quota_covers_the_run(self):
        core = {"remaining": cases.API_CALLS, "reset": 2000}
        self.assertEqual(cases.quota_wait(core, cases.API_CALLS, now=1000), 0)

    def test_a_short_quota_waits_for_its_reset(self):
        core = {"remaining": cases.API_CALLS - 1, "reset": 2000}
        self.assertEqual(
            cases.quota_wait(core, cases.API_CALLS, now=1000),
            1000 + cases.RESET_SLACK,
        )

    def test_a_reset_already_past_waits_only_the_slack(self):
        core = {"remaining": 0, "reset": 900}
        self.assertEqual(cases.quota_wait(core, 1, now=1000), cases.RESET_SLACK)

    def quotas(self, *remaining):
        answers = iter(
            {"resources": {"core": {"limit": 60, "remaining": left, "reset": 0}}}
            for left in remaining
        )
        urls = []

        def github(url):
            urls.append(url)
            return next(answers)

        return urls, mock.patch.object(cases, "github", github)

    def test_goes_ahead_once_the_quota_is_back(self):
        urls, github = self.quotas(0, 60)
        with github, mock.patch.object(cases.time, "sleep") as sleep:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cases.wait_for_api_quota(cases.API_CALLS)
        self.assertEqual(urls, [cases.RATE_LIMIT_URL] * 2)
        sleep.assert_called_once()
        self.assertIn("0 of 60 calls left", output.getvalue())
        self.assertIn("GITHUB_TOKEN", output.getvalue())

    def test_gives_up_on_a_quota_that_never_comes_back(self):
        _, github = self.quotas(0)
        with github, mock.patch.object(cases, "QUOTA_TIMEOUT", 1):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(cases.CheckFailed, "stayed short"):
                    cases.wait_for_api_quota(cases.API_CALLS)


if __name__ == "__main__":
    unittest.main()
