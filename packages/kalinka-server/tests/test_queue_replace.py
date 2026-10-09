"""POST /queue/replace swaps the queue for new tracks in one request: only the
new tracks count against the limit, and a replacement refused for the limit or
for having nothing to play leaves the queue as it was."""

import asyncio

import pytest

from kalinka_plugin_sdk.datamodel import Album, EntityId, Track

from kalinka_server import demo_mode, queue_limit, server
from kalinka_server.config_model import KalinkaConfig
from kalinka_server.queue_add import NO_TRACKS
from tests.app_harness import isolate_app, running

ALBUM = Album(id=EntityId.from_string("kalinka:jamendo:album:1"), title="Album")
FOLDER = "kalinka:localfiles:folder:"

LIMITS = pytest.mark.parametrize(
    "demo, limit",
    [(False, queue_limit.QUEUE), (True, demo_mode.DEMO_QUEUE)],
    ids=["queue", "demo"],
)


def _track_id(name: str) -> str:
    return f"kalinka:jamendo:track:{name}"


def _folder(size: int) -> str:
    """An id that expands to ``size`` tracks."""
    return f"{FOLDER}{size}"


async def _tracks_for(ids, *_):
    tracks = []
    for raw in ids:
        if raw.startswith(FOLDER):
            size = int(raw.removeprefix(FOLDER))
            names = [f"{size}-{n}" for n in range(size)]
        else:
            names = [raw.split(":")[-1]]
        tracks += [
            Track(
                id=EntityId.from_string(_track_id(name)),
                title=name,
                duration=60,
                album=ALBUM,
            )
            for name in names
        ]
    return tracks


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "tracks_for", _tracks_for)
    return tmp_path


async def _app(tmp_path, demo: bool):
    config = KalinkaConfig()
    config.server.demo_mode = demo
    return await server.create_app(str(tmp_path / "overrides.json"), config, {})


async def _queued(client) -> list[str]:
    listing = await client.get("/queue/list", params={"limit": 10000})
    return [track["id"] for track in listing.json()["items"]]


@pytest.mark.parametrize("demo", [False, True], ids=["queue", "demo"])
async def test_a_replace_leaves_only_the_new_tracks_in_order(isolated, demo):
    async with running(await _app(isolated, demo)) as client:
        await client.post("/queue/add", json=[_track_id("a"), _track_id("b")])

        replaced = await client.post(
            "/queue/replace", json=[_track_id("z"), _track_id("x"), _track_id("y")]
        )

        assert replaced.status_code == 200
        assert replaced.json()["count"] == 3
        assert await _queued(client) == [_track_id(n) for n in ("z", "x", "y")]


@LIMITS
async def test_the_tracks_queued_now_do_not_count_against_a_replace(
    isolated, demo, limit
):
    async with running(await _app(isolated, demo)) as client:
        filled = await client.post("/queue/add", json=[_folder(limit.tracks)])
        assert filled.status_code == 200

        replaced = await client.post("/queue/replace", json=[_folder(limit.tracks)])

        assert replaced.status_code == 200
        queued = await _queued(client)
        assert len(queued) == limit.tracks
        assert queued[0] == _track_id(f"{limit.tracks}-0")


@LIMITS
async def test_a_replace_of_more_ids_than_the_limit_keeps_the_queue(
    isolated, demo, limit
):
    async with running(await _app(isolated, demo)) as client:
        await client.post("/queue/add", json=[_track_id("a"), _track_id("b")])

        ids = [_track_id(str(n)) for n in range(limit.tracks + 1)]
        refused = await client.post("/queue/replace", json=ids)

        assert refused.status_code == 409
        assert refused.json() == {"detail": limit.replacement_refusal}
        assert await _queued(client) == [_track_id("a"), _track_id("b")]


@LIMITS
async def test_a_replace_that_expands_past_the_limit_keeps_the_queue(
    isolated, demo, limit
):
    async with running(await _app(isolated, demo)) as client:
        await client.post("/queue/add", json=[_track_id("a")])

        refused = await client.post("/queue/replace", json=[_folder(limit.tracks + 1)])

        assert refused.status_code == 409
        assert refused.json() == {"detail": limit.replacement_refusal}
        assert await _queued(client) == [_track_id("a")]


@LIMITS
def test_a_refused_replace_has_the_add_code_but_not_its_advice(demo, limit):
    assert limit.replacement_refusal["code"] == limit.refusal["code"]
    assert "clear" not in limit.replacement_refusal["message"]


@pytest.mark.parametrize("ids", [[], [_folder(0)]], ids=["no ids", "empty folder"])
async def test_a_replace_with_no_tracks_to_play_keeps_the_queue(isolated, ids):
    async with running(await _app(isolated, demo=False)) as client:
        await client.post("/queue/add", json=[_track_id("a")])

        refused = await client.post("/queue/replace", json=ids)

        assert refused.status_code == 422
        assert refused.json() == {"detail": NO_TRACKS}
        assert await _queued(client) == [_track_id("a")]


async def test_a_replace_whose_lookup_fails_keeps_the_queue(isolated, monkeypatch):
    async def failing(*_):
        raise RuntimeError("source down")

    async with running(await _app(isolated, demo=False)) as client:
        await client.post("/queue/add", json=[_track_id("a")])
        monkeypatch.setattr(server, "tracks_for", failing)

        with pytest.raises(RuntimeError):
            await client.post("/queue/replace", json=[_track_id("b")])

        assert await _queued(client) == [_track_id("a")]


async def test_a_replace_racing_an_add_never_passes_the_limit(isolated, monkeypatch):
    app = await _app(isolated, demo=False)
    playqueue = app.state.player_context.playqueue
    checked, release = asyncio.Event(), asyncio.Event()
    add = playqueue.add

    async def add_once_released(*args, **kwargs):
        checked.set()
        await release.wait()
        await add(*args, **kwargs)

    monkeypatch.setattr(playqueue, "add", add_once_released)
    full = queue_limit.QUEUE.tracks
    async with running(app) as client:
        adding = asyncio.create_task(client.post("/queue/add", json=[_folder(60)]))
        await checked.wait()
        replacing = asyncio.create_task(
            client.post("/queue/replace", json=[_folder(full)])
        )
        # Time for a replace that ignored the add's lock to land first.
        await asyncio.wait({replacing}, timeout=0.2)
        release.set()

        assert (await adding).status_code == 200
        assert (await replacing).status_code == 200
        queued = await _queued(client)
        assert len(queued) == full
        assert queued[0] == _track_id(f"{full}-0")
