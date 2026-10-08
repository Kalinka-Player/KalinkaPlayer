"""RendererRegistry lifecycle: identity by id, reconnect vs restart, reap."""

import asyncio

import pytest
from kalinka_plugin_sdk.direct_playback import OutputCapabilities

from kalinka_server.renderer_prefs import RendererPreferences
from kalinka_server.renderer_registry import (
    RegistrationKind,
    RendererRegistry,
    RendererStatus,
    RendererUnavailable,
)


def _register(
    registry, session, instance_id="inst-1", renderer_id="rid-1", server_addr=None
):
    return registry.register(
        renderer_id=renderer_id,
        instance_id=instance_id,
        friendly_name="Test Renderer",
        software_version="0.1.0",
        kind="native",
        platform={"os": "linux"},
        session=session,
        server_addr=server_addr,
    )


async def test_new_registration_listed_as_connected():
    registry = RendererRegistry()
    session = object()
    assert _register(registry, session) == RegistrationKind.NEW
    (entry,) = registry.list()
    assert entry["renderer_id"] == "rid-1"
    assert entry["status"] == "connected"


async def test_the_dialed_server_address_rides_each_registration():
    """The speaker test forms URLs from it, so a renderer that comes back via
    a different interface must overwrite the old address."""
    registry = RendererRegistry()
    _register(registry, object(), server_addr=("192.168.50.85", 8000))
    assert registry.get("rid-1").server_addr == ("192.168.50.85", 8000)

    _register(registry, object(), server_addr=("10.0.0.7", 8000))
    assert registry.get("rid-1").server_addr == ("10.0.0.7", 8000)


async def test_unclean_disconnect_goes_offline_then_reaped():
    registry = RendererRegistry(offline_timeout_s=0.05)
    session = object()
    _register(registry, session)
    registry.disconnect("rid-1", session, clean=False)
    (entry,) = registry.list()
    assert entry["status"] == "offline"
    await asyncio.sleep(0.1)
    assert registry.list() == []


async def test_clean_goodbye_removed_immediately():
    registry = RendererRegistry()
    session = object()
    _register(registry, session)
    registry.disconnect("rid-1", session, clean=True)
    assert registry.list() == []


async def test_reconnect_same_instance_cancels_reap_and_keeps_connected_at():
    registry = RendererRegistry(offline_timeout_s=0.05)
    first = object()
    _register(registry, first)
    original_connected_at = registry.get("rid-1").connected_at
    registry.disconnect("rid-1", first, clean=False)

    second = object()
    kind = _register(registry, second)  # same instance_id
    assert kind == RegistrationKind.RECONNECT
    record = registry.get("rid-1")
    assert record.status == RendererStatus.CONNECTED
    assert record.connected_at == original_connected_at
    # The reap scheduled at disconnect must not fire after the reconnect.
    await asyncio.sleep(0.1)
    assert registry.get("rid-1") is not None


async def test_new_instance_id_is_a_restart():
    registry = RendererRegistry(offline_timeout_s=0.05)
    first = object()
    _register(registry, first, instance_id="inst-1")
    registry.disconnect("rid-1", first, clean=False)
    kind = _register(registry, object(), instance_id="inst-2")
    assert kind == RegistrationKind.RESTART
    assert registry.get("rid-1").instance_id == "inst-2"


async def test_identity_is_id_not_name():
    """A different name with the same renderer_id is the same renderer."""
    registry = RendererRegistry()
    first = object()
    _register(registry, first)
    registry.disconnect("rid-1", first, clean=False)
    kind = registry.register(
        renderer_id="rid-1",
        instance_id="inst-1",
        friendly_name="Renamed Renderer",
        software_version="0.1.0",
        kind="native",
        platform={"os": "linux"},
        session=object(),
    )
    assert kind == RegistrationKind.RECONNECT
    (entry,) = registry.list()
    assert entry["friendly_name"] == "Renamed Renderer"


