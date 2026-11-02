"""A feature's optional packages install at the next start, whatever it is.

Turning a feature on queues its packages as the setting is saved, for
bootstrap.sh to install on the way up; a reboot or the streamer's own
Restart server asks nothing of the server first. Turning it off again takes
them back out. PUT /server/restart queues the same, after what its body asks
for, and refuses a key it does not know.
"""

import json
from typing import ClassVar

import pytest
from pydantic import Field

from kalinka_plugin_sdk import OptionalPackageSpec
from kalinka_plugin_sdk import __version__ as SDK_VERSION
from kalinka_plugin_sdk.inputmodule import InputModule
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import InputModulePlugin

from kalinka_server import server, update_check
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.player_setup import modules
from tests.app_harness import isolate_app, running

ART = "kalinka-test-art"
OTHER = "kalinka-test-other"


class _ArtConfig(ModuleConfig):
    name: str = Field(default="art", title="Art", frozen=True, exclude=True)
    generated_art: bool = False


class _Module(InputModule):
    def module_name(self) -> str:
        return "art"


class _ArtPlugin(InputModulePlugin):
    PLUGIN_ID = "art"
    REQUIRES_SDK = f"=={SDK_VERSION}"
    CONFIG_MODEL = _ArtConfig
    OPTIONAL_PACKAGES: ClassVar[dict[str, OptionalPackageSpec]] = {
        key: OptionalPackageSpec(
            pip_spec=f"{key}==1.0",
            description=f"{key} for generated art",
            import_name=key.replace("-", "_"),
        )
        for key in (ART, OTHER)
    }
    failure: ClassVar[Exception | None] = None

    async def setup(self, context):
        self._context = context
        self._interface = _Module()

    def get_interface(self):
        return self._interface

    async def required_packages(self) -> list[str]:
        if self.failure is not None:
            raise self.failure
        return [ART] if self._context.config.generated_art else []


@pytest.fixture
async def client(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        modules, "_scan_entry_points", lambda: iter([("art", _ArtPlugin)])
    )
    app = await server.create_app(str(tmp_path / "overrides.json"), KalinkaConfig(), {})
    async with running(app) as client:
        yield client


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "var" / "lib" / "kalinka"


def _queued(state_dir) -> list[str] | None:
    pending = state_dir / "pending_installs.json"
    return json.loads(pending.read_text())["requested"] if pending.exists() else None


async def _save(client, generated_art: bool):
    version = (await client.get("/server/config")).json()["schema_version"]
    return await client.put(
        "/server/config",
        json={
            "schema_version": version,
            "changes": {"input_modules.art.generated_art": generated_art},
        },
    )


async def test_saving_a_feature_on_queues_its_packages(client, state_dir):
    saved = await _save(client, True)

    assert saved.status_code == 200
    assert _queued(state_dir) == [ART]
    catalog = (await client.get("/server/optional_packages")).json()
    assert {p["key"]: p["pending"] for p in catalog["packages"]} == {
        ART: True,
        OTHER: False,
    }


async def test_saving_it_off_again_takes_them_back_out(client, state_dir):
    await _save(client, True)
    assert _queued(state_dir) == [ART]

    saved = await _save(client, False)

    assert saved.status_code == 200
    assert _queued(state_dir) is None


async def test_a_plugin_that_fails_to_answer_does_not_fail_the_save(
    client, state_dir, monkeypatch, caplog
):
    monkeypatch.setattr(_ArtPlugin, "failure", RuntimeError("client_id=s3cret"))

    with caplog.at_level("WARNING"):
        saved = await _save(client, True)

    assert saved.status_code == 200
    assert modules.prepared_input_modules["art"].plugin_context.config.generated_art
    assert _queued(state_dir) is None
    assert "art required_packages() failed (RuntimeError)" in caplog.text
    assert "s3cret" not in caplog.text


async def test_a_queue_that_cannot_be_written_does_not_fail_the_save(
    client, state_dir, caplog
):
    (state_dir / "pending_installs.json.tmp").mkdir(parents=True)

    saved = await _save(client, True)

    assert saved.status_code == 200
    assert modules.prepared_input_modules["art"].plugin_context.config.generated_art
    assert "Could not queue the optional packages needed" in caplog.text


async def test_a_restart_queues_what_it_names_then_what_the_plugins_need(
    client, state_dir
):
    await _save(client, True)

    restarted = await client.put("/server/restart", json={"install": [OTHER]})

    assert restarted.status_code == 200
    assert restarted.json()["install_queued"] == [OTHER, ART]
    assert _queued(state_dir) == [OTHER, ART]
    assert update_check.restart_pending()


async def test_a_restart_refuses_a_key_it_does_not_know(client):
    restarted = await client.put("/server/restart", json={"install": ["bogus", OTHER]})

    assert restarted.status_code == 400
    assert restarted.json()["detail"] == {
        "message": "Unknown optional-package keys",
        "rejected": ["bogus"],
        "accepted": [OTHER],
    }
    assert not update_check.restart_pending()


async def test_a_restart_refuses_an_install_that_is_not_a_list(client):
    restarted = await client.put("/server/restart", json={"install": OTHER})

    assert restarted.status_code == 400
    assert not update_check.restart_pending()
