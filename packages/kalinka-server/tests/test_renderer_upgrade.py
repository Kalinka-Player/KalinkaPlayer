"""Upgrading a renderer that usually lives on another machine.

The connection is often the only way to reach it, so the rules that matter
here are the ones that decide whether it is asked at all: never mid-playback
unless someone pressed upgrade, never a build that cannot replace itself, and
— the point of the whole mechanism — even when its protocol no longer matches
this Core's.
"""

import asyncio
import logging

import pytest

from kalinka_server.renderer_proto import renderer_pb2 as pb
from kalinka_server.renderer_registry import RendererRegistry, RendererUnavailable
from kalinka_server.renderer_upgrade import (
    FleetProgress,
    RendererUpgradeService,
    UpgradeRefused,
    version_is_newer,
)


class FakeLink:
    """Answers an upgrade request the way a renderer's session would,
    refusing it, as the renderer does, while playback holds a session."""

    def __init__(
        self,
        accepted: bool = True,
        detail: str = "upgrading",
        renderer_id: str = "rid-1",
    ):
        self.accepted = accepted
        self.detail = detail
        self.requested: list[str] = []
        self.service: RendererUpgradeService | None = None
        self.renderer_id = renderer_id
        self.playing = False
        self.stopped = 0
        self._id = 0

    def next_message_id(self) -> int:
        self._id += 1
        return self._id

    async def stop_playback(self, renderer_id: str) -> None:
        assert renderer_id == self.renderer_id
        self.stopped += 1
        self.playing = False

    async def send_upgrade(self, message_id: int, target_version: str) -> None:
        self.requested.append(target_version)
        if self.playing:
            result = pb.UpgradeResult(
                accepted=False, detail="a playback session is running"
            )
        else:
            result = pb.UpgradeResult(accepted=self.accepted, detail=self.detail)
        assert self.service is not None
        self.service.handle_reply(self.renderer_id, self, message_id, result)


class Fleet:
    """Renderers registered with one Core, each played through its own link."""

    def __init__(self, timeout_s: float = 1.0):
        self.registry = RendererRegistry()
        self.links: dict[str, FakeLink] = {}
        self.service = RendererUpgradeService(
            self.registry,
            lambda renderer_id: self.links[renderer_id].playing,
            lambda renderer_id: self.links[renderer_id].stop_playback(renderer_id),
            timeout_s=timeout_s,
        )

    def add(
        self,
        renderer_id: str,
        name: str,
        *,
        version: str = "0.3.0",
        compatible: bool = True,
        upgrade_supported: bool = True,
        kind: str = "native",
        playing: bool = False,
        local: bool = False,
        link: FakeLink | None = None,
    ) -> FakeLink:
        link = link or FakeLink(renderer_id=renderer_id)
        link.renderer_id = renderer_id
        link.service = self.service
        link.playing = playing
        self.links[renderer_id] = link
        self.registry.register(
            renderer_id=renderer_id,
            instance_id=f"{renderer_id}-inst",
            friendly_name=name,
            software_version=version,
            kind=kind,
            platform={"os": "linux"},
            session=link,
            compatible=compatible,
            upgrade_supported=upgrade_supported,
            local=local,
        )
        return link


def _registry_with(
    *,
    link=None,
    busy: bool = False,
    **renderer,
) -> tuple[RendererRegistry, RendererUpgradeService, FakeLink]:
    fleet = Fleet()
    link = fleet.add("rid-1", "Attic", link=link, playing=busy, **renderer)
    return fleet.registry, fleet.service, link


async def test_the_renderer_is_told_which_release_to_install():
    _, service, link = _registry_with()

    detail = await service.upgrade("rid-1", "0.4.0")

    assert link.requested == ["0.4.0"]
    assert detail == "upgrading"


async def test_a_renderer_we_cannot_drive_can_still_be_upgraded():
    """The whole reason the upgrade rides the raw link: a renderer left behind
    by a protocol bump is exactly the one that has to be replaced."""
    _, service, link = _registry_with(compatible=False)

    await service.upgrade("rid-1", "0.4.0")

    assert link.requested == ["0.4.0"]


