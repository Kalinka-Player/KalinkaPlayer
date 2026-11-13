"""Background workers yield the CPU to playback, the server and searches."""

import importlib
import os
import subprocess
import sys
import textwrap

import pytest

from kalinka_plugin_localfiles.worker_utils import BACKGROUND_NICE


def test_every_thread_of_a_lowered_worker_runs_at_background_priority():
    """Threads it had already (numpy's BLAS pool starts on import) and
    threads it starts later. In a child process: the test run's own
    priority could not be raised back."""
    probe = textwrap.dedent(
        """
        import os, threading
        from kalinka_plugin_localfiles.worker_utils import lower_priority

        def own_priority(into, after=None):
            if after is not None:
                after.wait()
            into.append(os.getpriority(os.PRIO_PROCESS, 0))

        lowered = threading.Event()
        earlier, later = [], []
        already_running = threading.Thread(
            target=own_priority, args=(earlier, lowered), daemon=True
        )
        already_running.start()

        lower_priority()
        lower_priority()
        lowered.set()
        already_running.join()
        started_after = threading.Thread(target=own_priority, args=(later,))
        started_after.start()
        started_after.join()
        print(os.getpriority(os.PRIO_PROCESS, 0), earlier[0], later[0])
        """
    )
    printed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    expected = max(os.getpriority(os.PRIO_PROCESS, 0), BACKGROUND_NICE)
    assert [int(n) for n in printed.split()] == [expected] * 3


@pytest.mark.parametrize("worker_name", ["librarian", "embedder.embedder"])
def test_background_workers_lower_their_priority_before_starting_work(
    worker_name, monkeypatch
):
    worker = importlib.import_module(f"kalinka_plugin_localfiles.{worker_name}")
    calls = []

    async def work(*args):
        calls.append("work")

    monkeypatch.setattr(worker, "async_main", work)
    monkeypatch.setattr(worker, "set_proc_title", lambda name: None)
    monkeypatch.setattr(worker, "configure_worker_logging", lambda queue: None)
    monkeypatch.setattr(worker, "lower_priority", lambda: calls.append("lower"))

    worker.main(None, None)

    assert calls == ["lower", "work"]

