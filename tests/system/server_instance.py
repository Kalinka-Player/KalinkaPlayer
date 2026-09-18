"""A Kalinka server of its own, in a throwaway fakeroot.

Set up with the same ``make dev-setup`` and run through the same
``scripts/dev_run.sh`` a developer uses, so the test exercises the launcher
too — including the in-app restart it implements by relaunching the server.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Optional

import httpx

from waiting import wait_until

logger = logging.getLogger("system-test")

_START_TIMEOUT_S = 240.0
_STOP_TIMEOUT_S = 60.0
_HTTP_TIMEOUT_S = 60.0


class KalinkaInstance:
    def __init__(
        self, repo_root: Path, prefix: Path, port: int, venv: Path, pid_file: Path
    ) -> None:
        self.repo_root = repo_root
        self.prefix = prefix
        self.port = port
        self.venv = venv
        self.base_url = f"http://127.0.0.1:{port}"
        self.config_path = prefix / "etc/kalinka/kalinka_conf.cfg"
        self.state_dir = prefix / "var/lib/kalinka"
        self.db_path = self.state_dir / "localfiles.db"
        self.models_dir = self.state_dir / "models"
        self.music_dir = prefix / "srv/kalinka/music"
        self.log_path = prefix / "var/log/kalinka/server.log"
        self.launcher_log = prefix / "dev_run.out"
        self._pid_file = pid_file
        self._proc: Optional[subprocess.Popen] = None
        self._launcher_out = None
        self._http = httpx.Client(base_url=self.base_url, timeout=_HTTP_TIMEOUT_S)

    @property
    def server_id(self) -> str:
        return (self.state_dir / "server_id").read_text().strip()

    def install(self, overrides: dict[str, Any], models_cache: Path) -> None:
        """Seed the fakeroot: config first (dev-setup keeps an existing one),
        then the editable installs, then the model cache in place of the
        model directory so downloads survive between runs."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {
            "base_config.server.interface": "lo",
            "base_config.server.port": self.port,
            "base_config.server.service_name": "Kalinka system test",
            "base_config.server.oobe_complete": True,
            **overrides,
        }
        self.config_path.write_text(json.dumps(config, indent=2))
        result = subprocess.run(
            ["make", "dev-setup", f"KALINKA_PREFIX={self.prefix}", f"VENV={self.venv}"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=900,
        )
        if result.returncode != 0:
            raise RuntimeError(f"make dev-setup failed:\n{result.stdout}{result.stderr}")
        models_cache.mkdir(parents=True, exist_ok=True)
        self.models_dir.parent.mkdir(parents=True, exist_ok=True)
        self.models_dir.symlink_to(models_cache, target_is_directory=True)

    def start(self) -> None:
        self._reap_stale()
        self._launcher_out = open(self.launcher_log, "ab")
        self._proc = subprocess.Popen(
            [str(self.repo_root / "scripts/dev_run.sh")],
            cwd=self.repo_root,
            env={**os.environ, "KALINKA_PREFIX": str(self.prefix), "VENV": str(self.venv)},
            stdout=self._launcher_out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._pid_file.write_text(str(self._proc.pid))
        self._wait_reachable()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        self._launcher_out.close()
        self._pid_file.unlink(missing_ok=True)

    def _reap_stale(self) -> None:
        """A run that never reached stop() — pytest killed outright, a crash
        that skipped fixture teardown — leaves its process group behind.
        Unlike its fakeroot, this file survives that run, so the next one can
        find and clear it before starting its own."""
        try:
            pgid = int(self._pid_file.read_text())
        except (OSError, ValueError):
            return
        try:
            cmdline = Path(f"/proc/{pgid}/cmdline").read_bytes()
        except OSError:
            cmdline = b""
        if b"dev_run.sh" in cmdline:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            else:
                logger.warning("killed a stale kalinka test server (pgid %d)", pgid)
        self._pid_file.unlink(missing_ok=True)

    def restart(self) -> None:
        """The in-app restart: the server asks for one, the launcher relaunches it."""
        seen = self.launcher_log.stat().st_size
        self.put("/server/restart", json={})
        wait_until(
            lambda: b"relaunching" in self.launcher_log.read_bytes()[seen:],
            timeout=_STOP_TIMEOUT_S,
            what="the launcher to relaunch the server",
        )
        self._wait_reachable()

    def _wait_reachable(self) -> None:
        def reachable() -> bool:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"the server exited with {self._proc.returncode}:\n{self.log_tail()}"
                )
            return self.alive()

        wait_until(reachable, timeout=_START_TIMEOUT_S, what="the server to answer")

    def alive(self) -> bool:
        try:
            return self._http.get("/server/version").status_code == 200
        except httpx.HTTPError:
            return False

    def get(self, path: str, **params: Any) -> Any:
        response = self._http.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def put(self, path: str, **kwargs: Any) -> Any:
        response = self._http.put(path, **kwargs)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, **kwargs: Any) -> Any:
        response = self._http.post(path, **kwargs)
        response.raise_for_status()
        return response.json()

    def raw(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self._http.request(method, url, **kwargs)

    def indexer_status(self) -> dict[str, Any]:
        return self.get("/indexer/status").get("localfiles", {})

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def tracks(self) -> dict[str, sqlite3.Row]:
        """Every indexed track, keyed by the location the library recorded."""
        return {
            row["file_path"]: row
            for row in self.rows(
                "SELECT id, title, file_path, album_id, artist_id, duration, enriched "
                "FROM tracks"
            )
        }

    def log_tail(self, lines: int = 80) -> str:
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError:
            return "(no server log)"
        return "\n".join(text.splitlines()[-lines:])