async def test_a_playing_renderer_is_left_alone():
    _, service, link = _registry_with(busy=True)

    with pytest.raises(UpgradeRefused):
        await service.upgrade("rid-1", "0.4.0")
    assert link.requested == []
    assert link.stopped == 0


async def test_an_interrupt_stops_playback_before_asking():
    """The renderer refuses while it holds a session, so the order matters:
    asked first, it would answer 'a playback session is running'."""
    _, service, link = _registry_with(busy=True)

    detail = await service.upgrade("rid-1", "0.4.0", interrupt=True)

    assert link.stopped == 1
    assert link.requested == ["0.4.0"]
    assert detail == "upgrading"


async def test_an_interrupt_leaves_an_idle_renderer_alone():
    _, service, link = _registry_with()

    await service.upgrade("rid-1", "0.4.0", interrupt=True)

    assert link.stopped == 0
    assert link.requested == ["0.4.0"]


async def test_a_build_that_cannot_replace_itself_is_never_asked():
    _, service, link = _registry_with(upgrade_supported=False)

    with pytest.raises(UpgradeRefused):
        await service.upgrade("rid-1", "0.4.0")
    assert link.requested == []


async def test_music_is_not_stopped_for_an_upgrade_that_cannot_happen():
    _, service, link = _registry_with(busy=True, upgrade_supported=False)

    with pytest.raises(UpgradeRefused):
        await service.upgrade("rid-1", "0.4.0", interrupt=True)
    assert link.stopped == 0


async def test_a_renderer_gone_while_its_playback_stopped_is_not_asked():
    registry, service, link = _registry_with(busy=True)

    async def stop_and_drop(renderer_id: str) -> None:
        link.playing = False
        registry.disconnect(renderer_id, link, clean=True)

    link.stop_playback = stop_and_drop

    with pytest.raises(RendererUnavailable, match="Attic"):
        await service.upgrade("rid-1", "0.4.0", interrupt=True)
    assert link.requested == []


async def test_a_renderer_back_as_another_build_while_stopping_is_judged_anew():
    """Asked on what was known before the stop, a build that cannot replace
    itself would be sent an upgrade it can only refuse."""
    fleet = Fleet()
    link = fleet.add("rid-1", "Attic", playing=True)

    async def stop_and_return_as_another_build(renderer_id: str) -> None:
        link.playing = False
        fleet.add("rid-1", "Attic", upgrade_supported=False)

    link.stop_playback = stop_and_return_as_another_build

    with pytest.raises(UpgradeRefused, match="cannot install a release of itself"):
        await fleet.service.upgrade("rid-1", "0.4.0", interrupt=True)
    assert link.requested == []
    assert fleet.links["rid-1"].requested == []


async def test_a_refusal_from_the_renderer_is_reported_not_swallowed():
    _, service, _ = _registry_with(link=FakeLink(accepted=False, detail="busy"))

    with pytest.raises(UpgradeRefused, match="busy"):
        await service.upgrade("rid-1", "0.4.0")


async def test_refusals_name_the_renderer_rather_than_its_id():
    """These reach a person, who knows the renderer as 'Attic' and has no way
    to read a uuid off a toast."""
    _, service, _ = _registry_with(upgrade_supported=False)

    with pytest.raises(UpgradeRefused) as refusal:
        await service.upgrade("rid-1", "0.4.0")
    assert "Attic" in str(refusal.value)
    assert "rid-1" not in str(refusal.value)


async def test_an_absent_renderer_cannot_be_upgraded():
    registry, service, _ = _registry_with()
    registry.disconnect("rid-1", registry.get("rid-1").session, clean=True)

    with pytest.raises(RendererUnavailable):
        await service.upgrade("rid-1", "0.4.0")


