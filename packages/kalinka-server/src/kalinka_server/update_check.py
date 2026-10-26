"""Background check for a newer Kalinka release.

A background task (started from the app lifecycle) reads the public
``releases.atom`` feeds of this repo and the app's once an hour — no token,
not subject to the GitHub API rate limit. ``GET /server/update`` serves the
cached result only and never does network I/O. With ``server.auto_upgrade``
on, the task also fires the upgrade itself during quiet hours while playback
is stopped.

Four release trains are watched, because one installer run covers all four:
the ``kalinka-v*`` app bundle, the ``kalinka-renderer-v*`` renderer package
and ``kalinka-supervisor-v*`` supervisor installed on this machine, and the
app repo's ``v*`` releases, which carry the browser player.
Renderers on other boxes are upgraded by running the installer there.

``PUT /server/upgrade`` must name the version the client is upgrading to;
it is rejected unless that matches the cached latest release, so a stale
banner can't fire an upgrade to a version nobody offered. The upgrade
itself is performed by root-side systemd units shipped in the deb —
kalinka-upgrade.path watches a trigger file the kalusr server touches and
runs upgrade.sh, which fetches the current published installer from
kalinkaplayer.com. This module only detects whether that machinery is
installed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from packaging.version import InvalidVersion, Version

from kalinka_plugin_sdk import paths

from .version import get_version

logger = logging.getLogger(__name__)

_BUNDLE_TAG_PREFIX = "kalinka-v"
_RENDERER_TAG_PREFIX = "kalinka-renderer-v"
_RENDERER_PACKAGE = "kalinka-renderer"
_SUPERVISOR_TAG_PREFIX = "kalinka-supervisor-v"
_SUPERVISOR_PACKAGE = "kalinka-supervisor"
_WEB_TAG_PREFIX = "v"
_WEB_PACKAGE = "kalinka-web"
_TICK_INTERVAL = 3600  # check hourly; also guarantees ticks inside quiet hours
_QUIET_HOURS = range(3, 6)  # local time — auto-upgrade fires in [3:00, 6:00)

# Root-side upgrade machinery shipped by the deb. Deliberately not
# KALINKA_PREFIX-resolved: a dev fakeroot has no systemd watching its run
# dir, so upgrade must read as unsupported there even when a deb install
# also exists on the same machine.
_UPGRADE_SCRIPT = "/opt/kalinka/upgrade.sh"
_UPGRADE_PATH_UNIT = "/etc/systemd/system/kalinka-upgrade.path"
_RESTART_TRIGGER = "restart-request"
_DPKG_STATUS = "/var/lib/dpkg/status"


def _repo() -> str:
    # Same override the install script honours.
    return os.environ.get("KALINKA_REPO", "Kalinka-Player/KalinkaPlayer")


def _web_repo() -> str:
    # Same override the install script honours.
    return os.environ.get("KALINKA_WEB_REPO", "Kalinka-Player/KalinkaAI")


def upgrade_supported() -> bool:
    """True when PUT /server/upgrade can actually perform an upgrade."""
    return (
        paths.prefix() == "/"
        and os.path.isfile(_UPGRADE_SCRIPT)
        and os.path.isfile(_UPGRADE_PATH_UNIT)
    )


_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def latest_release_version(feed_xml: str, prefix: str) -> str | None:
    """Version of the newest release tagged ``<prefix>*`` in a feed.

    Entries come newest-first; each entry's ``<id>`` ends in the release
    tag (``tag:github.com,2008:Repository/<id>/<tag>``). Tags of other
    families in the same repo (``kalinka-renderer-v*`` alongside
    ``kalinka-v*``, ``jamendo-ai-v*`` asset releases) are skipped, matching
    the selection in scripts/install-release.sh. Drafts never appear in the
    public feed.

    The feed carries one page of releases, so a family that has not been
    released in a long time can fall off it entirely and read as unknown —
    which costs an upgrade offer, never a wrong one.
    """
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError as e:
        logger.warning("Cannot parse releases feed: %s", e)
        return None
    for entry in root.iter(f"{_ATOM_NS}entry"):
        tag = (entry.findtext(f"{_ATOM_NS}id") or "").rsplit("/", 1)[-1]
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def is_newer(latest: str, current: str) -> bool:
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        logger.warning("Cannot compare versions %r and %r", latest, current)
        return False


def installed_package_version(package: str) -> str | None:
    """Version of an installed Debian package, excluding removed packages."""
    try:
        query = subprocess.run(
            ["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", package],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("Cannot query the installed package: %s", e)
        return None
    if query.returncode != 0:
        return None
    status, _, version = query.stdout.strip().partition(" ")
    return version if status == "installed" and version else None


def dpkg_status_stamp() -> int | None:
    """When dpkg last changed what is installed, or None without dpkg."""
    try:
        return os.stat(_DPKG_STATUS).st_mtime_ns
    except OSError:
        return None


def installed_renderer_version() -> str | None:
    return installed_package_version(_RENDERER_PACKAGE)


def installed_supervisor_version() -> str | None:
    return installed_package_version(_SUPERVISOR_PACKAGE)


def installed_web_version() -> str | None:
    return installed_package_version(_WEB_PACKAGE)


def deb_is_newer(candidate: str, installed: str) -> bool:
    """Whether dpkg orders ``candidate`` above ``installed``.

    Renderer packages carry deb versions — ``0.2.0~dev3+g1a2b3c4`` sorts
    *below* the ``0.2.0`` it leads up to — which PEP 440 reads differently
    or not at all, so the question goes to the tool that will run the
    install.
    """
    try:
        compare = subprocess.run(
            ["dpkg", "--compare-versions", candidate, "gt", installed],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Cannot compare Debian package versions: %s", e)
        return False
    return compare.returncode == 0


def request_upgrade() -> None:
    """Touch the trigger watched by kalinka-upgrade.path. Raises OSError."""
    _touch_trigger("upgrade-request")


def request_restart() -> None:
    """Touch the trigger watched by kalinka-restart.path. Raises OSError.

    The root-owned kalinka-restart.service it fires runs the package helpers
    and then restarts kalinka.service; Core exits when systemd stops it.
    """
    _touch_trigger(_RESTART_TRIGGER)


def restart_pending() -> bool:
    """True from a restart request until Core stops to carry it out.

    kalinka-restart.path starts no second run while kalinka-restart.service
    runs, so anything left for its helpers once they have run would wait for
    a restart that never comes. The trigger goes with Core's runtime
    directory when Core stops.
    """
    return (Path(paths.run_dir()) / _RESTART_TRIGGER).exists()


def _touch_trigger(name: str) -> None:
    trigger = Path(paths.run_dir()) / name
    trigger.parent.mkdir(parents=True, exist_ok=True)
    trigger.touch()


def validate_upgrade_request(
    target: str,
    latest: str | None,
    current: str,
    package_behind: bool = False,
) -> str | None:
    """Reason a PUT /server/upgrade must be rejected, or None to proceed.

    The client echoes the bundle version it saw in GET /server/update; no
    known release, nothing left to upgrade, or a different release
    published since all reject rather than firing the installer again.
    ``package_behind`` keeps requests valid when Core is current but the
    renderer, supervisor or browser player needs updating. The echoed
    version remains the bundle version, matching the existing app protocol.
    """
    if not latest:
        return "No update available"
    if not is_newer(latest, current) and not package_behind:
        return "No update available"
    if target != latest:
        return (
            f"Requested version {target!r} is not the available "
            f"update {latest!r}"
        )
    return None


@dataclass
class _PackageTrain:
    """A package on this machine and the release train it follows.

    A machine without the package is not missing an update: the installer
    would be adding one, which is an install decision, not an upgrade.
    """

    tag_prefix: str
    read_installed: Callable[[], str | None]
    latest: str | None = None
    installed: str | None = None
    behind: bool = False

    def follow(self, feed: str) -> None:
        if version := latest_release_version(feed, self.tag_prefix):
            self.latest = version

    def settle(self) -> None:
        self.behind = bool(
            self.installed and self.latest and deb_is_newer(self.latest, self.installed)
        )


class UpdateChecker:
    """Hourly background lookup of what is published and what is installed.

    ``run()`` is started as a task from the app lifecycle; endpoints read
    the cached properties, after :meth:`refresh_installed` has caught up
    with what dpkg installed since. A transient fetch failure keeps the last
    known result (an available update must not vanish on a network blip),
    and so does a feed page that happens to carry no release of one
    family. A package update is offered only once the upgrade API has a
    bundle target, since that is the version a press echoes back.
    """

    def __init__(self) -> None:
        self._latest: str | None = None
        # Late-bound, so the module's readers can be replaced after import.
        self._renderer = _PackageTrain(
            _RENDERER_TAG_PREFIX, lambda: installed_renderer_version()
        )
        self._supervisor = _PackageTrain(
            _SUPERVISOR_TAG_PREFIX, lambda: installed_supervisor_version()
        )
        self._web = _PackageTrain(_WEB_TAG_PREFIX, lambda: installed_web_version())
        self._dpkg_stamp: int | None = None
        self._last_auto_attempt: date | None = None

    @property
    def _packages(self) -> tuple[_PackageTrain, ...]:
        return (self._renderer, self._supervisor, self._web)

    @property
    def latest(self) -> str | None:
        """Latest known bundle release — None until a check succeeds."""
        return self._latest

    @property
    def latest_renderer(self) -> str | None:
        """Latest known renderer release — None until a check succeeds."""
        return self._renderer.latest

    @property
    def installed_renderer(self) -> str | None:
        """Renderer version installed here as of the last check."""
        return self._renderer.installed

    @property
    def latest_supervisor(self) -> str | None:
        return self._supervisor.latest

    @property
    def installed_supervisor(self) -> str | None:
        return self._supervisor.installed

    @property
    def latest_web(self) -> str | None:
        """Latest known app release, which carries the browser player."""
        return self._web.latest

    @property
    def installed_web(self) -> str | None:
        return self._web.installed

    def update_available(self) -> bool:
        """Whether an installer run would bring anything newer to this box."""
        return self.bundle_update_available() or self.package_update_available()

    def bundle_update_available(self) -> bool:
        """Whether a newer app bundle than the running one is published."""
        return bool(self._latest and is_newer(self._latest, get_version()))

    def package_update_available(self) -> bool:
        """Whether a package on this machine is behind its latest release."""
        return bool(self._latest) and any(p.behind for p in self._packages)

    def renderer_update_available(self) -> bool:
        return bool(self._latest and self._renderer.behind)

    def supervisor_update_available(self) -> bool:
        return bool(self._latest and self._supervisor.behind)

    def web_update_available(self) -> bool:
        return bool(self._latest and self._web.behind)

    async def check_now(self) -> str | None:
        """Refresh release trains and installed package versions.

        Returns the latest bundle release, or None when the feed could not
        be read. The package verdicts are settled here and when dpkg changes,
        rather than on every read, so asking for them rarely costs a process.
        """
        await asyncio.to_thread(self._read_installed)
        feed, web_feed = await asyncio.gather(self._fetch(), self._fetch_web())
        latest = None
        if feed is not None:
            latest = latest_release_version(feed, _BUNDLE_TAG_PREFIX)
            if latest:
                self._latest = latest
            self._renderer.follow(feed)
            self._supervisor.follow(feed)
        if web_feed is not None:
            self._web.follow(web_feed)
        await asyncio.to_thread(self._settle_packages)
        return latest

    def refresh_installed(self) -> None:
        """Re-read the installed packages if dpkg has changed them since.

        An installer run that moves only a package — the renderer, the
        supervisor or the browser player — leaves Core running, so without
        this the hourly check would keep offering the upgrade that run has
        just installed.
        """
        if dpkg_status_stamp() != self._dpkg_stamp:
            self._read_installed()
            self._settle_packages()

    def _read_installed(self) -> None:
        self._dpkg_stamp = dpkg_status_stamp()
        for package in self._packages:
            package.installed = package.read_installed()

    def _settle_packages(self) -> None:
        for package in self._packages:
            package.settle()

    async def run(
        self,
        auto_upgrade_enabled: Callable[[], bool] = lambda: False,
        playback_stopped: Callable[[], Awaitable[bool]] | None = None,
        renderers_ready: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        """Hourly tick: refresh the release info and, when the (live-read)
        config toggle is on, fire the auto-upgrade in the quiet-hours
        window."""
        while True:
            await self.check_now()
            if auto_upgrade_enabled():
                await self.maybe_auto_upgrade(playback_stopped, renderers_ready)
            await asyncio.sleep(_TICK_INTERVAL)

    async def maybe_auto_upgrade(
        self,
        playback_stopped: Callable[[], Awaitable[bool]] | None = None,
        renderers_ready: Callable[[], Awaitable[bool]] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Trigger the root-side upgrade during quiet hours, but never
        while something is playing — a later tick in the same window
        retries once playback stops.

        The renderers go first. ``renderers_ready`` says whether any
        registered renderer still has to be brought forward; while one
        does, this server holds where it is, because a release that moves
        the renderer protocol would otherwise leave it stranded on
        another machine. The tick that follows finds them upgraded and
        goes ahead.

        At most one attempt per day: a failed install (server still up
        next tick) retries the following night rather than hammering,
        and a successful one restarts the process anyway.
        """
        now = now or datetime.now()
        if now.hour not in _QUIET_HOURS:
            return
        if self._last_auto_attempt == now.date():
            return
        if not self.update_available():
            return
        if not upgrade_supported():
            return
        if playback_stopped is not None:
            try:
                if not await playback_stopped():
                    logger.info("Auto-upgrade postponed: playback active")
                    return
            except Exception as e:  # noqa: BLE001 — don't upgrade blind
                logger.warning("Auto-upgrade playback probe failed: %s", e)
                return
        if renderers_ready is not None:
            try:
                if not await renderers_ready():
                    logger.info("Auto-upgrade postponed: renderers go first")
                    return
            except Exception as e:  # noqa: BLE001 — don't upgrade blind
                logger.warning("Auto-upgrade renderer check failed: %s", e)
                return
        self._last_auto_attempt = now.date()
        try:
            request_upgrade()
        except OSError as e:
            logger.error("Auto-upgrade trigger failed: %s", e)
            return
        logger.info(
            "Auto-upgrade requested "
            "(server %s, renderer %s, supervisor %s, browser player %s)",
            self._latest if self.bundle_update_available() else "current",
            self.latest_renderer if self.renderer_update_available() else "current",
            self.latest_supervisor
            if self.supervisor_update_available()
            else "current",
            self.latest_web if self.web_update_available() else "current",
        )

    async def _fetch(self) -> str | None:
        """This repo's releases.atom document, or None when unreachable."""
        return await _fetch_feed(_repo())

    async def _fetch_web(self) -> str | None:
        """The app repo's releases.atom document, or None when unreachable."""
        return await _fetch_feed(_web_repo())


async def _fetch_feed(repo: str) -> str | None:
    url = f"https://github.com/{repo}/releases.atom"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Release feed lookup failed: %s", e)
        return None
    return response.text


#: Process-wide instance; run() is started from the server lifecycle.
checker = UpdateChecker()
