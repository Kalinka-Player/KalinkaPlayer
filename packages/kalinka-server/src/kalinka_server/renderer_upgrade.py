"""Upgrading the renderers registered with this Core.

A renderer usually runs on another machine, so the connection it holds here is
often the only way to reach it — most of all when a protocol bump has left it
unable to play, which is exactly when it needs replacing. The Upgrade message
is carried by every protocol version for that reason, and this service sends it
over the raw link rather than the drivable one.

Two rules keep an upgrade from being disruptive: a renderer running a playback
session is not asked unless someone pressed upgrade (it would cut the track
off mid-play), and a renderer that cannot install a release of itself is never
offered one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .renderer_link import RendererLink
from .renderer_registry import RendererRecord, RendererRegistry, RendererUnavailable
from .renderer_replies import PendingReplies
from .update_check import is_newer

logger = logging.getLogger(__name__.split(".")[-1])

DEFAULT_TIMEOUT_S = 10.0


class UpgradeRefused(Exception):
    """The renderer would not take the upgrade on, and said why."""


@dataclass(frozen=True)
class UpgradeCandidate:
    """A registered renderer an installer run here would bring forward."""

    renderer_id: str
    friendly_name: str
    installed_version: str
    busy: bool
    local: bool

    @property
    def name(self) -> str:
        """What a person knows it by."""
        return self.friendly_name or self.renderer_id


@dataclass(frozen=True)
class FleetProgress:
    """Where the renderers stand once :meth:`RendererUpgradeService.bring_forward`
    has been through them."""

    # Renderers that took the upgrade on and are installing it now.
    upgrading: list[str] = field(default_factory=list)
    # Why each of the others is not on its way, in words fit for a person.
    holding: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        """No renderer was behind, so none was asked and none holds back."""
        return not self.upgrading and not self.holding


class RendererUpgradeService:
    """At most one upgrade in flight per renderer, whatever asks for it."""

    def __init__(
        self,
        registry: RendererRegistry,
        is_busy: Callable[[str], bool],
        stop_playback: Callable[[str], Awaitable[None]],
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        """``is_busy`` says whether playback is running on a renderer and
        ``stop_playback`` ends it, telling whoever was playing — all this
        service needs of sessions, so it depends on those rather than on the
        pool that does them."""
        self._registry = registry
        self._is_busy = is_busy
        self._stop_playback = stop_playback
        self._pending = PendingReplies("upgrade", timeout_s)

    def candidates(self, latest_version: Optional[str]) -> list[UpgradeCandidate]:
        """Connected native renderers behind ``latest_version`` that could take
        an upgrade. Empty while no release is known — silence is not a verdict.
        """
        return self._behind(latest_version, can_upgrade=True)

    def stranded(self, latest_version: Optional[str]) -> list[UpgradeCandidate]:
        """The ones behind that cannot install a release of themselves.

        Nothing this Core does brings them forward, so they are reported
        rather than waited for — a flatpak or from-source renderer would
        otherwise hold every other upgrade back for ever.
        """
        return self._behind(latest_version, can_upgrade=False)

    def _behind(
        self, latest_version: Optional[str], *, can_upgrade: bool
    ) -> list[UpgradeCandidate]:
        if not latest_version:
            return []
        found = []
        for record in self._registry.records():
            if record.kind != "native" or record.session is None:
                continue
            if record.upgrade_supported is not can_upgrade:
                continue
            if not version_is_newer(latest_version, record.software_version):
                continue
            found.append(
                UpgradeCandidate(
                    renderer_id=record.renderer_id,
                    friendly_name=record.friendly_name,
                    installed_version=record.software_version,
                    busy=self._is_busy(record.renderer_id),
                    local=record.local,
                )
            )
        return found

    async def bring_forward(
        self,
        latest_version: Optional[str],
        *,
        interrupt: bool = False,
        installer_covers_local: bool = False,
    ) -> FleetProgress:
        """Upgrade every renderer ``latest_version`` would bring forward.

        For a caller with its own upgrade to make: the Core, whose next
        release may move the protocol. A renderer that takes the upgrade on
        installs it whatever the caller does next; one that does not is
        holding, and the caller should look again later rather than move past
        it and leave it behind. They are asked all at once, so the caller
        waits for the slowest answer rather than for the sum of them.

        ``installer_covers_local`` says the caller's own installer run also
        upgrades the renderer on this machine, as it does where the renderer
        package is installed; that renderer is then left to it. Otherwise it
        is asked like any other.

        A renderer that is playing is left for that later look, unless
        ``interrupt`` says someone asked for the upgrade: then its playback
        is stopped first. One that cannot install a release of itself is said
        out loud and not waited for, because no later look would change it.
        """
        def ours(candidate: UpgradeCandidate) -> bool:
            return not (candidate.local and installer_covers_local)

        for stranded in filter(ours, self.stranded(latest_version)):
            logger.warning(
                "Renderer '%s' is on %s and cannot upgrade itself",
                stranded.name,
                stranded.installed_version,
            )
        if latest_version is None:
            return FleetProgress()
        behind = list(filter(ours, self.candidates(latest_version)))
        reasons = await asyncio.gather(
            *(
                self._bring_one(candidate, latest_version, interrupt)
                for candidate in behind
            )
        )
        return FleetProgress(
            upgrading=[
                candidate.name
                for candidate, reason in zip(behind, reasons)
                if reason is None
            ],
            holding=[reason for reason in reasons if reason is not None],
        )

    async def _bring_one(
        self, candidate: UpgradeCandidate, latest_version: str, interrupt: bool
    ) -> Optional[str]:
        """None once the renderer has taken the upgrade on, otherwise why it
        has not, in words fit for a person."""
        name = candidate.name
        if candidate.busy and not interrupt:
            logger.info("Renderer '%s' is playing; leaving its upgrade for later", name)
            return f"{name} is playing"
        try:
            await self.upgrade(
                candidate.renderer_id, latest_version, interrupt=interrupt
            )
        except Exception as e:  # noqa: BLE001 — one bad renderer, not all
            logger.warning("Renderer '%s' did not take the upgrade: %s", name, e)
            if isinstance(e, UpgradeRefused):
                return str(e)
            if isinstance(e, RendererUnavailable):
                return f"{name} is not connected"
            return f"{name} did not answer"
        return None

    async def upgrade(
        self, renderer_id: str, target_version: str, *, interrupt: bool = False
    ) -> str:
        """Ask one renderer to install ``target_version``; returns its detail.

        Playback running on it is stopped first when ``interrupt`` is set and
        refused otherwise. Raises :class:`RendererUnavailable` when it is not
        connected and :class:`UpgradeRefused` when it declines — a session
        running on it, or an install that cannot replace itself.
        """
        record = self._askable(renderer_id)
        name = record.friendly_name or renderer_id
        if self._is_busy(renderer_id):
            if not interrupt:
                raise UpgradeRefused(f"{name} is playing right now")
            logger.info("Stopping playback on renderer '%s' to upgrade it", name)
            # The renderer refuses an upgrade while a session runs on it.
            await self._stop_playback(renderer_id)
            # The stop yields: meanwhile the renderer may have gone, returned
            # as another build, or been asked by another trigger.
            record = self._askable(renderer_id, known_as=name)
            name = record.friendly_name or renderer_id
        link = record.session
        result = await self._pending.request(
            renderer_id,
            link,
            lambda message_id: link.send_upgrade(message_id, target_version),
        )
        if not result.accepted:
            raise UpgradeRefused(
                f"{name} refused: {result.detail}"
                if result.detail
                else f"{name} refused the upgrade"
            )
        logger.info(
            "Renderer '%s' (id=%s) is upgrading to %s",
            record.friendly_name,
            renderer_id,
            target_version or "the latest release",
        )
        return result.detail

    def _askable(
        self, renderer_id: str, known_as: Optional[str] = None
    ) -> RendererRecord:
        """The renderer's record if it may be asked now; raises why not.

        ``known_as`` names it should its record be gone."""
        record = self._registry.get(renderer_id)
        if record is None or record.session is None:
            # Named, not numbered: these reach a person, who knows the
            # renderer by what it calls itself and never by its id.
            name = (record.friendly_name if record else None) or known_as or renderer_id
            raise RendererUnavailable(f"{name} is not connected")
        name = record.friendly_name or renderer_id
        if not record.upgrade_supported:
            raise UpgradeRefused(f"{name} cannot install a release of itself")
        if self._pending.waiting_on(record.session):
            # Two triggers would install twice, and the second would land on a
            # box already restarting into the first.
            raise UpgradeRefused(f"{name} is already upgrading")
        return record

    def handle_reply(
        self, renderer_id: str, link: RendererLink, in_reply_to: int, message
    ) -> None:
        self._pending.handle_reply(renderer_id, link, in_reply_to, message)

    def handle_disconnect(self, renderer_id: str, link: RendererLink) -> None:
        self._pending.handle_disconnect(renderer_id, link)


def version_is_newer(candidate: str, installed: str) -> bool:
    """Whether ``candidate`` is a later release than ``installed``.

    Ordered by the release each version leads to, not by packaging rules: the
    deb ordering :func:`update_check.deb_is_newer` applies does not hold on an
    rpm or flatpak host, and a renderer may run any of the three.
    """
    if not candidate or not installed:
        return False
    left, right = _release_of(candidate), _release_of(installed)
    if left == right:
        # 0.4.0 over the 0.4.0~dev3 that led up to it, never the reverse.
        return "~" in installed and "~" not in candidate
    return is_newer(left, right)


def _release_of(version: str) -> str:
    """The release a version belongs to: ``0.4.0~dev3+g1a2b3c4`` -> ``0.4.0``."""
    return version.split("~")[0].split("+")[0]