async def test_an_offline_renderer_without_a_name_is_named_by_its_id():
    """Its record is kept while it may come back, but has nothing in it a
    person could read the refusal by."""
    fleet = Fleet()
    link = fleet.add("rid-1", "")
    fleet.registry.disconnect("rid-1", link, clean=False)

    with pytest.raises(RendererUnavailable, match="^rid-1 is not connected$"):
        await fleet.service.upgrade("rid-1", "0.4.0")
    await fleet.registry.shutdown()


async def test_a_second_request_is_refused_while_one_is_in_flight():
    """Two triggers install twice, the second onto a box already restarting."""

    class SilentLink(FakeLink):
        async def send_upgrade(self, message_id: int, target_version: str) -> None:
            self.requested.append(target_version)

    _, service, link = _registry_with(link=SilentLink())
    first = asyncio.ensure_future(service.upgrade("rid-1", "0.4.0"))
    await asyncio.sleep(0)

    with pytest.raises(UpgradeRefused, match="already upgrading"):
        await service.upgrade("rid-1", "0.4.0")
    assert link.requested == ["0.4.0"]

    service.handle_disconnect("rid-1", link)
    with pytest.raises(RendererUnavailable):
        await first


async def test_an_ask_made_while_playback_stops_is_not_repeated():
    """Ending the session yields, and the renderer is idle from its first
    step, so a second trigger can ask before the press that stopped it."""

    class SlowStopLink(FakeLink):
        def __init__(self) -> None:
            super().__init__()
            self.stop_sent = asyncio.Event()

        async def stop_playback(self, renderer_id: str) -> None:
            await super().stop_playback(renderer_id)
            await self.stop_sent.wait()

        async def send_upgrade(self, message_id: int, target_version: str) -> None:
            self.requested.append(target_version)

    _, service, link = _registry_with(link=SlowStopLink(), busy=True)
    press = asyncio.ensure_future(service.upgrade("rid-1", "0.4.0", interrupt=True))
    await asyncio.sleep(0)
    other = asyncio.ensure_future(service.upgrade("rid-1", "0.4.0"))
    await asyncio.sleep(0)
    link.stop_sent.set()

    with pytest.raises(UpgradeRefused, match="already upgrading"):
        await other
    await asyncio.sleep(0)
    assert link.requested == ["0.4.0"]

    service.handle_disconnect("rid-1", link)
    with pytest.raises(RendererUnavailable):
        await press


async def test_music_is_not_stopped_for_a_renderer_already_upgrading():
    class SilentLink(FakeLink):
        async def send_upgrade(self, message_id: int, target_version: str) -> None:
            self.requested.append(target_version)

    _, service, link = _registry_with(link=SilentLink())
    first = asyncio.ensure_future(service.upgrade("rid-1", "0.4.0"))
    await asyncio.sleep(0)
    link.playing = True

    with pytest.raises(UpgradeRefused, match="already upgrading"):
        await service.upgrade("rid-1", "0.4.0", interrupt=True)
    assert link.stopped == 0

    service.handle_disconnect("rid-1", link)
    with pytest.raises(RendererUnavailable):
        await first


async def test_a_disconnect_mid_request_fails_the_wait():
    class SilentLink(FakeLink):
        async def send_upgrade(self, message_id: int, target_version: str) -> None:
            self.requested.append(target_version)

    registry, service, link = _registry_with(link=SilentLink())
    task = asyncio.ensure_future(service.upgrade("rid-1", "0.4.0"))
    await asyncio.sleep(0)
    service.handle_disconnect("rid-1", link)

    with pytest.raises(RendererUnavailable):
        await task


