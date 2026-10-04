"""``/server/config`` writes to a list of records holding credentials.

Contract:

* An app writes back the entries it read, which never carry a credential.
  When an edit points a share at another server, port or user, the saved
  password cannot go with it: the dry run and the save both answer with an
  error at that password's own path, and the save keeps nothing.
* A password the app sends, even an empty one, is what it asked for and is
  not reported; nor is one the edit keeps.
* A save answers with the resulting ``secrets_set``, as ``GET`` builds it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalinka_plugin_localfiles.config_model import (
    AccountSignIn,
    LocalFilesConfig,
    SmbLocation,
    SmbSource,
)
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.config_route import register_config_routes
from kalinka_server.options_registry import OptionsRegistry
from kalinka_server.player_setup import ModuleHealthState

SECRET = "hunter2-do-not-log"
_SOURCES = "input_modules.localfiles.music_sources"


@dataclass
class _Prepared:
    plugin_context: Any
    health_state: ModuleHealthState = ModuleHealthState.READY
    error_message: str | None = None
    plugin_instance: Any = None


def _share(id: str) -> SmbSource:
    return SmbSource(
        id=id,
        location=SmbLocation(host=f"{id}.local", path="music"),
        authentication=AccountSignIn(username="media", password=SECRET),
    )


@pytest.fixture
def library():
    return LocalFilesConfig(
        music_sources=[_share("nas"), _share("den"), _share("attic")]
    )


@pytest.fixture
def client(tmp_path, library):
    app = FastAPI()
    app.state.config = KalinkaConfig()
    app.state.schema_version = "1"
    app.state.dynamic_paths = frozenset()
    app.state.dynamic_field_registry = {}
    app.state.options_registry = OptionsRegistry()
    app.state.overrides = {}
    app.state.overrides_file = str(tmp_path / "overrides.json")
    modules = SimpleNamespace(
        prepared_input_modules={
            "localfiles": _Prepared(SimpleNamespace(config=library))
        },
        prepared_devices={},
    )
    register_config_routes(app, app.state.config, modules)
    return TestClient(app)


def _read(client) -> list[dict[str, Any]]:
    return client.get("/server/config").json()["values"][_SOURCES]


def _edited(client, **edit) -> list[dict[str, Any]]:
    """The sources as the app read them, with the first one edited."""
    sources = _read(client)
    first = sources[0]
    for part in ("location", "authentication"):
        first[part].update(edit.get(part, {}))
    return sources


def _body(sources) -> dict[str, Any]:
    return {"schema_version": "1", "changes": {_SOURCES: sources}}


_ELSEWHERE = [
    pytest.param({"location": {"host": "other.local"}}, id="host"),
    pytest.param({"location": {"port": 4450}}, id="port"),
    pytest.param({"authentication": {"username": "guest"}}, id="user"),
]
_PASSWORD = f"{_SOURCES}.nas.authentication.password"


def _issue_paths(answer) -> list[str]:
    return [issue["path"] for issue in answer.json()["issues"]]


def _dry_run(client, sources) -> list[str]:
    return _issue_paths(client.post("/server/config/validate", json=_body(sources)))


class TestASharePointedElsewhere:
    @pytest.mark.parametrize("edit", _ELSEWHERE)
    def test_the_dry_run_asks_for_the_password_again(self, client, edit):
        answer = client.post(
            "/server/config/validate", json=_body(_edited(client, **edit))
        )

        [issue] = answer.json()["issues"]
        assert (issue["path"], issue["severity"]) == (_PASSWORD, "error")
        assert "enter the password again" in issue["message"]

    @pytest.mark.parametrize("edit", _ELSEWHERE)
    def test_the_save_is_refused_the_same_way(self, client, library, edit):
        answer = client.put("/server/config", json=_body(_edited(client, **edit)))

        assert answer.status_code == 422
        assert _issue_paths(answer) == [_PASSWORD]
        assert library.music_sources[0].location.host == "nas.local"
        assert library.music_sources[0].authentication.password == SECRET

    @pytest.mark.parametrize("password", ["new", ""])
    def test_a_password_sent_with_it_is_what_was_asked_for(self, client, password):
        edit = {
            "location": {"host": "other.local"},
            "authentication": {"password": password},
        }
        sources = _edited(client, **edit)

        assert _dry_run(client, sources) == []
        assert client.put("/server/config", json=_body(sources)).status_code == 200


class TestAnEditThatKeepsTheScope:
    def test_nothing_is_reported_and_the_password_is_kept(self, client, library):
        sources = _edited(client, location={"path": "music/jazz"})

        assert _dry_run(client, sources) == []
        answer = client.put("/server/config", json=_body(sources))
        assert answer.status_code == 200
        assert _issue_paths(answer) == []
        assert library.music_sources[0].authentication.password == SECRET


class TestWhatASaveAnswers:
    def test_the_credentials_now_set(self, client):
        nas, _den, attic = _read(client)
        nas["authentication"]["password"] = ""

        answer = client.put("/server/config", json=_body([nas, attic]))

        assert answer.json()["secrets_set"] == [
            f"{_SOURCES}.attic.authentication.password"
        ]
        assert (
            answer.json()["secrets_set"]
            == client.get("/server/config").json()["secrets_set"]
        )

    def test_no_credential_is_in_it(self, client):
        answer = client.put("/server/config", json=_body(_read(client)))
        assert SECRET not in json.dumps(answer.json())
