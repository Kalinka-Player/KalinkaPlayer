"""``/server/update`` and ``/server/upgrade``: this install's own upgrade.

What is published comes from :mod:`update_check`'s cache; the renderers this
Core drives are brought forward through :class:`RendererUpgradeService`
before the installer is fired.
"""

import logging
from typing import Any, Dict

from fastapi import FastAPI, HTTPException

from . import update_check
from .renderer_upgrade import RendererUpgradeService
from .version import get_version

logger = logging.getLogger(__name__.split(".")[-1])


def register_upgrade_routes(
    app: FastAPI, renderer_upgrades: RendererUpgradeService
) -> None:
    """Mount the update report and the upgrade trigger on ``app``."""

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
            "upgrade_supported": update_check.upgrade_supported(),
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
        path: one asked here is still restarting into its new build, so
        this answers 409 and the press is repeated once it is back.
        """
        if not update_check.upgrade_supported():
            raise HTTPException(
                status_code=501,
                detail="Upgrade is not supported on this install "
                "(root-side upgrade units are missing)",
            )
        target = str(payload.get("version") or "")
        if not target:
            raise HTTPException(
                status_code=400, detail="'version' is required"
            )
        rejection = update_check.validate_upgrade_request(
            target,
            update_check.checker.latest,
            get_version(),
            update_check.checker.renderer_update_available(),
        )
        if rejection:
            raise HTTPException(status_code=409, detail=rejection)
        if not await renderer_upgrades.bring_forward(
            update_check.checker.latest_renderer
        ):
            raise HTTPException(
                status_code=409,
                detail="Upgrading the renderers first; try again in a moment",
            )
        try:
            update_check.request_upgrade()
        except OSError as e:
            logger.error("Failed to write upgrade trigger: %s", e)
            raise HTTPException(
                status_code=500, detail="Failed to request upgrade"
            ) from e
        return {"message": "upgrading"}
