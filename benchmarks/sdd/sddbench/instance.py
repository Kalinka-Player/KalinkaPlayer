"""The server under test: the shipped one, in a fakeroot of its own.

Launch, configuration and the indexing-completion signal are the system
test's (``tests/system``), reused rather than reimplemented — the point of
the benchmark is that nothing about the pipeline is special-cased for it.
What is added here is the benchmark's own configuration and the environment
that arms the timing wrappers.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from .clock import Clock
from .paths import REPO_ROOT, Layout

sys.path.insert(0, str(REPO_ROOT / "tests" / "system"))

from server_instance import KalinkaInstance  # noqa: E402
from waiting import wait_until  # noqa: E402

TIMING_DIR = Path(__file__).resolve().parent / "timing"

#: Every module the venv has a plugin for, off except the one under test: an
#: enabled Jamendo would fetch its own catalogue index, and a device module
#: would advertise itself on the network mid-run.
_OTHER_MODULES = {
    "input_modules.jamendo.enabled": False,
    "devices.musiccast.enabled": False,
    "devices.dummydevice.enabled": False,
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def bench_env() -> None:
    """Arm the timing wrappers and the timestamped log format for every
    process the launcher starts. Set in this process, inherited from here."""
    os.environ["KALINKA_BENCH_TIMING"] = "1"
    os.environ["KALINKA_LOG_FORMAT"] = "full"
    existing = os.environ.get("PYTHONPATH", "")
    entries = [str(TIMING_DIR), *(e for e in existing.split(os.pathsep) if e)]
    os.environ["PYTHONPATH"] = os.pathsep.join(entries)


def overrides(
    layout: Layout, mood: bool, top_k: int, port: int,
    candidates: Optional[int] = None,
) -> dict[str, Any]:
    """The configuration the benchmark runs against.

    Shipped defaults except where the benchmark's terms differ from a
    listener's: the ranked list is as deep as the benchmark scores, metadata
    enrichment is off (it would fetch names for audio whose names are what we
    are hiding), and nothing rescans behind the run.
    """
    return {
        "base_config.server.interface": "lo",
        "base_config.server.port": port,
        "base_config.server.service_name": "Kalinka SDD benchmark",
        "base_config.server.oobe_complete": True,
        "base_config.search.ai_suggestions_limit": top_k,
        "input_modules.localfiles.music_folders": [str(layout.audio)],
        "input_modules.localfiles.scan_interval_minutes": 1440,
        "input_modules.localfiles.file_watch_enabled": False,
        "input_modules.localfiles.enricher.enabled": False,
        "input_modules.localfiles.ai_search.enabled": True,
        "input_modules.localfiles.ai_search.max_results": top_k,
        "input_modules.localfiles.ai_search.mood.enabled": mood,
        **(
            {"input_modules.localfiles.ai_search.knn_candidate_limit": candidates}
            if candidates
            else {}
        ),
        **_OTHER_MODULES,
    }


def build(
    layout: Layout, mood: bool = True, top_k: int = 50,
    candidates: Optional[int] = None,
) -> KalinkaInstance:
    """A configured, not-yet-started instance in ``<out>/fakeroot``.

    A fakeroot that already holds an index is reused as it stands — the
    library is the expensive part of the run — and only its configuration is
    rewritten, since the port it was given is not the port it gets next time.
    """
    bench_env()
    instance = KalinkaInstance(
        REPO_ROOT,
        layout.out / "fakeroot",
        free_port(),
        Path(sys.prefix),
        layout.out / "server.pid",
    )
    config = overrides(
        layout, mood=mood, top_k=top_k, port=instance.port, candidates=candidates
    )
    if instance.db_path.exists():
        instance.config_path.write_text(json.dumps(config, indent=2))
    else:
        instance.install(config, layout.models)
    return instance


def set_mood(instance: KalinkaInstance, enabled: bool) -> None:
    """Flip valence/arousal ranking through the settings API and restart, which
    is the ablation: the index is untouched, only the ranking changes."""
    instance.put(
        "/server/config",
        json={"input_modules.localfiles.ai_search.mood.enabled": enabled},
    )
    instance.restart()


class MoodAblation:
    """Puts the instance into the ranking configuration a run is named for.

    It restarts only when the setting actually changes, and it knows what the
    instance was built with — so the order the runs are asked in cannot
    decide what a run measured. Each restart is timed under a span of its
    own, because a run list may ask for more than one.
    """

    def __init__(
        self,
        instance: KalinkaInstance,
        clock: Clock,
        wanted: Mapping[str, bool],
        applied: bool = True,
    ):
        self._instance = instance
        self._clock = clock
        self._wanted = wanted
        self._applied = applied

    def prepare(self, run_name: str) -> None:
        enabled = self._wanted[run_name]
        if enabled == self._applied:
            return
        with self._clock.span(f"ablation_{run_name}"):
            set_mood(self._instance, enabled)
        self._applied = enabled


def stage(instance: KalinkaInstance, name: str) -> dict | None:
    return instance.indexer_status().get(name)


def settled(stage_status: dict | None) -> bool:
    return (
        stage_status is not None
        and stage_status["pending"] == 0
        and stage_status["in_progress"] == 0
    )


def describe(stage_status: dict | None) -> str:
    if stage_status is None:
        return "not reported"
    return (
        f"{stage_status['done']} done, {stage_status['pending']} pending, "
        f"{stage_status['failed']} failed of {stage_status['total']}"
    )


__all__ = [
    "KalinkaInstance",
    "MoodAblation",
    "build",
    "describe",
    "free_port",
    "set_mood",
    "settled",
    "stage",
    "wait_until",
]
