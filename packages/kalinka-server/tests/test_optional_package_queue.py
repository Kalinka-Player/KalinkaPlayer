"""A feature's optional packages install at the next start, whatever it is.

Turning a feature on queues its packages as the setting is saved, for
bootstrap.sh to install on the way up; a reboot or the streamer's own
Restart server asks nothing of the server first. Turning it off again takes
them back out. The server's own start queues what is still missing, except
an install that failed, which only a save or Restart Kalinka asks for again.
PUT /server/restart queues the same, after what its body asks for, and
refuses a key it does not know without queueing anything. The modules page
says what a restart does, and offers no package this box cannot install.
"""

import asyncio
import json
import sys
from typing import ClassVar

import pytest
from pydantic import Field

from kalinka_plugin_sdk import (
    ModuleHealthState,
    ModuleState,
    OptionalPackageSpec,
    wheel_architecture,
)
from kalinka_plugin_sdk import __version__ as SDK_VERSION
from kalinka_plugin_sdk.inputmodule import InputModule
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import InputModulePlugin

from kalinka_server import no_wheel_record, server, update_check
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
    health: ClassVar[ModuleState] = ModuleState(state=ModuleHealthState.READY)

    async def setup(self, context):
        self._context = context
        self._interface = _Module()

    def get_interface(self):
        return self._interface

    async def get_state(self) -> ModuleState:
        return self.health

    async def required_packages(self) -> list[str]:
        if self.failure is not None:
            raise self.failure
        return [ART] if self._context.config.generated_art else []


@pytest.fixture
def state_dir(tmp_path):
    state_dir = tmp_path / "var" / "lib" / "kalinka"
    state_dir.mkdir(parents=True)
    return state_dir


async def _start(monkeypatch, tmp_path, generated_art: bool = False):
    """The app, once its start has queued what the plugins still need."""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        modules, "_scan_entry_points", lambda: iter([("art", _ArtPlugin)])
    )
    app = await server.create_app(
        str(tmp_path / "overrides.json"),
        KalinkaConfig(),
        {"input_modules.art.generated_art": generated_art},
    )
    await app.state.package_queue_task
    return app


@pytest.fixture
async def app(monkeypatch, tmp_path, state_dir):
    return await _start(monkeypatch, tmp_path)


@pytest.fixture
async def client(app):
    async with running(app) as client:
        yield client


async def _eventually(condition) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the queue never got there")


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


async def _listed(client) -> dict:
    listed = (await client.get("/server/modules")).json()["input_modules"]
    return next(entry for entry in listed if entry["name"] == "art")


def _lacking(*keys: str) -> ModuleState:
    return ModuleState(
        state=ModuleHealthState.WARNING,
        message="Generated art unavailable.",
        missing_packages=list(keys),
    )


def _failed_last_install(state_dir, key: str) -> None:
    (state_dir / "last_install.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "requested": [key],
                "results": [{"key": key, "status": "failed"}],
            }
        )
    )


def _no_wheel_found(state_dir, key: str) -> str:
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    reason = f"No prebuilt {key}==1.0 for Python {python} on {wheel_architecture()}"
    no_wheel_record.write(
        state_dir / no_wheel_record.FILE_NAME,
        python,
        wheel_architecture(),
        {key: {"pip_spec": f"{key}==1.0", "reason": reason}},
    )
    return reason


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


async def test_a_save_queues_only_once_an_earlier_one_has_written(
    app, client, state_dir, monkeypatch
):
    """An answer the plugins gave before a later save never lands after it."""
    asked: list[list[str]] = []
    asking = asyncio.Event()
    answered = asyncio.Event()
    answer = _ArtPlugin.required_packages

    async def slow_first_answer(self):
        needed = await answer(self)
        asked.append(needed)
        if len(asked) == 1:
            asking.set()
            await answered.wait()
        return needed

    monkeypatch.setattr(_ArtPlugin, "required_packages", slow_first_answer)
    turning_on = asyncio.create_task(_save(client, True))
    await asyncio.wait_for(asking.wait(), 1)

    queueing = asyncio.Event()
    queue = app.state.queue_required_packages

    async def queue_again(*requested):
        # The later save's own queueing: one made once its value is in.
        if not modules.prepared_input_modules["art"].plugin_context.config.generated_art:
            queueing.set()
        return await queue(*requested)

    monkeypatch.setattr(app.state, "queue_required_packages", queue_again)
    turning_off = asyncio.create_task(_save(client, False))
    await asyncio.wait_for(queueing.wait(), 1)

    assert asked == [[ART]]
    answered.set()
    assert (await turning_on).status_code == 200
    assert (await turning_off).status_code == 200
    # The plugin set up again on each save answers too; none lands [ART] last.
    assert asked[0] == [ART] and all(answer == [] for answer in asked[1:])
    assert _queued(state_dir) is None


