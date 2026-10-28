"""Shared test setup.

The MusicBrainz client paces real request starts a second apart; tests call
it with stubs, so the pacing would only add dead wall-clock (seconds per
test that exercises a multi-request match). Zero it for the suite — the
pacing logic itself is covered explicitly in ``test_mb_pacing.py``.
"""

import aiosqlite
import pytest

from kalinka_plugin_localfiles.enricher import mb_client


@pytest.fixture(autouse=True)
def _no_musicbrainz_pacing(monkeypatch):
    monkeypatch.setattr(mb_client._pacer, "_min_interval", 0.0)


@pytest.fixture
def opened(monkeypatch):
    """Every library-database connection opened while the test runs."""
    connections = []
    connect = aiosqlite.connect

    def counting(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(aiosqlite, "connect", counting)
    return connections
