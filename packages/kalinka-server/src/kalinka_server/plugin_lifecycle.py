"""Setting a plugin up, and taking it down, while the server runs.

One owner of the order things happen in, so a settings change, the on/off
switch and a package operation all unload and load a plugin the same way:
clients stop being offered it, the output it holds is taken back, what plays
from it stops, calls already under way drain, it shuts down, what it left on
the bus is closed, and only then is it forgotten or set up again. One at a
time: everything here runs under one lock.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from importlib.metadata import packages_distributions
from typing import Any, Awaitable, Callable, MutableMapping, Optional

from kalinka_plugin_sdk import ModuleHealthState, ModuleState
from kalinka_plugin_sdk.direct_playback import RevokeReason
from kalinka_plugin_sdk.plugin import PluginBase, PluginType

from .device_automation import DeviceAutomation
from .module_timeout import PLUGIN_CALL_TIMEOUT_S, TRACK_SOURCE_TIMEOUT_S, TimeLimitedInputModule
from .output_device_router import OutputDeviceRouter
from .playback_arbiter import PlaybackArbiter
from .player_setup import PreparedModuleCollection, PreparedPlugin
from .playqueue import PlayQueueImpl
from .replaygain import ReplayGainController
from .storage.manager import StorageManager

logger = logging.getLogger(__name__.split(".")[-1])

SHUTDOWN_TIMEOUT_S = 15.0
RESTARTING = "Restarting…"
# Never purged from the process, whatever a plugin's distribution says.
_KEPT_PACKAGES = frozenset({"kalinka_plugin_sdk", "kalinka_server", "kalinka_eventbus"})


class PluginNotInstalled(LookupError):
    """No installed entry point carries this plugin id."""


class PluginLifecycle:
    """Loads, unloads and re-initialises one plugin at a time.

    @note Every method takes the lifecycle lock; none may be called while
        holding it, including from a plugin's ``shutdown()``.
    """

    def __init__(
        self,
        modules: PreparedModuleCollection,
        *,
        overrides: MutableMapping[str, Any],
        playqueue: PlayQueueImpl,
        arbiter: PlaybackArbiter,
        device_router: OutputDeviceRouter,
        replaygain: ReplayGainController,
        storage: StorageManager,
        device_automation: Callable[[], Optional[DeviceAutomation]],
        republish: Callable[[], Awaitable[None]],
        rebuild_schema: Callable[[], None],
        shutdown_timeout_s: float = SHUTDOWN_TIMEOUT_S,
        drain_timeout_s: float = TRACK_SOURCE_TIMEOUT_S,
    ):
        self._modules = modules
        self._overrides = overrides
        self._playqueue = playqueue
        self._arbiter = arbiter
        self._router = device_router
        self._replaygain = replaygain
        self._storage = storage
        self._device_automation = device_automation
        self._republish = republish
        self._rebuild_schema = rebuild_schema
        self._shutdown_timeout_s = shutdown_timeout_s
        self._drain_timeout_s = drain_timeout_s
        self._lock = asyncio.Lock()

    async def unload(self, name: str, *, gone: bool) -> None:
        """Take the plugin down: forgotten when ``gone``, else left switched
        off, its settings still shown."""
        async with self._lock:
            await self._unload(name, gone=gone, resuming=False)

    async def load(self, name: str, *, purge: bool) -> PreparedPlugin:
        """Set the plugin up from what is installed now. With ``purge`` its
        code is imported afresh, for a package that was updated.

        @raise PluginNotInstalled When no entry point carries ``name``.
        """
        async with self._lock:
            return await self._load(name, purge=purge)

    async def suspend(self, name: str) -> None:
        """Take the plugin down for its files to be replaced: listed as
        restarting, its routes say retry, and a save made meanwhile applies
        when ``load`` brings it back — or ``abandon`` says it will not."""
        async with self._lock:
            await self._unload(name, gone=False, resuming=True)

    async def abandon(self, name: str, message: str) -> None:
        """Give up on a plugin taken down with ``suspend``: reachable again
        and in error with ``message``, so the modules page says why."""
        async with self._lock:
            prepared = self._modules.prepared(name)
            if prepared is None or prepared.mounted:
                return
            _leave_in_error(prepared, message)
            self._rebuild_schema()
            await self._republish()

    async def settings_changed(self, name: str, changed: frozenset[str]) -> None:
        """Apply a save to the plugin that owns it.

        The on/off switch loads or unloads it — except a plugin set up while
        switched off, a receiver that watches for pairing meanwhile, which
        takes the switch in as any other setting. Otherwise it is asked to
        take the change in and, unless it says it did, set up again on the
        live config — which also gives a plugin in error a chance with a
        corrected setting.
        """
        async with self._lock:
            prepared = self._modules.prepared(name)
            if prepared is None:
                return
            if not prepared.mounted:
                # Down for an update: set up on the live config once it is back.
                logger.info("%s is being set up again; the save applies then", name)
                return
            enabled = bool(getattr(prepared.plugin_context.config, "enabled", True))
            runs_while_off = getattr(prepared.plugin_class, "SETUP_WHEN_DISABLED", False)
            if not enabled and not runs_while_off:
                if prepared.health_state != ModuleHealthState.DISABLED:
                    await self._unload(name, gone=False, resuming=False)
                return
            if not prepared.set_up:
                await self._load(name, purge=False)
                return
            if await self._taken_in(prepared, changed):
                return
            await self._unload(name, gone=False, resuming=True)
            await self._load(name, purge=False)

    async def _taken_in(self, prepared: PreparedPlugin, changed: frozenset[str]) -> bool:
        instance = prepared.plugin_instance
        assert instance is not None
        # A one-shot acted on at save time is disarmed on disk before the
        # plugin sees it armed, and reset in memory once it has.
        consumed = self._modules.consume_immediate_actions(
            prepared, set(changed), self._overrides
        )
        try:
            handled = await asyncio.wait_for(
                instance.config_changed(changed), PLUGIN_CALL_TIMEOUT_S
            )
        except Exception as e:  # noqa: BLE001 — whatever it did, it is set up again
            logger.warning(
                "%s did not take its settings in (%s); setting it up again",
                prepared.plugin_context.plugin_id,
                type(e).__name__,
            )
            return False
        finally:
            if consumed:
                self._modules.finish_immediate_actions(prepared, consumed)
        return handled is True

    async def _unload(self, name: str, *, gone: bool, resuming: bool) -> None:
        prepared = self._modules.prepared(name)
        if prepared is None:
            return
        is_device = name in self._modules.prepared_devices
        running = prepared.set_up and prepared.plugin_instance is not None
        prepared.mounted = False
        if running:
            prepared.reported = _restarting()
        await self._republish()

        if running:
            await self._arbiter.revoke_plugin(name, RevokeReason.PLUGIN_UNLOADED)
            if is_device:
                await self._router.resync()
                await self._replaygain.refresh()
                automation = self._device_automation()
                if automation is not None:
                    automation.forget(name)
            else:
                await self._playqueue.drop_source(name, gone=gone)
            interface = prepared.interface
            if isinstance(interface, TimeLimitedInputModule):
                interface.close()
                if not await interface.drain(self._drain_timeout_s):
                    logger.warning("%s still had calls under way; not waiting", name)
            await self._shutdown(name, prepared)
        elif gone:
            await self._playqueue.drop_source(name, gone=True)
        prepared.cancel_pending()
        revoke = getattr(prepared.plugin_context.listener, "revoke_all", None)
        if revoke is not None:
            left = revoke()
            if left:
                logger.warning("%s left %d bus subscription(s) open", name, left)

        if gone:
            self._modules.remove(name)
        else:
            # As the boot scan leaves a switched-off plugin: an instance that
            # was never set up, so its settings are still validated and shown.
            prepared.plugin_instance = prepared.plugin_class()
            prepared.interface = None
            prepared.health_state = ModuleHealthState.DISABLED
            prepared.error_message = None
            prepared.reported = _restarting() if resuming else None
            prepared.mounted = not resuming
        self._rebuild_schema()
        await self._republish()
        if not is_device:
            await self._storage.refresh()

    async def _shutdown(self, name: str, prepared: PreparedPlugin) -> None:
        instance = prepared.plugin_instance
        assert instance is not None
        try:
            await asyncio.wait_for(instance.shutdown(), self._shutdown_timeout_s)
        except Exception as e:  # noqa: BLE001 — the type only: its text may carry a credential
            logger.warning(
                "%s did not shut down cleanly (%s); carrying on", name, type(e).__name__
            )

    async def _load(self, name: str, *, purge: bool) -> PreparedPlugin:
        existing = self._modules.prepared(name)
        try:
            prepared = await self._set_up(name, existing, purge=purge)
        except Exception as e:  # noqa: BLE001 — it must not stay unreachable
            if existing is not None and not existing.mounted:
                _leave_in_error(existing, f"Could not be set up ({type(e).__name__}).")
                self._rebuild_schema()
                await self._republish()
            raise
        plugin_class = prepared.plugin_class
        input_modules = self._modules.prepared_input_modules
        devices = self._modules.prepared_devices
        owners, others = (
            (input_modules, devices)
            if plugin_class.PLUGIN_TYPE == PluginType.INPUT_MODULE
            else (devices, input_modules)
        )
        others.pop(name, None)
        # Assigned in place: the settings page lists modules in this order.
        owners[name] = prepared
        self._rebuild_schema()
        await self._republish()
        if plugin_class.PLUGIN_TYPE == PluginType.OUTPUT_DEVICE:
            await self._router.resync()
            await self._replaygain.refresh()
        else:
            await self._storage.refresh()
            await self._playqueue.source_returned(name)
        return prepared

    async def _set_up(
        self, name: str, existing: Optional[PreparedPlugin], *, purge: bool
    ) -> PreparedPlugin:
        if existing is None or purge:
            if existing is not None:
                _purge_imports(existing.plugin_class)
            plugin_class = self._modules.find_class(name)
            if plugin_class is None:
                raise PluginNotInstalled(name)
        else:
            plugin_class = existing.plugin_class
        prepared = await self._modules.prepare_one(name, plugin_class, self._overrides)
        self._modules.flush_overrides(self._overrides)
        if prepared is None:
            raise RuntimeError(f"{name} could not be set up: its configuration would not build")
        return prepared


def _restarting() -> ModuleState:
    return ModuleState(state=ModuleHealthState.WARNING, message=RESTARTING)


def _leave_in_error(prepared: PreparedPlugin, message: str) -> None:
    """An unloaded plugin that is not coming back by itself: reachable, so
    its routes say it is not available rather than restarting for good, and
    in error, so the modules page says why and a save gives it another go.
    The instance the unload left, never set up, still validates its
    settings."""
    prepared.mounted = True
    prepared.health_state = ModuleHealthState.ERROR
    prepared.error_message = message
    prepared.reported = None


def _purge_imports(plugin_class: type[PluginBase]) -> None:
    """Drop the plugin's distribution from the import cache, so the next
    import reads the package now installed."""
    top = plugin_class.__module__.partition(".")[0]
    by_top = packages_distributions()
    distributions = set(by_top.get(top, ()))
    tops = {top} | {
        other for other, dists in by_top.items() if distributions & set(dists)
    }
    tops -= _KEPT_PACKAGES
    for module_name in list(sys.modules):
        root = module_name.partition(".")[0]
        if root in tops:
            del sys.modules[module_name]
    importlib.invalidate_caches()
