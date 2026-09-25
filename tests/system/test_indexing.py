"""Indexing a library split between a local folder and an SMB share.

One server, two storages, ten real recordings. The read-only checks come
first; the ones that take tracks away and bring them back run last, and
leave the library as they found it.
"""

from __future__ import annotations

import pytest
from kalinka_plugin_sdk.datamodel import EntityId

from recordings import ARTIST
from renderer import FakeRenderer
from server_instance import KalinkaInstance
from waiting import wait_until

SOURCE = "localfiles"
CONTENT = f"/content/{SOURCE}"

CHANGE_TIMEOUT_S = 120.0
AI_SEARCH_TIMEOUT_S = 180.0
RENDERER_TIMEOUT_S = 60.0


def _entity(track_id: str) -> str:
    return f"kalinka:{SOURCE}:track:{track_id}"


def _local_id(entity_id: str) -> str:
    return EntityId.from_string(entity_id).id


def _track_ids(items) -> set[str]:
    return {_local_id(item["track"]["id"]) for item in items if item.get("track")}


def _name_matches(kalinka: KalinkaInstance, query: str) -> list[dict]:
    return kalinka.get("/search/matches", query=query, sources=SOURCE)["items"]


def _ai_tracks(kalinka: KalinkaInstance, query: str) -> set[str]:
    items = kalinka.get("/ai_search", query=query, sources=SOURCE)["items"]
    return _track_ids(items[0].get("sections") or []) if items else set()


def test_module_is_ready_with_both_folders_reachable(kalinka, planted):
    modules = kalinka.get("/server/modules")["input_modules"]
    localfiles = next(module for module in modules if module["name"] == SOURCE)
    assert localfiles["enabled"]
    assert localfiles["state"] == "ready", localfiles["error_message"]


def test_every_planted_track_is_indexed_under_its_root(planted, library):
    assert {track.library_path for track in planted.all} <= set(library)
    for root, tracks in planted.per_root.items():
        indexed = [path for path in library if path.startswith(root + "/")]
        assert len(indexed) == len(tracks), root
    assert len(library) == len(planted.all)
    for track in planted.all:
        row = library[track.library_path]
        assert row["duration"] > 0
        assert row["title"]


def test_enrichment_ran_over_the_whole_library(kalinka, library):
    stage = kalinka.indexer_status()["enrichment"]
    assert stage["pending"] == 0 and stage["in_progress"] == 0
    assert stage["done"] > 0, f"nothing was enriched: {stage}"
    unresolved = kalinka.rows(
        "SELECT id FROM artists WHERE enriched = 0 AND id != 'unknown_artist'"
    )
    assert not unresolved


def test_name_search_finds_each_track_by_title(kalinka, planted, library):
    for track in planted.all:
        row = library[track.library_path]
        hits = _track_ids(_name_matches(kalinka, row["title"]))
        assert row["id"] in hits, f"{row['title']!r} ({track.library_path}) not found"


def test_name_search_finds_the_albums_and_the_artist(kalinka, planted, library):
    for tracks in (planted.local, planted.smb):
        album_id = library[tracks[0].library_path]["album_id"]
        title = kalinka.rows("SELECT title FROM albums WHERE id = ?", (album_id,))[0]["title"]
        items = _name_matches(kalinka, title)
        assert any(
            item.get("album") and _local_id(item["album"]["id"]) == album_id for item in items
        ), title
    items = _name_matches(kalinka, ARTIST)
    assert any(item.get("artist") for item in items)


def test_ai_search_answers_a_mood_query_from_both_storages(kalinka, planted, library):
    found: set[str] = set()

    def answered() -> bool:
        found.update(_ai_tracks(kalinka, "calm and peaceful piano"))
        return bool(found)

    wait_until(answered, timeout=AI_SEARCH_TIMEOUT_S, what="AI search to answer", interval=5.0)
    assert found <= {row["id"] for row in library.values()}
    for root, tracks in planted.per_root.items():
        ids = {library[track.library_path]["id"] for track in tracks}
        assert found & ids, f"no AI hit from under {root}"
    audio = kalinka.indexer_status()["clap_audio"]
    assert audio["done"] == len(library), audio


@pytest.mark.parametrize("which", ["local", "smb"])
def test_content_route_serves_the_track_bytes(kalinka, planted, library, which):
    track = getattr(planted, which)[0]
    row = library[track.library_path]
    expected = track.content

    full = kalinka.raw("GET", f"{CONTENT}/{row['id']}")
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("audio/mpeg")
    assert full.content == expected

    head = kalinka.raw("HEAD", f"{CONTENT}/{row['id']}")
    assert head.status_code == 200
    assert int(head.headers["content-length"]) == len(expected)

    partial = kalinka.raw("GET", f"{CONTENT}/{row['id']}", headers={"Range": "bytes=100-199"})
    assert partial.status_code == 206
    assert partial.headers["content-range"] == f"bytes 100-199/{len(expected)}"
    assert partial.content == expected[100:200]


def test_content_route_rejects_an_unknown_track(kalinka, planted):
    assert kalinka.raw("GET", f"{CONTENT}/no-such-track").status_code == 404


