#!/usr/bin/env python3
"""Fingerprinting a file the decoder could only partly read.

``fpcalc`` stops at the first unreadable frame, reports a non-zero exit, and
still prints the fingerprint it built from everything before it. A rip with
corrupt frames is precisely the case where the tags deserve least trust, so
throwing that fingerprint away loses identification exactly where it is worth
most. The exit status is therefore not what decides — the output is.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.enricher.acoustid_plugin import AcoustIdPlugin
from kalinka_plugin_localfiles.storage.local import LocalStorage

TRUNCATED_MP3 = "/music/Dolphin Smiles/06 - The Farthest Shore.mp3"
DECODE_ERROR = "ERROR: Error reading from the audio source (Invalid data found)"


@pytest.fixture
def plugin(tmp_path):
    config = LocalFilesConfig(
        music_folders=[str(tmp_path)],
        db_path=str(tmp_path / "localfiles.db"),
        artwork_path=str(tmp_path / "artwork"),
    )
    return AcoustIdPlugin(config, db_manager=MagicMock())


def _completed(returncode, stdout, stderr=""):
    return subprocess.CompletedProcess(
        args=["fpcalc", "-json", TRUNCATED_MP3],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _run(plugin, completed):
    # The track is a stand-in for a real one, so its storage is told it is
    # there; a local file is handed to fpcalc by name and never read here.
    with patch.object(LocalStorage, "is_file", return_value=True), patch(
        "subprocess.run", return_value=completed
    ):
        return plugin._generate_fingerprint(TRUNCATED_MP3)


class TestAPartialRead:
    def test_a_fingerprint_survives_a_non_zero_exit(self, plugin):
        """The regression this file exists for: exit 3 with usable output."""
        out = json.dumps({"duration": 428.71, "fingerprint": "AQAAqEmTZWqk"})
        assert _run(plugin, _completed(3, out, DECODE_ERROR)) == (
            "AQAAqEmTZWqk",
            428.71,
        )

    def test_a_clean_run_is_unaffected(self, plugin):
        out = json.dumps({"duration": 210, "fingerprint": "AQADtEmYhY"})
        assert _run(plugin, _completed(0, out)) == ("AQADtEmYhY", 210)


class TestAFailureThatYieldsNothing:
    def test_no_output_at_all_is_still_a_failure(self, plugin):
        assert _run(plugin, _completed(2, "", "Unable to open file")) == (None, None)

    def test_the_reason_is_reported_not_a_json_error(self, plugin, caplog):
        """A genuine failure now arrives as a decode error, so the message has
        to carry what fpcalc actually said or the cause is unrecoverable."""
        _run(plugin, _completed(2, "", "Unable to open file"))

        logged = caplog.text
        assert "Unable to open file" in logged
        assert "exit 2" in logged

    def test_output_without_a_fingerprint_is_a_failure(self, plugin):
        assert _run(plugin, _completed(0, json.dumps({"duration": 100}))) == (
            None,
            None,
        )

    def test_output_without_a_duration_is_a_failure(self, plugin):
        out = json.dumps({"fingerprint": "AQADtEmYhY"})
        assert _run(plugin, _completed(0, out)) == (None, None)


class TestWhenFpcalcCannotRun:
    def test_a_missing_binary_is_reported(self, plugin, caplog):
        with patch.object(LocalStorage, "is_file", return_value=True), patch(
            "subprocess.run", side_effect=FileNotFoundError()
        ):
            assert plugin._generate_fingerprint(TRUNCATED_MP3) == (None, None)
        assert "chromaprint" in caplog.text

    def test_a_timeout_is_still_caught(self, plugin):
        """Dropping ``check`` must not stop a hung fpcalc being handled."""
        with patch.object(LocalStorage, "is_file", return_value=True), patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="fpcalc", timeout=30),
        ):
            assert plugin._generate_fingerprint(TRUNCATED_MP3) == (None, None)

    def test_a_missing_file_is_not_run_at_all(self, plugin):
        with patch.object(LocalStorage, "is_file", return_value=False), patch(
            "subprocess.run"
        ) as run:
            assert plugin._generate_fingerprint(TRUNCATED_MP3) == (None, None)
        run.assert_not_called()