class TestCandidates:
    def _service(self, **kwargs):
        return _registry_with(**kwargs)[1]

    async def test_a_behind_renderer_is_a_candidate(self):
        service = self._service(version="0.3.0")
        assert [c.renderer_id for c in service.candidates("0.4.0")] == ["rid-1"]

    async def test_a_candidate_says_whether_it_is_playing(self):
        """Listed either way: the Core decides whether to wait for it."""
        service = self._service(version="0.3.0", busy=True)
        assert [c.busy for c in service.candidates("0.4.0")] == [True]

    async def test_a_current_renderer_is_not(self):
        service = self._service(version="0.4.0")
        assert service.candidates("0.4.0") == []

    async def test_a_browser_renderer_is_not(self):
        """It upgrades by reloading the page, not by installing a package."""
        service = self._service(kind="web")
        assert service.candidates("0.4.0") == []

    async def test_no_known_release_means_no_candidates(self):
        """Silence about what is published is not a verdict on what is stale."""
        service = self._service()
        assert service.candidates(None) == []

    async def test_a_renderer_that_cannot_upgrade_itself_is_stranded_not_queued(
        self,
    ):
        """Reported so it can be said out loud, and left out of the work list:
        waiting for it would hold every other upgrade back for ever."""
        service = self._service(version="0.3.0", upgrade_supported=False)
        assert service.candidates("0.4.0") == []
        assert [c.renderer_id for c in service.stranded("0.4.0")] == ["rid-1"]

    async def test_one_that_can_upgrade_is_not_stranded(self):
        service = self._service(version="0.3.0")
        assert service.stranded("0.4.0") == []

    async def test_this_machines_renderer_is_a_candidate_that_says_so(self):
        """Still offered on its own; only the Core's own upgrade passes it by."""
        service = self._service(version="0.3.0", local=True)
        assert [c.local for c in service.candidates("0.4.0")] == [True]


class TestVersionOrdering:
    """Judged the same for any packaging: the deb rules do not apply to an rpm
    host, and both sides carry plain releases."""

    def test_a_later_release_wins(self):
        assert version_is_newer("0.4.0", "0.3.0")
        assert version_is_newer("0.10.0", "0.9.0")

    def test_the_same_release_is_not_newer(self):
        assert not version_is_newer("0.4.0", "0.4.0")

    def test_an_earlier_release_is_not_newer(self):
        assert not version_is_newer("0.3.0", "0.4.0")

    def test_a_release_outranks_the_development_build_leading_to_it(self):
        assert version_is_newer("0.4.0", "0.4.0~dev3+g1a2b3c4")

    def test_an_unknown_version_is_never_ranked(self):
        assert not version_is_newer("0.4.0", "")
        assert not version_is_newer("", "0.3.0")