async def test_live_session_replaced_and_stale_disconnect_ignored():
    replaced = []

    async def on_replace(session):
        replaced.append(session)

    registry = RendererRegistry(replace_session=on_replace)
    old = object()
    _register(registry, old)

    new = object()
    kind = _register(registry, new, instance_id="inst-2")
    assert kind == RegistrationKind.RESTART
    await asyncio.sleep(0)  # let the replace task run
    assert replaced == [old]

    # The retired session's disconnect must not clobber the new record.
    registry.disconnect("rid-1", old, clean=False)
    record = registry.get("rid-1")
    assert record.status == RendererStatus.CONNECTED
    assert record.session is new


async def test_selection_holds_through_an_offline_spell():
    """A hiccup or an upgrade must fail playback, not move it to another room."""
    registry = RendererRegistry(offline_timeout_s=60)
    a, b = object(), object()
    _register(registry, a, renderer_id="rid-a")
    _register(registry, b, renderer_id="rid-b")
    assert registry.active_id() == "rid-a"  # automatic: first connected

    registry.select("rid-b")
    assert registry.active_id() == "rid-b"
    entries = {e["renderer_id"]: e for e in registry.list()}
    assert entries["rid-b"]["active"] and entries["rid-b"]["selected"]
    assert not entries["rid-a"]["active"] and not entries["rid-a"]["selected"]

    registry.disconnect("rid-b", b, clean=False)
    assert registry.active_id() == "rid-b"
    with pytest.raises(RendererUnavailable):
        registry.require_session("rid-b")
    _register(registry, object(), renderer_id="rid-b")
    assert registry.active_id() == "rid-b"

    registry.select(None)
    assert registry.active_id() == "rid-a"
    await registry.shutdown()


async def test_with_nothing_selected_playback_moves_off_an_offline_renderer():
    registry = RendererRegistry(offline_timeout_s=60)
    a = object()
    _register(registry, a, renderer_id="rid-a")
    _register(registry, object(), renderer_id="rid-b")

    registry.disconnect("rid-a", a, clean=False)

    assert registry.active_id() == "rid-b"
    await registry.shutdown()


async def test_a_selected_renderer_that_says_goodbye_stays_listed_offline():
    """An upgrading renderer leaves cleanly; it is still where the user wants
    the music."""
    removed = []
    registry = RendererRegistry(offline_timeout_s=60)
    registry.set_on_removed(lambda rid, clean: removed.append((rid, clean)))
    link = object()
    _register(registry, link)
    _register(registry, object(), renderer_id="rid-other")
    registry.select("rid-1")

    registry.disconnect("rid-1", link, clean=True)

    assert removed == [("rid-1", True)]
    assert registry.get("rid-1").status == RendererStatus.OFFLINE
    assert registry.active_id() == "rid-1"
    assert _register(registry, object(), instance_id="inst-2") == (
        RegistrationKind.RESTART
    )
    await registry.shutdown()


async def test_a_selected_renderer_outlives_its_reap_but_its_session_does_not():
    removed = []
    registry = RendererRegistry(offline_timeout_s=0.05)
    registry.set_on_removed(lambda rid, clean: removed.append((rid, clean)))
    link = object()
    _register(registry, link)
    registry.select("rid-1")
    registry.disconnect("rid-1", link, clean=False)

    await asyncio.sleep(0.1)

    assert removed == [("rid-1", False)]
    (entry,) = registry.list()
    assert entry["status"] == "offline"
    assert entry["active"] and entry["selected"]


async def test_a_restored_selection_is_listed_offline_under_its_last_name():
    """After a server restart the pinned renderer may not be back yet; playback
    must fail on it by name, not land elsewhere or on a bare id."""
    prefs = RendererPreferences()
    prefs.set_selected("rid-1", "Living Room")
    registry = RendererRegistry(prefs=prefs)
    _register(registry, object(), renderer_id="rid-other")

    entries = {e["renderer_id"]: e for e in registry.list()}
    assert entries["rid-1"]["status"] == "offline"
    assert entries["rid-1"]["friendly_name"] == "Living Room"
    assert entries["rid-1"]["active"] and entries["rid-1"]["selected"]
    with pytest.raises(RendererUnavailable, match="^Living Room is not connected$"):
        registry.require_session("rid-1")

    assert _register(registry, object()) == RegistrationKind.RESTART
    assert registry.get("rid-1").status == RendererStatus.CONNECTED
    assert registry.active_id() == "rid-1"
    await registry.shutdown()


