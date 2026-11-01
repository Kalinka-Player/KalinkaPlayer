"""Requests to the root helper, and reading its answers back."""

import json
import os
import time

from kalinka_server.storage import requests as requests_mod
from kalinka_server.storage.requests import MountRequests


def _requests(tmp_path):
    (tmp_path / "req").mkdir()
    (tmp_path / "res").mkdir()
    return MountRequests(str(tmp_path / "req"), str(tmp_path / "res"))


def _asked(tmp_path):
    return [json.loads(p.read_text()) for p in sorted((tmp_path / "req").iterdir())]


def test_each_request_is_a_file_of_its_own_in_the_order_asked(tmp_path):
    requests = _requests(tmp_path)
    requests.unmount("rec_nas")
    requests.mount("rec_nas", "nfs4", "nas:/music", ["ro"])

    names = sorted(p.name for p in (tmp_path / "req").iterdir())
    asked = _asked(tmp_path)
    assert [a["action"] for a in asked] == ["unmount", "mount"]
    assert names == [f"{a['seq']}.rec_nas.json" for a in asked]
    assert asked[1]["source"] == "nas:/music"


def _answer(tmp_path, seq, ok=True, error=None):
    (tmp_path / "res/rec_nas.json").write_text(json.dumps({"ok": ok, "error": error, "seq": seq, "at": 1.0}))


def test_an_answer_to_an_earlier_request_does_not_count(tmp_path):
    requests = _requests(tmp_path)
    requests.unmount("rec_nas")
    first = _asked(tmp_path)[0]["seq"]
    requests.mount("rec_nas", "nfs4", "nas:/music", ["ro"])
    _answer(tmp_path, first, ok=False, error="old")

    assert requests.pending("rec_nas")
    assert requests.outcome("rec_nas") is None


def test_the_answer_to_the_request_is_read(tmp_path):
    requests = _requests(tmp_path)
    requests.unmount("rec_nas")
    _answer(tmp_path, _asked(tmp_path)[0]["seq"])

    assert not requests.pending("rec_nas")
    assert requests.outcome("rec_nas").ok


def test_a_request_nobody_answers_times_out(tmp_path, monkeypatch):
    requests = _requests(tmp_path)
    requests.unmount("rec_nas")
    monkeypatch.setattr(requests_mod, "ANSWER_TIMEOUT_S", 0.0)

    assert not requests.pending("rec_nas")
    assert requests.outcome("rec_nas").error == "The mount helper did not answer"


def test_a_request_queued_behind_a_slow_one_waits_its_turn(tmp_path, monkeypatch):
    requests = _requests(tmp_path)
    monkeypatch.setattr(requests_mod, "ANSWER_TIMEOUT_S", 0.2)
    requests.mount("rec_b", "nfs4", "nas:/b", ["ro"])
    time.sleep(0.3)
    assert not requests.pending("rec_b")

    # The helper answered another request just now, so this one is next.
    os.utime(tmp_path / "res")
    assert requests.pending("rec_b")
    assert requests.outcome("rec_b") is None