class TestBringForward:
    """The Core asks this before upgrading itself, so 'nothing left to do' is
    the only answer that lets it move unattended. A renderer that took the
    upgrade on is on its way; one that did not is said in words a person can
    act on."""

    async def test_a_renderer_that_is_current_needs_nothing(self):
        _, service, link = _registry_with(version="0.4.0")
        assert (await service.bring_forward("0.4.0")).done
        assert link.requested == []

    async def test_a_behind_renderer_is_upgraded_and_the_caller_waits(self):
        _, service, link = _registry_with(version="0.3.0")

        progress = await service.bring_forward("0.4.0")

        assert progress == FleetProgress(upgrading=["Attic"])
        assert not progress.done
        assert link.requested == ["0.4.0"]

    async def test_a_playing_renderer_is_left_for_later(self, caplog):
        """The unattended path: nobody asked, so nobody's music stops."""
        _, service, link = _registry_with(version="0.3.0", busy=True)

        with caplog.at_level(logging.INFO, logger="renderer_upgrade"):
            progress = await service.bring_forward("0.4.0")

        assert progress == FleetProgress(holding=["Attic is playing"])
        assert link.requested == []
        assert link.stopped == 0
        assert "Renderer 'Attic' is playing; leaving its upgrade for later" in (
            caplog.messages
        )

    async def test_a_press_stops_a_playing_renderer_and_upgrades_it(self):
        _, service, link = _registry_with(version="0.3.0", busy=True)

        progress = await service.bring_forward("0.4.0", interrupt=True)

        assert link.stopped == 1
        assert link.requested == ["0.4.0"]
        assert progress == FleetProgress(upgrading=["Attic"])

    async def test_one_that_cannot_upgrade_itself_is_not_waited_for(self):
        """No later attempt would change it, so holding the Core back for ever
        buys nothing."""
        _, service, link = _registry_with(version="0.3.0", upgrade_supported=False)
        assert (await service.bring_forward("0.4.0")).done
        assert link.requested == []

    async def test_one_that_cannot_upgrade_itself_is_said_out_loud(self, caplog):
        fleet = Fleet()
        fleet.add("rid-1", "", upgrade_supported=False)

        with caplog.at_level(logging.WARNING, logger="renderer_upgrade"):
            await fleet.service.bring_forward("0.4.0")

        assert caplog.messages == [
            "Renderer 'rid-1' is on 0.3.0 and cannot upgrade itself"
        ]

    async def test_this_machines_renderer_left_to_the_installer_is_not_stranded(
        self, caplog
    ):
        """The installer brings it forward whatever it can do itself, so a
        warning every hour would cry wolf."""
        fleet = Fleet()
        fleet.add("rid-here", "Living room", local=True, upgrade_supported=False)

        with caplog.at_level(logging.WARNING, logger="renderer_upgrade"):
            progress = await fleet.service.bring_forward(
                "0.4.0", installer_covers_local=True
            )

        assert progress.done
        assert caplog.messages == []

    async def test_a_refusal_is_named_with_its_reason(self):
        _, service, _ = _registry_with(
            version="0.3.0", link=FakeLink(accepted=False, detail="disk full")
        )
        progress = await service.bring_forward("0.4.0", interrupt=True)
        assert progress == FleetProgress(holding=["Attic refused: disk full"])

    async def test_a_silent_renderer_is_named_without_its_id(self):
        class SilentLink(FakeLink):
            async def send_upgrade(self, message_id, target_version):
                self.requested.append(target_version)

        fleet = Fleet(timeout_s=0.05)
        fleet.add("rid-1", "Attic", link=SilentLink())

        progress = await fleet.service.bring_forward("0.4.0")
        assert progress == FleetProgress(holding=["Attic did not answer"])

    async def test_one_gone_while_its_playback_stopped_is_named_as_gone(self):
        registry, service, link = _registry_with(version="0.3.0", busy=True)

        async def stop_and_drop(renderer_id: str) -> None:
            link.playing = False
            registry.disconnect(renderer_id, link, clean=True)

        link.stop_playback = stop_and_drop

        progress = await service.bring_forward("0.4.0", interrupt=True)
        assert progress == FleetProgress(holding=["Attic is not connected"])

    async def test_one_still_answering_an_earlier_ask_holds_the_press(self):
        """It may yet refuse, so the Core cannot count on it being on its way."""

        class SilentLink(FakeLink):
            async def send_upgrade(self, message_id, target_version):
                self.requested.append(target_version)

        fleet = Fleet()
        link = fleet.add("rid-1", "Attic", link=SilentLink())
        earlier = asyncio.ensure_future(fleet.service.upgrade("rid-1", "0.4.0"))
        await asyncio.sleep(0)

        progress = await fleet.service.bring_forward("0.4.0", interrupt=True)

        assert progress == FleetProgress(holding=["Attic is already upgrading"])
        assert link.requested == ["0.4.0"]
        fleet.service.handle_disconnect("rid-1", link)
        with pytest.raises(RendererUnavailable):
            await earlier

    async def test_nothing_is_done_while_no_release_is_known(self):
        _, service, link = _registry_with(version="0.3.0")
        assert (await service.bring_forward(None)).done
        assert link.requested == []

    @pytest.mark.parametrize("interrupt", [False, True])
    async def test_this_machines_renderer_is_left_to_the_installer(self, interrupt):
        """The installer the Core fires upgrades the packaged renderer beside
        it, so waiting on it — or stopping its music early — gains nothing."""
        fleet = Fleet()
        here = fleet.add("rid-here", "Living room", local=True, playing=True)

        progress = await fleet.service.bring_forward(
            "0.4.0", interrupt=interrupt, installer_covers_local=True
        )

        assert progress.done
        assert here.requested == []
        assert here.stopped == 0

    async def test_this_machines_renderer_installed_otherwise_is_asked(self):
        """No installer run here touches it, so nothing else brings it on."""
        fleet = Fleet()
        here = fleet.add("rid-here", "Living room", local=True, playing=True)

        progress = await fleet.service.bring_forward("0.4.0", interrupt=True)

        assert progress == FleetProgress(upgrading=["Living room"])
        assert here.stopped == 1
        assert here.requested == ["0.4.0"]

    async def test_only_the_renderers_elsewhere_are_asked(self):
        fleet = Fleet()
        here = fleet.add("rid-here", "Living room", local=True)
        there = fleet.add("rid-there", "Attic", playing=True)

        progress = await fleet.service.bring_forward(
            "0.4.0", interrupt=True, installer_covers_local=True
        )

        assert progress == FleetProgress(upgrading=["Attic"])
        assert here.requested == []
        assert there.requested == ["0.4.0"]

    async def test_silent_renderers_cost_one_wait_not_one_each(self):
        """The press waits on this, so three silent renderers must not keep
        it three timeouts long."""

        class SilentLink(FakeLink):
            async def send_upgrade(self, message_id, target_version):
                self.requested.append(target_version)

        fleet = Fleet(timeout_s=0.3)
        for name in ("Attic", "Kitchen", "Study"):
            fleet.add(f"rid-{name}", name, link=SilentLink())
        loop = asyncio.get_running_loop()

        started = loop.time()
        progress = await fleet.service.bring_forward("0.4.0", interrupt=True)

        assert loop.time() - started < 0.6
        assert len(progress.holding) == 3

    async def test_answers_are_listed_in_the_renderers_order_not_by_speed(self):
        class SilentLink(FakeLink):
            async def send_upgrade(self, message_id, target_version):
                self.requested.append(target_version)

        fleet = Fleet(timeout_s=0.05)
        fleet.add("rid-1", "Attic", link=SilentLink())
        fleet.add("rid-2", "Kitchen", link=FakeLink(accepted=False, detail="full"))
        fleet.add("rid-3", "Study")
        fleet.add("rid-4", "Den")

        progress = await fleet.service.bring_forward("0.4.0")

        assert progress == FleetProgress(
            upgrading=["Study", "Den"],
            holding=["Attic did not answer", "Kitchen refused: full"],
        )