async def test_a_selection_never_heard_by_name_falls_back():
    """A pin saved before names were kept has nothing to list it by."""
    prefs = RendererPreferences()
    prefs.set_selected("rid-1")
    registry = RendererRegistry(prefs=prefs)
    _register(registry, object(), renderer_id="rid-other")

    assert registry.get("rid-1") is None
    assert registry.active_id() == "rid-other"

    _register(registry, object())
    assert registry.active_id() == "rid-1"
    assert prefs.selected_renderer_name == "Test Renderer"
    await registry.shutdown()


async def test_the_selected_renderer_s_name_is_kept_current():
    prefs = RendererPreferences()
    registry = RendererRegistry(prefs=prefs)
    _register(registry, object())

    registry.select("rid-1")
    assert prefs.selected_renderer_name == "Test Renderer"

    registry.register(
        renderer_id="rid-1",
        instance_id="inst-1",
        friendly_name="Kitchen",
        software_version="0.1.0",
        kind="native",
        platform={},
        session=object(),
    )
    assert prefs.selected_renderer_name == "Kitchen"

    registry.select(None)
    assert prefs.selected_renderer_name is None
    await registry.shutdown()


async def test_selecting_elsewhere_drops_a_renderer_kept_only_for_the_pin():
    registry = RendererRegistry(offline_timeout_s=60)
    link = object()
    _register(registry, link)
    _register(registry, object(), renderer_id="rid-other")
    registry.select("rid-1")
    registry.disconnect("rid-1", link, clean=True)
    events = _wire(registry)

    registry.select("rid-other")

    assert registry.get("rid-1") is None
    assert [e[0] for e in events] == ["renderers", "current"]
    assert [row.renderer_id for row in events[0][1]] == ["rid-other"]
    await registry.shutdown()


async def test_selecting_elsewhere_leaves_a_pending_reap_to_run_its_course():
    registry = RendererRegistry(offline_timeout_s=0.05)
    link = object()
    _register(registry, link)
    _register(registry, object(), renderer_id="rid-other")
    registry.select("rid-1")
    registry.disconnect("rid-1", link, clean=False)

    registry.select("rid-other")
    assert registry.get("rid-1").status == RendererStatus.OFFLINE

    await asyncio.sleep(0.1)
    assert registry.get("rid-1") is None


async def test_resolve_active_answers_without_committing_the_choice():
    """The selection endpoint stops playback before pinning, so it needs to
    know where the choice leads while the old one is still in force."""
    registry = RendererRegistry(offline_timeout_s=60)
    _register(registry, object(), renderer_id="rid-a")
    _register(registry, object(), renderer_id="rid-b")

    assert registry.resolve_active("rid-b") == "rid-b"
    assert registry.resolve_active(None) == "rid-a"  # automatic
    assert registry.resolve_active("rid-gone") == "rid-a"  # unlisted: fall back
    assert registry.active_id() == "rid-a"  # nothing was pinned
    await registry.shutdown()


async def test_two_renderers_are_independent():
    registry = RendererRegistry(offline_timeout_s=0.05)
    a, b = object(), object()
    _register(registry, a, renderer_id="rid-a")
    _register(registry, b, renderer_id="rid-b")
    registry.disconnect("rid-a", a, clean=False)
    await asyncio.sleep(0.1)
    ids = [e["renderer_id"] for e in registry.list()]
    assert ids == ["rid-b"]


def _wire(registry):
    """Subscribe and drop the seed report, leaving only what follows."""
    events = _subscribe(registry)
    events.clear()
    return events


def _subscribe(registry):
    events = []
    registry.set_on_changed(
        renderers=lambda rows: events.append(("renderers", rows)),
        current=lambda active, selected: events.append(
            ("current", active, selected)
        ),
    )
    return events


async def test_register_emits_rows_and_current():
    registry = RendererRegistry()
    events = _wire(registry)
    _register(registry, object())

    kinds = [e[0] for e in events]
    assert kinds == ["renderers", "current"]
    (row,) = events[0][1]
    assert row.renderer_id == "rid-1"
    assert row.status == "connected"
    assert events[1][1:] == ("rid-1", None)


async def test_select_emits_current_only():
    registry = RendererRegistry()
    _register(registry, object())
    events = _wire(registry)

    registry.select("rid-1")

    assert events == [("current", "rid-1", "rid-1")]


