"""``/server/update`` and ``/server/upgrade``: this install's own upgrade.

What is published comes from :mod:`update_check`'s cache; the renderers this
Core drives are brought forward through :class:`RendererUpgradeService`
before the installer is fired.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict

from fastapi import FastAPI, HTTPException

from . import update_check
from .renderer_upgrade import INSTALL_TIMEOUT_S, RendererUpgradeService
from .version import get_version

logger = logging.getLogger(__name__.split(".")[-1])
RECHECK_INTERVAL_S = 1.0


def register_upgrade_routes(
    app: FastAPI,
    renderer_upgrades: RendererUpgradeService,
    *,
    demo_mode: Callable[[], bool] = lambda: False,
) -> None:
    """Mount the update report and the upgrade trigger on ``app``."""
    lock = asyncio.Lock()
    requested_at = None
    app.state.upgrade_task = None

    def request_install() -> None:
        nonlocal requested_at
        update_check.request_upgrade()
        requested_at = time.monotonic()

    async def finish_upgrade(renderer_version: str, covers_local: bool) -> None:
        """Continue a manual press even when automatic upgrades are disabled.

        Acceptance and disconnect are not installation success. The service
        remembers every accepted renderer until its advertised version confirms
        the upgrade, including ones that disappear from the registry.
        """
        try:
            async with asyncio.timeout(INSTALL_TIMEOUT_S):
                while True:
                    await asyncio.sleep(RECHECK_INTERVAL_S)
                    progress = await renderer_upgrades.bring_forward(
                        renderer_version, installer_covers_local=covers_local
                    )
                    if progress.holding:
                        logger.warning(
                            "Server upgrade held: %s", "; ".join(progress.holding)
                        )
                        return
                    if progress.done:
                        request_install()
                        return
        except (TimeoutError, OSError):
            logger.exception("Server upgrade could not finish; retry the upgrade")

    @app.get("/server/update")
    def get_update_info():
        """Report whether a newer release is available for this machine.

        Served entirely from the daily background check's cache — never
        does network I/O. The app should show its upgrade banner only
        when both ``update_available`` and ``upgrade_supported`` are
        true (dev installs report ``upgrade_supported: false``);
        dismissing the banner is purely client-side state.

        ``update_available`` covers the whole install — one upgrade run
        also brings the renderer on this machine up to date, and the
        ``renderer_*`` fields say where that one stands. ``latest_version``
        stays the bundle's, and is what PUT /server/upgrade expects back.
        """
        checker = update_check.checker
        return {
            "current_version": get_version(),
            "latest_version": checker.latest,
            "update_available": checker.update_available(),
            "renderer_current_version": checker.installed_renderer,
            "renderer_latest_version": checker.latest_renderer,
            "renderer_update_available": checker.renderer_update_available(),
            "supervisor_current_version": checker.installed_supervisor,
            "supervisor_latest_version": checker.latest_supervisor,
            "supervisor_update_available": checker.supervisor_update_available(),
            "upgrade_supported": not demo_mode() and update_check.upgrade_supported(),
        }

    @app.put("/server/upgrade")
    async def upgrade_server(payload: Dict[str, Any]):
        """Upgrade this install to the release named in ``{"version": ...}``.

        The version must be the one currently advertised as
        ``latest_version`` by GET /server/update; a stale banner, or a
        retry once there is nothing left to upgrade, gets a 409 instead
        of firing the installer again.

        Touches the trigger file watched by the root-owned
        kalinka-upgrade.path unit; its oneshot fetches the published
        installer from kalinkaplayer.com and runs it, which upgrades the
        bundle, the web player and this machine's renderer together, and
        the new package's postinst restarts kalinka.service — so a
        successful upgrade looks to clients like a (long) restart.
        Progress and failure detail stay in the systemd journal; the app
        confirms the outcome by re-reading /server/version after
        reconnect.

        Renderers on other machines go first, as they do on the automatic
        path, except that the press is someone asking: playback on one that
        is behind is stopped so it can take its upgrade, and once each has
        taken it on this answers 'upgrading'. The server's installer follows
        only after all accepted renderers reconnect on the target version.
        This also serialises installs for older local renderers without machine
        identity: they must finish their own update before this install starts.
        One that does not take the request on
        — it refused, did not answer, or is still answering an earlier ask —
        gets a 409 naming it and why. This machine's renderer is left to the
        installer run where it came from the renderer package, which that run
        upgrades; installed any other way, it is asked like the rest.
        """
        if demo_mode():
            raise HTTPException(status_code=403, detail="Demo server is read-only")
        if not update_check.upgrade_supported():
            raise HTTPException(
                status_code=501,
                detail="Upgrade is not supported on this install "
                "(root-side upgrade units are missing)",
            )
        target = str(payload.get("version") or "")
        if not target:
            raise HTTPException(status_code=400, detail="'version' is required")
        checker = update_check.checker
        rejection = update_check.validate_upgrade_request(
            target,
            checker.latest,
            get_version(),
            checker.renderer_update_available(),
            checker.supervisor_update_available(),
        )
        if rejection:
            raise HTTPException(status_code=409, detail=rejection)
        async with lock:
            task = app.state.upgrade_task
            if (
                requested_at is not None
                and time.monotonic() - requested_at < INSTALL_TIMEOUT_S
            ) or (task is not None and not task.done()):
                return {"message": "upgrading"}
            renderers = await renderer_upgrades.bring_forward(
                checker.latest_renderer,
                interrupt=True,
                installer_covers_local=checker.installed_renderer is not None,
            )
            if renderers.holding:
                raise HTTPException(
                    status_code=409,
                    detail="Renderers upgrade first: " + "; ".join(renderers.holding),
                )
            if renderers.done:
                try:
                    request_install()
                except OSError as e:
                    logger.error("Failed to write upgrade trigger: %s", e)
                    raise HTTPException(
                        status_code=500, detail="Failed to request upgrade"
                    ) from e
            else:
                app.state.upgrade_task = asyncio.create_task(
                    finish_upgrade(
                        checker.latest_renderer, checker.installed_renderer is not None
                    )
                )
        return {"message": "upgrading"}