async def test_acceptance_blocks_repeated_asks_until_the_target_is_confirmed():
    fleet = Fleet()
    original = fleet.add("rid-1", "Attic")
    await fleet.service.upgrade("rid-1", "0.4.0")

    with pytest.raises(UpgradeRefused, match="already upgrading"):
        await fleet.service.upgrade("rid-1", "0.4.0", interrupt=True)
    assert await fleet.service.bring_forward("0.4.0") == FleetProgress(
        upgrading=["Attic"]
    )
    assert original.requested == ["0.4.0"]

    # Reconnecting on the old build is not success, nor permission to ask again.
    old = fleet.add("rid-1", "Attic", version="0.3.0")
    assert fleet.service.is_upgrading("rid-1")
    assert not (await fleet.service.bring_forward("0.4.0")).done
    assert old.requested == []

    fleet.add("rid-1", "Attic", version="0.4.0")
    assert not fleet.service.is_upgrading("rid-1")
    assert (await fleet.service.bring_forward("0.4.0")).done


@pytest.mark.parametrize("clean", [False, True])
async def test_a_disconnected_accepted_renderer_still_holds_the_server(clean):
    fleet = Fleet()
    link = fleet.add("rid-1", "Attic")
    await fleet.service.upgrade("rid-1", "0.4.0")
    fleet.registry.disconnect("rid-1", link, clean=clean)
    fleet.service.handle_disconnect("rid-1", link)
    assert await fleet.service.bring_forward("0.4.0") == FleetProgress(
        upgrading=["Attic"]
    )
    await fleet.registry.shutdown()


