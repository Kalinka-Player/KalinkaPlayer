"""Fixtures for the full-stack system test.

Opt-in: everything here is skipped unless ``KALINKA_SYSTEM_TEST=1``. A run
needs podman, network access, a few minutes of CPU, and on first use the
CLAP models and test recordings, which land in the cache directory
(``KALINKA_SYSTEM_TEST_CACHE``, default ``~/.cache/kalinka-system-test``) so
later runs skip the downloads.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from recordings import ART_OF_FUGUE, GOLDBERG, PlantedTrack, plant
from samba import SambaShare
from server_instance import KalinkaInstance
from waiting import wait_until

logger = logging.getLogger("system-test")
logging.getLogger("httpx").setLevel(logging.WARNING)

REPO_ROOT = Path(__file__).resolve().parents[2]

INDEX_TIMEOUT_S = 600.0
ENRICH_TIMEOUT_S = 900.0
EMBED_TIMEOUT_S = 2400.0

_INSTANCE = pytest.StashKey[KalinkaInstance]()


def _opted_in() -> bool:
    return os.environ.get("KALINKA_SYSTEM_TEST") == "1"


def pytest_configure(config: pytest.Config) -> None:
    if _opted_in():
        # A bare SIGTERM has no default cleanup; make it unwind fixtures the
        # same way Ctrl-C does, so instance.stop()/share.stop() still run.
        signal.signal(signal.SIGTERM, signal.default_int_handler)


def pytest_collection_modifyitems(config, items):
    if _opted_in():
        return
    skip = pytest.mark.skip(reason="system test; set KALINKA_SYSTEM_TEST=1 to run it")
    for item in items:
        item.add_marker(skip)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    instance = item.config.stash.get(_INSTANCE, None)
    if report.failed and instance is not None:
        report.sections.append(("kalinka server log (tail)", instance.log_tail()))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def cache_dir() -> Path:
    path = Path(
        os.environ.get("KALINKA_SYSTEM_TEST_CACHE", "~/.cache/kalinka-system-test")
    ).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def workspace(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("kalinka")


@pytest.fixture(scope="session")
def samba(workspace: Path) -> SambaShare:
    if shutil.which("podman") is None:
        pytest.fail("the system test needs podman to run its Samba server")
    share = SambaShare(workspace / "share", _free_port())
    share.start()
    yield share
    share.stop()


@dataclass(frozen=True)
class PlantedLibrary:
    """What was planted under each configured music folder, keyed the way the
    library spells that folder."""

    local_root: str
    smb_root: str
    local: list[PlantedTrack]
    smb: list[PlantedTrack]

    @property
    def all(self) -> list[PlantedTrack]:
        return [*self.local, *self.smb]

    @property
    def per_root(self) -> dict[str, list[PlantedTrack]]:
        return {self.local_root: self.local, self.smb_root: self.smb}


@pytest.fixture(scope="session")
def kalinka(workspace: Path, cache_dir: Path, samba: SambaShare, request) -> KalinkaInstance:
    instance = KalinkaInstance(
        REPO_ROOT, workspace / "prefix", _free_port(), Path(sys.prefix),
        cache_dir / "server.pid",
    )
    instance.install(
        {
            "input_modules.localfiles.music_folders": [str(instance.music_dir), samba.url],
            "input_modules.localfiles.scan_interval_minutes": 60,
            "input_modules.localfiles.smb.username": samba.USERNAME,
            "input_modules.localfiles.smb.password": samba.PASSWORD,
            "input_modules.localfiles.ai_search.enabled": True,
        },
        cache_dir / "models",
    )
    request.config.stash[_INSTANCE] = instance
    yield instance
    instance.stop()
    logger.info("server log kept at %s", instance.log_path)


@pytest.fixture(scope="session")
def planted(kalinka: KalinkaInstance, samba: SambaShare, cache_dir: Path) -> PlantedLibrary:
    """Both albums in place before the server first starts, so its initial
    scan is what indexes them."""
    local_root = str(kalinka.music_dir.resolve())
    library = PlantedLibrary(
        local_root=local_root,
        smb_root=samba.url,
        local=plant(GOLDBERG, cache_dir, kalinka.music_dir, local_root),
        smb=plant(ART_OF_FUGUE, cache_dir, samba.share_dir, samba.url),
    )
    kalinka.start()
    return library


def _stage_settled(stage) -> bool:
    return stage is not None and stage["pending"] == 0 and stage["in_progress"] == 0


def _describe(stage) -> str:
    if stage is None:
        return "not reported"
    return (
        f"{stage['done']} done, {stage['pending']} pending, "
        f"{stage['failed']} failed of {stage['total']}"
    )


@pytest.fixture(scope="session")
def library(kalinka: KalinkaInstance, planted: PlantedLibrary) -> dict:
    """The library once indexing, enrichment and AI embedding have all run to
    completion. Returns the indexed tracks keyed by library path."""
    expected = {track.library_path for track in planted.all}
    wait_until(
        lambda: expected <= set(kalinka.tracks()),
        timeout=INDEX_TIMEOUT_S,
        what="every planted track to be indexed",
        progress=lambda: f"{len(expected & set(kalinka.tracks()))}/{len(expected)} indexed",
    )
    wait_until(
        lambda: "indexing" not in kalinka.indexer_status(),
        timeout=INDEX_TIMEOUT_S,
        what="the scan to finish",
    )
    wait_until(
        lambda: _stage_settled(kalinka.indexer_status().get("enrichment")),
        timeout=ENRICH_TIMEOUT_S,
        what="enrichment to settle",
        interval=5.0,
        progress=lambda: _describe(kalinka.indexer_status().get("enrichment")),
    )
    wait_until(
        lambda: all(
            _stage_settled(kalinka.indexer_status().get(stage))
            for stage in ("clap_audio", "clap_text")
        ),
        timeout=EMBED_TIMEOUT_S,
        what="AI embedding to settle",
        interval=5.0,
        progress=lambda: "audio {}; text {}".format(
            _describe(kalinka.indexer_status().get("clap_audio")),
            _describe(kalinka.indexer_status().get("clap_text")),
        ),
    )
    return kalinka.tracks()
