"""A Samba server in a rootless podman container.

Nothing on the host needs root: the container publishes its port on the
loopback interface and reads the share directory through a bind mount, so
the test writes files into that directory and the server sees them at once.
"""

from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path

_CONTEXT = Path(__file__).with_name("smb")
_IMAGE = "localhost/kalinka-system-test-smb"
_START_TIMEOUT_S = 60.0


class SambaShare:
    """One directory exported twice: ``music`` for the named account and
    ``public`` for guests."""

    USERNAME = "kalinka"
    PASSWORD = "kalinka-pass"

    def __init__(self, share_dir: Path, port: int) -> None:
        self.share_dir = share_dir
        self.port = port
        self._name = f"kalinka-system-test-smb-{port}"

    @property
    def url(self) -> str:
        return f"smb://127.0.0.1:{self.port}/music"

    @property
    def guest_url(self) -> str:
        return f"smb://127.0.0.1:{self.port}/public"

    def start(self) -> None:
        self.share_dir.mkdir(parents=True, exist_ok=True)
        _podman("build", "-q", "-t", _IMAGE, str(_CONTEXT))
        _podman("rm", "-f", self._name, check=False)
        _podman(
            "run", "-d", "--rm", "--name", self._name,
            "-p", f"127.0.0.1:{self.port}:445",
            "-v", f"{self.share_dir}:/music:Z",
            _IMAGE,
        )
        try:
            deadline = time.monotonic() + _START_TIMEOUT_S
            while not _accepting(self.port):
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"Samba did not start listening on {self.port}:\n{self.logs()}"
                    )
                time.sleep(0.5)
        except BaseException:
            # Nothing else will: the fixture only takes ownership once start()
            # returns, and the name carries a port no later run will guess.
            self.stop()
            raise

    def stop(self) -> None:
        _podman("rm", "-f", self._name, check=False)

    def logs(self) -> str:
        return _podman("logs", self._name, check=False)


def _podman(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["podman", *args], capture_output=True, text=True, timeout=600
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"podman {' '.join(args)} failed ({result.returncode}):\n"
            f"{result.stdout}{result.stderr}"
        )
    return result.stdout + result.stderr


def _accepting(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False