def test_renderer_is_handed_a_track_url_on_this_server(kalinka, planted, library):
    with FakeRenderer(kalinka.base_url) as renderer:
        assert renderer.server_id == kalinka.server_id
        renderers = kalinka.get("/renderer/list")
        assert any(r["renderer_id"] == renderer.RENDERER_ID for r in renderers["renderers"])

        for track in (planted.smb[0], planted.local[0]):
            row = library[track.library_path]
            kalinka.put("/queue/clear")
            renderer.forget_sources()
            kalinka.post("/queue/add", json=[_entity(row["id"])])
            try:
                kalinka.put("/queue/play")
                uri = renderer.next_source(RENDERER_TIMEOUT_S)
                assert uri == f"{kalinka.base_url}{CONTENT}/{row['id']}"
                served = kalinka.raw("GET", uri)
                assert served.status_code == 200
                assert served.content == track.content
            finally:
                kalinka.put("/queue/stop")
        kalinka.put("/queue/clear")


def test_local_removal_is_seen_by_the_watcher_and_undone_by_a_copy(kalinka, planted, library):
    track = planted.local[-1]
    row = library[track.library_path]

    track.remove()
    wait_until(
        lambda: track.library_path not in kalinka.tracks(),
        timeout=CHANGE_TIMEOUT_S,
        what="the deleted local track to leave the library",
    )
    assert row["id"] not in _track_ids(_name_matches(kalinka, row["title"]))
    assert kalinka.raw("GET", f"/get/{_entity(row['id'])}").status_code != 200
    assert kalinka.raw("GET", f"{CONTENT}/{row['id']}").status_code == 404

    track.restore()
    wait_until(
        lambda: track.library_path in kalinka.tracks(),
        timeout=CHANGE_TIMEOUT_S,
        what="the restored local track to be indexed again",
    )
    restored = kalinka.tracks()[track.library_path]
    assert restored["id"] in _track_ids(_name_matches(kalinka, restored["title"]))


def test_smb_removal_is_applied_by_the_next_scan_after_a_restart(kalinka, planted, library):
    track = planted.smb[-1]
    row = library[track.library_path]
    local_paths = {t.library_path for t in planted.local}

    track.remove()
    kalinka.restart()
    wait_until(
        lambda: track.library_path not in kalinka.tracks(),
        timeout=CHANGE_TIMEOUT_S,
        what="the deleted share track to leave the library after the restart",
    )
    tracks = kalinka.tracks()
    assert local_paths <= set(tracks)
    assert len(tracks) == len(library) - 1
    assert kalinka.raw("GET", f"{CONTENT}/{row['id']}").status_code == 404

    track.restore()
    kalinka.restart()
    wait_until(
        lambda: track.library_path in kalinka.tracks(),
        timeout=CHANGE_TIMEOUT_S,
        what="the restored share track to be indexed again",
    )
    restored = kalinka.tracks()[track.library_path]
    assert len(kalinka.tracks()) == len(library)
    assert kalinka.raw("GET", f"{CONTENT}/{restored['id']}").content == track.content


def test_a_share_that_goes_down_keeps_its_tracks(kalinka, samba, planted, library):
    """A share nobody can reach is not a library someone emptied.

    Every file under it looks gone, and none of them may be purged for it —
    that is the whole difference between a NAS that is switched off and a
    library that was deleted. The deletion the library really did owe is
    applied once the share answers again, which is what says the sweep
    deferred the work rather than forgot it.
    """
    track = planted.smb[-1]
    row = library[track.library_path]
    smb_paths = {planted_track.library_path for planted_track in planted.smb}

    # Gone for real — but only the share can testify to that, and it is
    # about to stop being able to.
    track.remove()
    samba.stop()
    try:
        kalinka.restart()
        blind = kalinka.tracks()
        assert smb_paths <= set(blind), "an unreachable share lost tracks"
        assert len(blind) == len(library)

        # A renderer retries a 5xx and abandons the track on a 4xx, so a
        # share that is down must not read as a track that is missing.
        assert kalinka.raw("GET", f"{CONTENT}/{row['id']}").status_code == 503

        modules = kalinka.get("/server/modules")["input_modules"]
        localfiles = next(module for module in modules if module["name"] == SOURCE)
        assert localfiles["state"] == "warning"
        assert "Music folder access" in localfiles["error_message"]
    finally:
        samba.start()

    kalinka.restart()
    wait_until(
        lambda: track.library_path not in kalinka.tracks(),
        timeout=CHANGE_TIMEOUT_S,
        what="the deletion owed from while the share was unreachable",
    )
    tracks = kalinka.tracks()
    assert smb_paths - {track.library_path} <= set(tracks)
    assert len(tracks) == len(library) - 1

    track.restore()
    kalinka.restart()
    wait_until(
        lambda: track.library_path in kalinka.tracks(),
        timeout=CHANGE_TIMEOUT_S,
        what="the restored share track to be indexed again",
    )
    assert len(kalinka.tracks()) == len(library)


def test_guest_share_is_readable_without_credentials(samba):
    from kalinka_plugin_localfiles.storage.smb import SmbCredentials, SmbStorage

    status = SmbStorage(SmbCredentials()).probe_root_blocking(samba.guest_url)
    assert status.available, status.reason