async def test_unclean_disconnect_flips_row_and_moves_current():
    registry = RendererRegistry(offline_timeout_s=60)
    a, b = object(), object()
    _register(registry, a, renderer_id="rid-a")
    _register(registry, b, renderer_id="rid-b")
    events = _wire(registry)

    registry.disconnect("rid-a", a, clean=False)

    rows = dict((r.renderer_id, r.status) for r in events[0][1])
    assert rows == {"rid-a": "offline", "rid-b": "connected"}
    assert ("current", "rid-b", None) in events
    await registry.shutdown()


async def test_reap_emits_removal():
    registry = RendererRegistry(offline_timeout_s=0.05)
    session = object()
    _register(registry, session)
    registry.disconnect("rid-1", session, clean=False)
    events = _wire(registry)

    await asyncio.sleep(0.1)

    # Current moved when the renderer went offline; the reap only drops the row.
    assert events == [("renderers", [])]


async def test_descriptor_rows_carry_no_selection_flags():
    """Which renderer is current is CurrentRendererChanged's fact alone; rows
    repeating it would let the two events contradict each other."""
    registry = RendererRegistry()
    events = _wire(registry)
    _register(registry, object())

    (row,) = events[0][1]
    assert "active" not in row.model_dump()
    assert "selected" not in row.model_dump()


async def test_resubscribing_reports_to_the_new_callback_too():
    """A replacement subscriber knows nothing of what the last one was told,
    so an unchanged pair must still be reported to it."""
    registry = RendererRegistry()
    _register(registry, object())
    _subscribe(registry)

    events = _subscribe(registry)

    assert events == [("renderers", events[0][1]), ("current", "rid-1", None)]


async def test_subscribing_reports_the_picture_at_once():
    registry = RendererRegistry()
    registry.select("rid-later")  # restored-from-prefs pin, nothing connected

    events = _subscribe(registry)

    assert events == [("renderers", []), ("current", None, "rid-later")]


async def test_incompatible_renderer_is_listed_but_never_played_to():
    """A renderer whose protocol this Core cannot speak stays visible — that
    listing is the only handle anyone has on it — but playback goes elsewhere."""
    registry = RendererRegistry()
    stale = object()
    registry.register(
        renderer_id="old-rid",
        instance_id="inst-old",
        friendly_name="Old Renderer",
        software_version="0.3.0",
        kind="native",
        platform={"os": "linux"},
        session=stale,
        compatible=False,
    )
    (entry,) = registry.list()
    assert entry["status"] == "connected"
    assert entry["compatible"] is False
    assert registry.active_id() is None

    _register(registry, object(), renderer_id="new-rid", instance_id="inst-new")
    assert registry.active_id() == "new-rid"


async def test_a_selected_renderer_that_turns_incompatible_keeps_the_pin():
    """Upgraded past what this Core speaks: playback fails there instead of
    moving to another room."""
    registry = RendererRegistry()
    _register(registry, object(), renderer_id="new-rid", instance_id="inst-new")
    _register(registry, object(), renderer_id="old-rid", instance_id="inst-old")
    registry.select("old-rid")

    registry.register(
        renderer_id="old-rid",
        instance_id="inst-old2",
        friendly_name="Old Renderer",
        software_version="0.3.0",
        kind="native",
        platform={"os": "linux"},
        session=object(),
        compatible=False,
    )

    assert registry.active_id() == "old-rid"
    with pytest.raises(RendererUnavailable):
        registry.require_session("old-rid")


async def test_driving_an_incompatible_renderer_is_refused_not_left_hanging():
    registry = RendererRegistry()
    link = object()
    registry.register(
        renderer_id="old-rid",
        instance_id="inst-old",
        friendly_name="Old Renderer",
        software_version="0.3.0",
        kind="native",
        platform={"os": "linux"},
        session=link,
        compatible=False,
    )
    # The link itself stays reachable — it is what an upgrade would ride.
    assert registry.live_session("old-rid") is link
    with pytest.raises(RendererUnavailable):
        registry.require_session("old-rid")


