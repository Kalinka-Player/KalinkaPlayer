import asyncio
import enum
import logging
import os
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Generator, Mapping, MutableMapping

from kalinka_eventbus import EventBus
from kalinka_plugin_sdk import DeviceVolume, ModuleHealthState, paths
from kalinka_plugin_sdk import __version__ as SDK_VERSION
from kalinka_plugin_sdk.datamodel import PlaybackMode, PlaybackState
from kalinka_plugin_sdk.events import (
    PlayQueueState,
    PlayQueueEvent,
    PlayQueueEventType,
)
from kalinka_plugin_sdk.ext_device import ExternalOutputDevice, SupportedFunction
from kalinka_plugin_sdk.ext_device_events import (
    ExtDeviceEvent,
    ExtDeviceEventType,
    ExtDeviceState,
)
from kalinka_plugin_sdk.inputmodule import InputModule
from kalinka_plugin_sdk.module_config import ModuleConfig
from kalinka_plugin_sdk.plugin import (
    InputPluginContext,
    OutputDevicePluginContext,
    PluginBase,
    PluginType,
    cast_plugin_interface,
)
from pydantic import BaseModel, ConfigDict

from .config_model import KalinkaConfig
from .config_overrides import (
    apply_overrides_with_prefix,
    find_one_shot_overrides,
    is_one_shot_field,
    save_overrides,
)
from .config_secrets import is_secret_path, loggable
from .module_timeout import TimeLimitedInputModule
from .output_device_router import OutputDeviceRouter
from .playqueue import PlayQueueImpl
from .renderer_output_device import RendererOutputPlugin
from .renderer_prefs import RendererPreferences
from .renderer_registry import RendererRegistry
from .renderer_sessions import SessionPool
from .text_embedder import SharedTextEmbedder
from kalinka_plugin_sdk.api import PlayQueueController

logger = logging.getLogger(__name__.split(".")[-1])


def _to_jsonable(value: Any) -> Any:
    """Coerce a model value into the JSON-friendly form the overrides
    file stores. Enum-typed fields read back off the model as Enum
    instances, but the on-disk override is the raw scalar (``.value``):
    without this the ``==`` comparison would always miss and the Enum
    would be written straight into the dict, crashing the later
    ``json.dumps`` in ``save_overrides``. Recurses through lists/dicts."""
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


@dataclass
class PlayerContext:
    playqueue: PlayQueueController
    playqueue_eventbus: EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent]  # type: ignore[type-var]
    ext_device_eventbus: EventBus[ExtDeviceState, ExtDeviceEventType, ExtDeviceEvent]  # type: ignore[type-var]
    # Shared MiniLM text embedder — one model instance for the server and
    # every plugin (handed out via the plugin contexts). Lazy: loads on
    # first embed() call.
    embedder: SharedTextEmbedder | None = None
    # Resolves which device module owns the active renderer's output. Set by
    # setup() before the plugin scan, so device emitters can be routed.
    device_router: "OutputDeviceRouter | None" = None


@dataclass
class PreparedPlugin:
    """A class to hold prepared plugins for shutdown."""

    plugin_class: type[PluginBase]
    plugin_instance: PluginBase | None
    health_state: ModuleHealthState
    plugin_context: InputPluginContext | OutputDevicePluginContext
    interface: InputModule | ExternalOutputDevice | None
    error_message: str | None = None

    @classmethod
    async def setup(cls, 
              plugin_class: type[PluginBase],
              plugin_context: InputPluginContext | OutputDevicePluginContext):
        """Setup the module with the provided components."""
        plugin_instance = plugin_class()
        health_state = ModuleHealthState.DISABLED
        interface = None
        if plugin_context.config.enabled is True:
            await plugin_instance.setup(plugin_context)
            health_state = ModuleHealthState.READY
            interface = cast_plugin_interface(plugin_instance)
            if (
                plugin_class.PLUGIN_TYPE == PluginType.INPUT_MODULE
                and interface is not None
            ):
                # Enforce the SDK latency contract: every call into an input
                # module must finish within the per-call budget, regardless
                # of what the module does (see module_timeout).
                interface = TimeLimitedInputModule(
                    interface, plugin_context.plugin_id
                )
        else:
            logger.info(
                f"Plugin {plugin_class.PLUGIN_ID} is disabled in configuration - skipping setup"
            )

        return cls(
            plugin_class=plugin_class,
            plugin_instance=plugin_instance,
            health_state=health_state,
            plugin_context=plugin_context,
            interface=interface,
        )

    async def shutdown(self):
        """Shutdown the module if it has a shutdown method."""
        if self.plugin_instance:
            await self.plugin_instance.shutdown()