async def test_an_install_that_never_restarts_can_be_retried(monkeypatch):
    from types import SimpleNamespace
    from kalinka_server import renderer_upgrade

    clock = [100.0]
    monkeypatch.setattr(
        renderer_upgrade, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    fleet = Fleet()
    link = fleet.add("rid-1", "Attic")
    await fleet.service.upgrade("rid-1", "0.4.0")
    clock[0] += renderer_upgrade.INSTALL_TIMEOUT_S + 1
    assert not fleet.service.is_upgrading("rid-1")
    assert await fleet.service.bring_forward("0.4.0", interrupt=True) == FleetProgress(
        upgrading=["Attic"]
    )
    assert link.requested == ["0.4.0", "0.4.0"]


async def test_expiring_a_disconnected_install_does_not_release_the_server(monkeypatch):
    from types import SimpleNamespace
    from kalinka_server import renderer_upgrade

    clock = [100.0]
    monkeypatch.setattr(
        renderer_upgrade, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    fleet = Fleet()
    link = fleet.add("rid-1", "Attic")
    await fleet.service.upgrade("rid-1", "0.4.0")
    fleet.registry.disconnect("rid-1", link, clean=True)
    clock[0] += renderer_upgrade.INSTALL_TIMEOUT_S + 1
    progress = await fleet.service.bring_forward("0.4.0", interrupt=True)
    assert not progress.done
    assert "Attic did not return" in progress.holding[0]


async def test_a_failed_request_releases_the_playback_guard():
    _, service, _ = _registry_with(link=FakeLink(accepted=False))
    with pytest.raises(UpgradeRefused):
        await service.upgrade("rid-1", "0.4.0")
    assert not service.is_upgrading("rid-1")


async def test_a_request_in_flight_still_blocks_core_if_the_registry_loses_it():
    stopping = asyncio.Event()
    proceed = asyncio.Event()
    fleet = Fleet()
    link = fleet.add("rid-1", "Attic", playing=True)

    async def slow_stop(renderer_id):
        link.playing = False
        stopping.set()
        await proceed.wait()

    link.stop_playback = slow_stop
    request = asyncio.create_task(
        fleet.service.upgrade("rid-1", "0.4.0", interrupt=True)
    )
    await asyncio.wait_for(stopping.wait(), 1)
    fleet.registry.disconnect("rid-1", link, clean=True)
    try:
        assert not (await fleet.service.bring_forward("0.4.0")).done
    finally:
        proceed.set()
    with pytest.raises(RendererUnavailable):
        await request


async def test_reinstalling_the_current_version_waits_for_a_new_registration():
    fleet = Fleet()
    link = fleet.add("rid-1", "Attic", version="0.4.0")
    await fleet.service.upgrade("rid-1", "0.4.0")
    assert fleet.service.is_upgrading("rid-1")
    with pytest.raises(UpgradeRefused, match="already upgrading"):
        await fleet.service.upgrade("rid-1", "0.4.0")
    assert link.requested == ["0.4.0"]
    fleet.add("rid-1", "Attic", version="0.4.0")
    assert not fleet.service.is_upgrading("rid-1")


async def test_a_confirmation_during_another_renderers_reply_is_not_a_failure():
    fleet = Fleet()
    fleet.add("rid-1", "Attic")
    await fleet.service.upgrade("rid-1", "0.4.0")

    class ConfirmsAttic(FakeLink):
        async def send_upgrade(self, message_id, target_version):
            fleet.add("rid-1", "Attic", version="0.4.0")
            await super().send_upgrade(message_id, target_version)

    fleet.add("rid-2", "Kitchen", link=ConfirmsAttic())
    assert await fleet.service.bring_forward("0.4.0") == FleetProgress(
        upgrading=["Kitchen"]
    )