async def test_a_renderer_returning_mid_playback_does_not_take_the_audio_back():
    """The pinned renderer drops mid-track and playback is moved to another
    one; then the pinned renderer comes back. Resolution prefers the pin
    again, but the audio is elsewhere — saying otherwise leaves every client
    naming a renderer that is silent."""
    registry = RendererRegistry(offline_timeout_s=60)
    web = object()
    _register(registry, object(), renderer_id="rid-hifi")
    _register(registry, web, renderer_id="rid-web", instance_id="inst-web")
    registry.select("rid-web")
    registry.session_claimed("rid-web")
    assert registry.active_id() == "rid-web"

    # Link dropped, session held for its return: playback has gone nowhere.
    registry.disconnect("rid-web", web, clean=False)
    assert registry.active_id() == "rid-web"

    # The track is moved to the other renderer.
    registry.session_released("rid-web")
    registry.session_claimed("rid-hifi")
    assert registry.active_id() == "rid-hifi"

    # The tab returns and is playable again — but it is not what is playing.
    _register(registry, object(), renderer_id="rid-web", instance_id="inst-web2")
    assert registry.active_id() == "rid-hifi"
    entries = {e["renderer_id"]: e for e in registry.list()}
    assert entries["rid-hifi"]["active"]
    assert entries["rid-web"]["selected"] and not entries["rid-web"]["active"]

    # Playback over, the pin decides again.
    registry.session_released("rid-hifi")
    assert registry.active_id() == "rid-web"
    await registry.shutdown()


async def test_a_late_release_does_not_clear_the_claim_that_replaced_it():
    """Switching claims the new renderer before giving up the old, so the old
    session's release arrives after the new one is already the answer."""
    registry = RendererRegistry()
    _register(registry, object(), renderer_id="rid-a")
    _register(registry, object(), renderer_id="rid-b")

    registry.session_claimed("rid-a")
    registry.session_claimed("rid-b")
    registry.session_released("rid-a")
    assert registry.active_id() == "rid-b"
    await registry.shutdown()


async def test_claiming_and_releasing_a_session_tell_clients():
    registry = RendererRegistry()
    _register(registry, object(), renderer_id="rid-a")
    _register(registry, object(), renderer_id="rid-b")
    registry.select("rid-a")
    events = _wire(registry)

    registry.session_claimed("rid-b")
    assert events == [("current", "rid-b", "rid-a")]
    events.clear()

    registry.session_released("rid-b")
    assert events == [("current", "rid-a", "rid-a")]
    await registry.shutdown()


async def test_an_observer_hears_of_a_reconnect_clients_are_not_told_of():
    registry = RendererRegistry()
    _register(registry, object())
    events = _wire(registry)
    links = []
    registry.add_observer(lambda: links.append(registry.live_session("rid-1")))
    replacement = object()

    _register(registry, replacement)

    assert [e[0] for e in events] == ["renderers"]
    assert links == [replacement]


async def test_an_observer_hears_of_every_move_of_playback():
    registry = RendererRegistry(offline_timeout_s=60)
    a = object()
    _register(registry, a, renderer_id="rid-a")
    _register(registry, object(), renderer_id="rid-b")
    moves = []
    registry.add_observer(lambda: moves.append(registry.active_id()))

    registry.select("rid-b")
    registry.session_claimed("rid-a")
    registry.session_released("rid-a")
    registry.disconnect("rid-a", a, clean=True)

    assert moves == ["rid-b", "rid-a", "rid-b", "rid-b"]
    await registry.shutdown()


async def test_what_a_renderer_says_it_plays_is_kept_and_observed():
    registry = RendererRegistry()
    link = object()
    registry.register(
        renderer_id="rid-1",
        instance_id="inst-1",
        friendly_name="Test Renderer",
        software_version="0.1.0",
        kind="native",
        platform={},
        session=link,
        capabilities=OutputCapabilities(dsd=False),
    )
    told = []
    registry.add_observer(lambda: told.append(registry.get("rid-1").capabilities))

    registry.update_capabilities("rid-1", link, OutputCapabilities(dsd=True))
    registry.update_capabilities("rid-1", object(), OutputCapabilities(dsd=False))
    registry.update_capabilities("nobody", link, OutputCapabilities(dsd=False))

    assert told == [OutputCapabilities(dsd=True)]