@dataclass
class PreparedModuleCollection:
    """A collection to hold prepared input modules and devices."""

    prepared_input_modules: dict[str, PreparedPlugin] = field(default_factory=dict)
    prepared_devices: dict[str, PreparedPlugin] = field(default_factory=dict)
    enabled_input_modules: set[str] = field(default_factory=set)
    player_context: PlayerContext | None = None
    # Set by ``scan_and_setup_plugins`` when a plugin's setup mutated
    # config fields that came from the overrides file. The caller
    # (``create_app``) reads this to decide whether to re-persist the
    # overrides dict so the mutations survive a restart.
    overrides_dirty: bool = False
    # Path of the overrides file, so one-shot triggers can be reset on disk
    # *before* the owning plugin acts on them (see _consume_one_shot_overrides).
    # None disables the disk persist (in-memory reset only — used by tests).
    overrides_file: str | None = None

    def _update_enabled_input_modules(self):
        """Update the set of enabled input module names."""
        self.enabled_input_modules = {
            name
            for name, module in self.prepared_input_modules.items()
            if module.health_state == ModuleHealthState.READY
        }

    def _scan_entry_points(self) -> Generator[tuple[str, type], None, None]:
        """Scan for installed plugins using entry points and yield classes derived from PluginBase."""

        try:
            eps = entry_points(group="kalinka.plugins")
        except Exception as e:
            logger.warning(f"Failed to load entry points: {e}")
            return

        for ep in eps:
            try:
                logger.debug(f"Loading entry point: {ep.name}")

                # Load the class from the entry point
                plugin_cls = ep.load()

                # Check if it's a subclass of PluginBase
                if isinstance(plugin_cls, type) and issubclass(plugin_cls, PluginBase):
                    # Check for required attributes
                    if hasattr(plugin_cls, "PLUGIN_ID"):
                        logger.info("Found plugin class: %s, name: %s", plugin_cls.PLUGIN_TYPE, plugin_cls.PLUGIN_ID)
                        yield (plugin_cls.PLUGIN_ID, plugin_cls)
                    else:
                        logger.warning(
                            f"Plugin class {ep.name} is missing required attribute: PLUGIN_ID"
                        )
                else:
                    logger.warning(
                        f"Entry point {ep.name} is not a subclass of PluginBase"
                    )

            except Exception as e:
                logger.error(f"Failed to load plugin {ep.name}: {e}", exc_info=True)

    def _build_module_config(
        self,
        plugin_name: str,
        plugin_class: type[PluginBase],
        overrides: Mapping[str, Any],
    ) -> ModuleConfig:
        """Instantiate a plugin's default config, then apply matching overrides."""
        config = plugin_class.CONFIG_MODEL()
        prefix = (
            "input_modules."
            if plugin_class.PLUGIN_TYPE == PluginType.INPUT_MODULE
            else "devices."
        )
        apply_overrides_with_prefix(config, overrides, f"{prefix}{plugin_name}.")
        return config

    def _consume_one_shot_overrides(
        self,
        plugin_name: str,
        plugin_class: type[PluginBase],
        plugin_config: ModuleConfig,
        overrides: MutableMapping[str, Any],
    ) -> list[str]:
        """Reset any *armed* one-shot triggers for this plugin, persist-first,
        before its ``setup()`` runs. Returns the override keys durably consumed
        this boot (empty if none were armed or a persist failure forced a
        disarm), so the caller can reset their *loaded* value once the plugin
        has acted (see ``_reset_consumed_one_shot_values``).

        A one-shot field (``json_schema_extra={"one_shot": True}``) is a "do X
        once on next restart" toggle. We clear it from the overrides — on disk
        *first* — while leaving the armed value on ``plugin_config`` so the
        plugin acts this boot. Because the reset is durable before the action,
        a crash/power-loss mid-action can't make it re-fire next boot. If the
        reset can't be persisted, we instead *disarm* the in-memory value and
        skip it this boot, so an action never runs without a durable reset.
        """
        prefix = (
            "input_modules."
            if plugin_class.PLUGIN_TYPE == PluginType.INPUT_MODULE
            else "devices."
        ) + plugin_name + "."

        armed = find_one_shot_overrides(
            plugin_class.CONFIG_MODEL, prefix, plugin_config, overrides
        )
        if not armed:
            return []

        armed_set = set(armed)
        cleared = {k: v for k, v in overrides.items() if k not in armed_set}

        if self.overrides_file is not None:
            try:
                # Persist the reset FIRST; only then does the plugin act.
                save_overrides(self.overrides_file, cleared)
            except OSError as exc:
                logger.error(
                    "Could not persist one-shot reset to %s; disarming %s this "
                    "boot so the action doesn't run without a durable reset: %s",
                    self.overrides_file,
                    armed,
                    exc,
                )
                default_config = plugin_class.CONFIG_MODEL()
                for key in armed:
                    attrs = key[len(prefix):].split(".")
                    parent = plugin_config
                    try:
                        for part in attrs[:-1]:
                            parent = getattr(parent, part)
                        default_parent = default_config
                        for part in attrs[:-1]:
                            default_parent = getattr(default_parent, part)
                        setattr(
                            parent,
                            attrs[-1],
                            getattr(default_parent, attrs[-1]),
                        )
                    except (AttributeError, IndexError) as disarm_exc:
                        logger.error(
                            "Could not disarm one-shot '%s' after a persist "
                            "failure; it may still act this boot: %s",
                            key,
                            disarm_exc,
                        )
                # Disarmed, not consumed: the override stays on disk for a retry
                # and the loaded value is already back to default, so there's
                # nothing for the caller to reset.
                return []

        # When there's no overrides file the reset only lives in memory
        # (tests); say so rather than implying durability.
        reset_kind = "reset persisted" if self.overrides_file else "reset in memory only"
        for key in armed:
            overrides.pop(key, None)
            logger.warning(
                "One-shot override '%s' armed — consuming it this boot; %s.",
                key,
                reset_kind,
            )
        return armed

    def _reset_consumed_one_shot_values(
        self,
        plugin_name: str,
        plugin_class: type[PluginBase],
        plugin_config: ModuleConfig,
        consumed_keys: list[str],
    ) -> None:
        """Reset the *loaded* value of already-consumed one-shot triggers back
        to their field default, after the plugin has acted on them this boot.

        ``_consume_one_shot_overrides`` clears the override from disk but leaves
        the armed value on ``plugin_config`` so the plugin can act. That live
        object is what ``GET /server/config`` reports, so without this reset the
        trigger reads as still-armed for the rest of the process — the UI shows
        it "on" until the next restart rebuilds config from the (now-clean)
        overrides. Clearing it here keeps the loaded value in step with disk.
        """
        if not consumed_keys:
            return
        prefix = (
            "input_modules."
            if plugin_class.PLUGIN_TYPE == PluginType.INPUT_MODULE
            else "devices."
        ) + plugin_name + "."
        default_config = plugin_class.CONFIG_MODEL()
        for key in consumed_keys:
            if not key.startswith(prefix):
                continue
            attrs = key[len(prefix):].split(".")
            try:
                parent = plugin_config
                default_parent = default_config
                for part in attrs[:-1]:
                    parent = getattr(parent, part)
                    default_parent = getattr(default_parent, part)
                setattr(parent, attrs[-1], getattr(default_parent, attrs[-1]))
            except (AttributeError, IndexError) as exc:
                logger.error(
                    "Could not reset consumed one-shot '%s' on the loaded "
                    "config; it may read as still-armed until restart: %s",
                    key,
                    exc,
                )

    def _reconcile_consumed_overrides(
        self,
        plugin_name: str,
        plugin_class: type[PluginBase],
        plugin_config: ModuleConfig,
        overrides: MutableMapping[str, Any],
    ) -> int:
        """Sync the overrides dict with any mutations the plugin's setup
        applied to its in-memory config.

        Plugins are free to mutate fields on ``context.config`` during
        ``setup()`` — for example, a "do X on next start" toggle that
        clears itself after firing. Without this reconciliation those
        mutations would only live in memory: the override loaded from
        disk would re-fire on the next boot. Compare each override key
        targeting this plugin against the current in-memory value;
        update the dict to match, dropping keys whose value reverted
        to the type default. Returns the number of override entries
        added, modified, or removed so the caller can decide whether
        to persist the file.
        """
        prefix = (
            "input_modules."
            if plugin_class.PLUGIN_TYPE == PluginType.INPUT_MODULE
            else "devices."
        ) + plugin_name + "."

        default_config = plugin_class.CONFIG_MODEL()

        def _read(model: ModuleConfig, attrs: list[str]) -> Any:
            current: Any = model
            for part in attrs:
                current = getattr(current, part)
            return current

        changed = 0
        for key in list(overrides.keys()):
            if not key.startswith(prefix):
                continue
            attrs = key[len(prefix):].split(".")
            # One-shot triggers have their own lifecycle
            # (_consume_one_shot_overrides). Never reconcile them here: a
            # transient persist failure leaves the trigger armed on disk for a
            # retry, and reconcile must not drop it just because the plugin
            # disarmed the in-memory value.
            if is_one_shot_field(plugin_class.CONFIG_MODEL, attrs):
                continue
            try:
                current = _to_jsonable(_read(plugin_config, attrs))
                default = _to_jsonable(_read(default_config, attrs))
            except (AttributeError, IndexError, TypeError, ValueError):
                # The override targets a field that no longer exists or
                # is unreachable on the current model. Leave it alone —
                # apply_overrides_with_prefix already logged a warning
                # and skipped it; preserving the entry lets a future
                # schema revival pick it back up.
                continue
            stored = overrides[key]
            if current == stored:
                continue
            if current == default:
                del overrides[key]
            else:
                overrides[key] = current
            changed += 1
            secret = is_secret_path(plugin_class.CONFIG_MODEL, attrs)
            logger.info(
                "Reconciled override %s: %s → %s%s",
                key,
                loggable(stored, secret),
                loggable(current, secret),
                " (dropped, matches default)" if current == default else "",
            )
        return changed

    async def _scan_and_setup_plugins_from_entry_points(
        self,
        overrides: MutableMapping[str, Any],
    ) -> list[tuple[str, PreparedPlugin]]:
        """Scan for installed plugins and set them up, overlapping the slow
        ``setup()`` calls so startup is gated by the slowest plugin rather than
        the sum of all of them.

        A plugin's ``setup()`` can block for many seconds (a network login, a
        web-bundle download, spawning subprocesses). Awaiting them one after
        another — as this used to — serialised every plugin behind the slowest
        one and delayed the server from accepting connections. We instead fan
        the ``setup()`` calls out with ``asyncio.gather``.

        The override-mutating work stays sequential to avoid races on the
        shared ``overrides`` mapping and its on-disk persistence: per-plugin
        config building and one-shot consumption run *before* the fan-out, and
        reconciliation runs *after* it. Plugins are returned in entry-point
        order regardless of which finished first, so the default-module choice
        (first enabled) stays deterministic.
        """

        # Phase 1 (sequential, no awaiting): build each plugin's config and
        # context and consume any armed one-shot overrides before the plugin
        # acts on them. Touching ``overrides`` here, single-threaded, keeps the
        # shared mapping race-free.
        prepared: list[dict[str, Any]] = []
        for plugin_name, plugin_class in self._scan_entry_points():
            logger.info(f"Found plugin: {plugin_name}")
            entry: dict[str, Any] = {
                "name": plugin_name,
                "class": plugin_class,
                "config": None,
                "context": None,
                "error": None,
                "consumed_one_shot": [],
            }
            try:
                config = self._build_module_config(
                    plugin_name, plugin_class, overrides
                )
                entry["config"] = config
                # Reset armed one-shot triggers on disk *before* the plugin
                # acts, leaving the armed value on ``config`` for this boot.
                # Only when the module will actually run (setup() is skipped
                # for disabled modules) — otherwise the trigger waits, armed,
                # until the module is enabled rather than being silently lost.
                if getattr(config, "enabled", True):
                    entry["consumed_one_shot"] = self._consume_one_shot_overrides(
                        plugin_name, plugin_class, config, overrides
                    )
                entry["context"] = self._make_plugin_context(
                    plugin_name, plugin_class, config
                )
            except Exception as e:
                logger.error(
                    f"Failed to prepare plugin {plugin_name}: {e}", exc_info=True
                )
                entry["error"] = str(e)
            prepared.append(entry)

        # Phase 2 (concurrent): run the slow setup() calls in parallel. Entries
        # that failed to build a context in phase 1 are skipped here and handled
        # as errors below.
        async def _do_setup(entry: dict[str, Any]):
            if entry["context"] is None:
                return None
            return await PreparedPlugin.setup(entry["class"], entry["context"])

        results = await asyncio.gather(
            *(_do_setup(entry) for entry in prepared),
            return_exceptions=True,
        )

        # Phase 3 (sequential): fold results back into PreparedPlugins,
        # reconcile consumed overrides, and emit in entry-point order.
        out: list[tuple[str, PreparedPlugin]] = []
        for entry, result in zip(prepared, results):
            plugin_name = entry["name"]
            prepared_module: PreparedPlugin | None = None

            if isinstance(result, BaseException):
                logger.error(
                    f"Failed to setup plugin {plugin_name}: {result}",
                    exc_info=(type(result), result, result.__traceback__),
                )
                entry["error"] = str(result)
            elif result is not None:
                prepared_module = result

            # Create a PreparedPlugin with error state even if setup failed,
            # as long as we got far enough to have a config + context.
            if (
                prepared_module is None
                and entry["config"] is not None
                and entry["context"] is not None
            ):
                prepared_module = PreparedPlugin(
                    plugin_class=entry["class"],
                    plugin_instance=None,
                    health_state=ModuleHealthState.ERROR,
                    plugin_context=entry["context"],
                    interface=None,
                    error_message=entry["error"],
                )

            # Reconcile overrides regardless of READY/ERROR state: a
            # plugin that crashed midway through setup may still have
            # consumed an override before crashing (e.g. localfiles
            # purges the DB before raising) and we don't want that
            # consumption to repeat on every restart.
            if entry["config"] is not None:
                changed = self._reconcile_consumed_overrides(
                    plugin_name, entry["class"], entry["config"], overrides
                )
                if changed:
                    self.overrides_dirty = True
                # The plugin has now acted on any armed one-shot trigger; reset
                # its loaded value to default so GET /server/config reflects the
                # disarmed state immediately, matching the already-cleared disk
                # override instead of echoing the armed value until next restart.
                self._reset_consumed_one_shot_values(
                    plugin_name,
                    entry["class"],
                    entry["config"],
                    entry.get("consumed_one_shot") or [],
                )

            if prepared_module is not None:
                out.append((plugin_name, prepared_module))

        return out

    def _make_plugin_context(
        self,
        name: str,
        plugin_class: type[PluginBase],
        config: ModuleConfig,
    ) -> InputPluginContext | OutputDevicePluginContext:
        """Create a PluginContext instance."""
        if self.player_context is None:
            raise ValueError("PlayerContext is not set in PreparedModuleCollection")
        
        match plugin_class.PLUGIN_TYPE:
            case PluginType.INPUT_MODULE:
                return InputPluginContext(
                    playqueue=self.player_context.playqueue,
                    listener=self.player_context.playqueue_eventbus,  # type: ignore[arg-type]
                    logger=logging.getLogger(name),
                    plugin_id=name,
                    sdk_version=SDK_VERSION,
                    config=config,
                    embedder=self.player_context.embedder,
                )
            case PluginType.OUTPUT_DEVICE:
                router = self.player_context.device_router
                emitter = router.emitter_for(name) if router else None
                listener = (
                    router.listener_for(name, self.player_context.playqueue_eventbus)
                    if router
                    else None
                )
                return OutputDevicePluginContext(
                    listener=listener or self.player_context.playqueue_eventbus,  # type: ignore[arg-type]
                    emitter=emitter or self.player_context.ext_device_eventbus,  # type: ignore[arg-type]
                    logger=logging.getLogger(name),
                    plugin_id=name,
                    sdk_version=SDK_VERSION,
                    config=config,
                    embedder=self.player_context.embedder,
                )
            case _:
                raise ValueError(
                    f"Unsupported plugin type: {plugin_class.PLUGIN_TYPE}"
                )

    async def _setup_renderer_output_device(
        self,
        devices: dict[str, PreparedPlugin],
        overrides: Mapping[str, Any],
        renderer_registry: RendererRegistry,
        renderer_sessions: SessionPool,
    ) -> dict[str, PreparedPlugin]:
        """Register the built-in renderer volume device.

        Always registered: it is what controls every renderer that has not been
        delegated to another module, so an enabled MusicCast no longer displaces
        it — the two coexist and :class:`OutputDeviceRouter` picks per renderer.
        Its own ``enabled`` flag gates setup: disabled ⇒ no interface ⇒ no
        volume control surfaced to clients. It's built-in (not entry-point)
        because only the server can hand it the renderer services — injected
        via ``bind``.
        """
        if self.player_context is None:
            return devices

        config = self._build_module_config(
            "kalinka-renderer", RendererOutputPlugin, overrides
        )
        context = self._make_plugin_context(
            "kalinka-renderer", RendererOutputPlugin, config
        )

        if not getattr(config, "enabled", True):
            prepared = PreparedPlugin(
                plugin_class=RendererOutputPlugin,
                plugin_instance=None,
                health_state=ModuleHealthState.DISABLED,
                plugin_context=context,
                interface=None,
            )
            return {"kalinka-renderer": prepared, **devices}

        plugin = RendererOutputPlugin()
        plugin.bind(renderer_registry, renderer_sessions)
        try:
            await plugin.setup(context)
            prepared = PreparedPlugin(
                plugin_class=RendererOutputPlugin,
                plugin_instance=plugin,
                health_state=ModuleHealthState.READY,
                plugin_context=context,
                interface=plugin.get_interface(),
            )
        except Exception as e:
            logger.error(
                "Failed to set up renderer volume device: %s", e, exc_info=True
            )
            prepared = PreparedPlugin(
                plugin_class=RendererOutputPlugin,
                plugin_instance=None,
                health_state=ModuleHealthState.ERROR,
                plugin_context=context,
                interface=None,
                error_message=str(e),
            )
        return {"kalinka-renderer": prepared, **devices}

    async def scan_and_setup_plugins(
        self,
        player_context: PlayerContext,
        overrides: MutableMapping[str, Any],
        renderer_registry: RendererRegistry,
        renderer_sessions: SessionPool,
        overrides_file: str | None = None,
    ):
        """Scan for input modules from both entry points and legacy filesystem locations."""

        # First, scan for input modules using entry points
        self.player_context = player_context
        self.overrides_dirty = False
        self.overrides_file = overrides_file
        input_modules = {}
        devices = {}
        for (
            plugin_name,
            prepared_plugin,
        ) in await self._scan_and_setup_plugins_from_entry_points(overrides):
            plugin_type = prepared_plugin.plugin_class.PLUGIN_TYPE
            if plugin_type == PluginType.INPUT_MODULE:
                input_modules[plugin_name] = prepared_plugin
            elif plugin_type == PluginType.OUTPUT_DEVICE:
                devices[plugin_name] = prepared_plugin

        self.prepared_input_modules = {**input_modules}
        self._update_enabled_input_modules()

        devices = await self._setup_renderer_output_device(
            devices,
            overrides,
            renderer_registry,
            renderer_sessions,
        )
        self.prepared_devices = {**devices}