async def test_a_plugin_taken_down_leaves_the_queue_and_is_asked_again_once_up(
    monkeypatch, tmp_path, state_dir
):
    app = await _start(monkeypatch, tmp_path, generated_art=True)
    async with running(app):
        assert _queued(state_dir) == [ART]
        lifecycle = app.state.plugin_lifecycle

        await lifecycle.unload("art", gone=False)
        await _eventually(lambda: _queued(state_dir) is None)

        await lifecycle.load("art", purge=False)
        await _eventually(lambda: _queued(state_dir) == [ART])


async def test_the_start_queues_a_feature_on_whose_packages_never_installed(
    monkeypatch, tmp_path, state_dir
):
    """As after an upgrade that gives a feature already on a new package."""
    await _start(monkeypatch, tmp_path, generated_art=True)

    assert _queued(state_dir) == [ART]


async def test_the_start_holds_back_an_install_that_failed_and_a_save_retries_it(
    monkeypatch, tmp_path, state_dir
):
    """A box offline at every start would otherwise run pip at every one."""
    _failed_last_install(state_dir, ART)
    monkeypatch.setattr(_ArtPlugin, "health", _lacking(ART))
    app = await _start(monkeypatch, tmp_path, generated_art=True)

    queued_at_start = _queued(state_dir)
    async with running(app) as client:
        art = await _listed(client)
        saved = await _save(client, True)

    assert queued_at_start is None
    assert art["missing_packages"] == [ART]
    assert art["error_message"] == (
        f"Generated art unavailable.\n\nInstalling `{ART}` failed when Kalinka "
        "last started. Use **Restart Kalinka** to try again."
    )
    assert saved.status_code == 200
    assert _queued(state_dir) == [ART]


async def test_a_restart_queues_what_it_names_then_what_the_plugins_need(
    client, state_dir
):
    await _save(client, True)

    restarted = await client.put("/server/restart", json={"install": [OTHER]})

    assert restarted.status_code == 200
    assert restarted.json()["install_queued"] == [OTHER, ART]
    assert _queued(state_dir) == [OTHER, ART]
    assert update_check.restart_pending()


async def test_a_restart_refuses_a_key_it_does_not_know_and_queues_nothing(
    client, state_dir
):
    await _save(client, True)

    restarted = await client.put("/server/restart", json={"install": ["bogus", OTHER]})

    assert restarted.status_code == 400
    assert restarted.json()["detail"] == {
        "message": "Unknown optional-package keys",
        "rejected": ["bogus"],
        "accepted": [OTHER],
    }
    assert _queued(state_dir) == [ART]
    assert not update_check.restart_pending()


async def test_a_restart_refuses_an_install_that_is_not_a_list(client):
    restarted = await client.put("/server/restart", json={"install": OTHER})

    assert restarted.status_code == 400
    assert not update_check.restart_pending()


async def test_the_modules_page_says_a_restart_installs_what_is_missing(
    client, monkeypatch
):
    monkeypatch.setattr(_ArtPlugin, "health", _lacking(OTHER))
    await modules.refresh_health(publish=True)

    art = await _listed(client)

    assert art["missing_packages"] == [OTHER]
    assert art["error_message"] == (
        f"Generated art unavailable.\n\nRestart to finish setting up: `{OTHER}` "
        "installs as Kalinka starts."
    )


async def test_the_modules_page_offers_nothing_once_pip_found_no_wheel_of_one(
    monkeypatch, tmp_path, state_dir
):
    """#332: the module says so instead of offering an install that fails,
    and the package it needs alongside is of no use alone."""
    reason = _no_wheel_found(state_dir, ART)
    monkeypatch.setattr(_ArtPlugin, "health", _lacking(ART, OTHER))
    app = await _start(monkeypatch, tmp_path)

    async with running(app) as client:
        art = await _listed(client)
        saved = await _save(client, True)

    assert art["missing_packages"] == []
    assert art["error_message"] == (
        f"Generated art unavailable.\n\n`{ART}` is not available on this "
        f"device. {reason}."
    )
    assert saved.status_code == 200
    assert _queued(state_dir) is None


async def test_what_earlier_installs_found_is_read_as_a_plugin_is_set_up(
    app, client, state_dir, monkeypatch
):
    """The installs run before the server does; what they found is read
    with the registry, so a plugin set up again sees it."""
    monkeypatch.setattr(_ArtPlugin, "health", _lacking(ART))
    _no_wheel_found(state_dir, ART)
    await modules.refresh_health(publish=True)
    assert (await _listed(client))["missing_packages"] == [ART]

    await app.state.plugin_lifecycle.unload("art", gone=False)
    await app.state.plugin_lifecycle.load("art", purge=False)

    art = await _listed(client)
    assert art["missing_packages"] == []
    assert "is not available on this device" in art["error_message"]