def volume_control_modules(devices: Mapping[str, PreparedPlugin]) -> list[str]:
    """Device modules a renderer's volume can be delegated to: enabled, live,
    and able to set volume. The renderer controlling its own volume is the
    absent mapping, so its own module is never a candidate."""
    return [
        name
        for name, prepared in devices.items()
        if name != RendererOutputPlugin.PLUGIN_ID
        and prepared.health_state == ModuleHealthState.READY
        and isinstance(prepared.interface, ExternalOutputDevice)
        and SupportedFunction.SET_VOLUME in prepared.interface.supported_functions()
    ]


modules = PreparedModuleCollection()


async def setup(
    config: KalinkaConfig,
    overrides: MutableMapping[str, Any],
    renderer_registry: RendererRegistry,
    renderer_prefs: RendererPreferences,
    renderer_sessions: SessionPool,
    overrides_file: str | None = None,
) -> PlayerContext:
    """Setup the player components.

    ``overrides`` is the user-set config map (loaded from the overrides
    file); only entries whose keys begin with ``input_modules.<name>.``
    or ``devices.<name>.`` will be applied to plugin configs. The dict
    is mutated in place when a plugin's setup consumes one of its
    overrides — callers inspect ``modules.overrides_dirty`` afterwards
    to decide whether to re-persist.
    """

    playqueue_eventbus=EventBus[PlayQueueState, PlayQueueEventType, PlayQueueEvent](  # type: ignore[type-var]
            initial_state=PlayQueueState(
                playback_state=PlaybackState(),
                track_list=[],
                playback_mode=PlaybackMode(
                    shuffle=False, repeat_single=False, repeat_all=False
                ),
            )
        )

    device_eventbus=EventBus[ExtDeviceState, ExtDeviceEventType, ExtDeviceEvent](  # type: ignore[type-var]
            initial_state=ExtDeviceState(power_on=False, volume=DeviceVolume()))

    # Create core components
    player_context = PlayerContext(
        playqueue_eventbus=playqueue_eventbus,
        playqueue=PlayQueueImpl(
            config, playqueue_eventbus, renderer_registry, renderer_sessions
        ),
        ext_device_eventbus=device_eventbus,
        embedder=SharedTextEmbedder(
            config.embedding.model_dir,
            config.embedding.model_url or None,
            # Pre-SDK-1.2 releases stored the model in the Jamendo plugin's
            # private directory; migrate instead of re-downloading ~90 MB.
            legacy_dirs=[os.path.join(paths.state_dir(), "jamendo", "minilm")],
        ),
        )

    # Built before the plugin scan: each device plugin is handed an emitter
    # that only reaches clients while that plugin owns the active renderer.
    player_context.device_router = OutputDeviceRouter(
        renderer_registry,
        renderer_prefs,
        lambda: modules.prepared_devices,
        device_eventbus,
    )

    # Scan and setup plugins
    await modules.scan_and_setup_plugins(
        player_context,
        overrides,
        renderer_registry,
        renderer_sessions,
        overrides_file,
    )

    logger.info("Input modules found: %s", list(modules.prepared_input_modules.keys()))
    logger.info("Output devices found: %s", list(modules.prepared_devices.keys()))

    return player_context


async def shutdown_modules(modules: dict[str, PreparedPlugin]):
    """Shutdown all modules."""
    for module_name, prepared_module in modules.items():
        logger.info(f"Shutting down module: {module_name}")
        if prepared_module.plugin_instance is not None:
            try:
                await prepared_module.plugin_instance.shutdown()
            except Exception as e:
                logger.error(f"Error shutting down module {module_name}: {e}")
        else:
            logger.info(f"Module {module_name} was not initialized, skipping shutdown.")


async def shutdown():
    """Shutdown all plugin modules."""
    global modules

    await shutdown_modules(modules.prepared_input_modules)
    await shutdown_modules(modules.prepared_devices)

