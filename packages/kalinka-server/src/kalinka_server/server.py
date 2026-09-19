import asyncio
import json
import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.staticfiles import StaticFiles

from kalinka_plugin_sdk.datamodel import (
    BrowseItem,
    BrowseItemList,
    Catalog,
    EntityId,
    EntityType,
    FavoriteIds,
    PlaybackState,
    PlayerStateEnum,
)
from kalinka_plugin_sdk.ext_device import DeviceVolume
from kalinka_plugin_sdk.ext_device_events import ExtDeviceEventType
from kalinka_plugin_sdk.inputmodule import InputModule, SearchType, TrackInfo
from kalinka_plugin_sdk.events import (
    CurrentRendererChangedEvent,
    PlayQueueEventType,
    RenderersChangedEvent,
)
from kalinka_plugin_sdk import ConfigIssue, paths

from .config_model import KalinkaConfig
from .config_overrides import save_overrides
from .config_schema_processor import (
    build_enum_options,
    build_presentation,
    build_values,
    get_field_value,
)
from .catalog_art_service import CatalogArtService
from .browse_route import register_browse_routes
from .browse_source import BrowseSource, BrowseSourceRegistry, RegisteredSource
from .collections.route import register_collection_routes
from .collections.source import CollectionsSource
from .collections.store import CollectionStore
from .content_route import register_content_route
from .queue_add import track_infos_for
from .search_route import register_search_routes
from .suggestions import SuggestionEngine, SuggestionList
from .merge_utils import get_favorite_ids_merged, k_way_merge_browse_items
from .config_validation import (
    ConfigTargets,
    ConfigWriteError,
    apply_change,
    blocking,
    changes_from_payload,
    validate_changes,
)
from .dynamic_field_registry import build_dynamic_field_registry
from .options_registry import OptionsRegistry, register_plugin_options
from .web_ui import WebUiStaticFiles
from .optional_packages_registry import (
    build_catalog as build_optional_packages_catalog,
    write_pending_installs,
)
from .player_setup import (
    modules,
    setup,
    shutdown,
    ModuleHealthState,
    volume_control_modules,
)
from .internal_modules import internal_modules
from .module_capabilities import capabilities_of
from .service_discovery import ServiceDiscovery
from . import update_check
from .version import get_rest_api_version, get_version
from .state_keeper import save_state, restore_state
from .queue_ws_handler import (
    handle_websocket_connection as handle_queue_websocket_connection,
)
from .device_ws_handler import (
    handle_websocket_connection as handle_device_websocket_connection,
)
from .renderer_link import RendererLink
from .renderer_ws_handler import handle_renderer_connection
from .renderer_config import RendererConfigService
from .renderer_core_settings import (
    DEVICE_MODULE_PATH,
    RENDERER_ITSELF,
    CoreRendererSettings,
)
from .renderer_prefs import RendererPreferences
from .renderer_registry import RendererRegistry, RendererUnavailable
from .renderer_sessions import (
    RendererBusy,
    SessionNotActive,
    SessionOpenFailed,
    SessionPool,
)
from .renderer_upgrade import RendererUpgradeService, UpgradeRefused
from .renderer_test_tone import (
    TONE_DIR,
    TONE_MOUNT_NAME,
    TONE_ROUTE,
    TonePlayer,
    tone_channel,
    tone_filename,
    tone_url,
)
from .server_identity import get_server_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    sd = None
    try:
        await app.state.collections.open()

        sd = ServiceDiscovery(app.state.config, bind_host=app.state.bind_host)
        await sd.register_service()

        # Initialize internal modules (device automation, etc.)
        await internal_modules.initialize(
            app.state.config,
            app.state.player_context,
            app.state.device_router,
        )

        await restore_state(
            app.state.player_context.playqueue,
            {
                name: module.interface
                for name, module in modules.prepared_input_modules.items()
                if name in modules.enabled_input_modules
                and isinstance(module.interface, InputModule)
            },
        )
        await app.state.player_context.playqueue.__aenter__()

        yield

    except Exception as e:
        logger.error(f"Error: {e}")

    finally:
        logger.info("Shutting down...")
        # These loops run forever by design; cancel them and swallow the result.
        for attr in ("suggestions_task", "catalog_art_task", "update_check_task"):
            task = getattr(app.state, attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    # A crashed loop must not derail the rest of shutdown.
                    pass
        art = getattr(app.state, "catalog_art", None)
        if art is not None:
            await art.close()
        collections = getattr(app.state, "collections", None)
        if collections is not None:
            await collections.close()
        # Finalize sessions and fire their close callbacks. uvicorn has already
        # closed the renderer sockets by now, so the renderer itself only
        # learns the session is gone from the STALE reconciliation at its next
        # Hello.
        test_tone = getattr(app.state, "test_tone", None)
        if test_tone is not None:
            await test_tone.shutdown()
        renderer_sessions = getattr(app.state, "renderer_sessions", None)
        if renderer_sessions is not None:
            await renderer_sessions.shutdown()
        renderer_registry = getattr(app.state, "renderer_registry", None)
        if renderer_registry is not None:
            await renderer_registry.shutdown()
        if sd is not None:
            await sd.unregister_service()

        # Shutdown internal modules first
        await internal_modules.shutdown()

        # Then shutdown plugins, including the built-in renderer volume device.
        await shutdown()

        app.state.player_context.playqueue_eventbus.close()
        await save_state(app.state.player_context.playqueue_eventbus)
        await app.state.player_context.playqueue.__aexit__(None, None, None)


logger = logging.getLogger(__name__.split(".")[-1])

SourceType = Query(..., description="Name of the input module")


def parse_entity_id(id: str) -> EntityId:
    """Dependency to parse entity ID from string."""
    try:
        return EntityId.from_string(id)
    except (ValueError, Exception) as e:
        logger.error(f"Failed to parse entity ID '{id}': {str(e)}")
        raise HTTPException(
            status_code=400, detail=f"Invalid entity ID format: {str(e)}"
        )


_browse_registry: Optional[BrowseSourceRegistry] = None


def browse_registry() -> BrowseSourceRegistry:
    """Every source a read request may reach. Built once the plugins are
    scanned, so nothing routes before there is something to route to."""
    if _browse_registry is None:
        raise HTTPException(status_code=500, detail="No browse sources available")
    return _browse_registry


def browse_source_from_id(entity_id: str | EntityId) -> BrowseSource:
    """The source that answers for `entity_id`, whether a plugin provides it
    or the server does."""
    parsed = (
        EntityId.from_string(entity_id) if isinstance(entity_id, str) else entity_id
    )
    entry = browse_registry().get(parsed.source)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"Source '{parsed.source}' is not available"
        )
    return entry.source


def _named_sources(sources: str) -> List[str]:
    """The names a ``sources`` parameter holds, refusing one that names none.

    A blank value used to fall through to "every source", which is how a
    client could still ask for the merged listing that per-source routes
    exist to replace — and a blank segment is a client that built its
    parameter wrong, not a source.
    """
    names = [name.strip() for name in sources.split(",")]
    if any(not name for name in names):
        raise HTTPException(
            status_code=422, detail="`sources` must name at least one source"
        )
    return names


def extract_browse_sources(sources: Optional[str]) -> List[BrowseSource]:
    """The browse sources named by `sources`, or every one of them when it is
    absent altogether.

    A name nobody knows is refused rather than dropped: a listing assembled
    from the sources that happened to be recognised looks complete and is
    not, which is the thing the per-source routes exist to avoid.
    """
    entries = browse_registry().entries()
    if sources is None:
        return [entry.source for entry in entries]

    wanted = _named_sources(sources)
    by_name = {entry.name: entry.source for entry in entries}
    unknown = [name for name in wanted if name not in by_name]
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=f"No such source: {', '.join(unknown)}",
        )
    return [by_name[name] for name in wanted]


def input_module(name: str) -> InputModule:
    """Get the currently active input module."""
    if not modules.prepared_input_modules:
        raise HTTPException(status_code=500, detail="No input modules available")

    if name not in modules.prepared_input_modules:
        raise HTTPException(status_code=404, detail=f"Input module '{name}' not found")

    interface = modules.prepared_input_modules[name].interface
    if isinstance(interface, InputModule):
        return interface

    raise HTTPException(
        status_code=500,
        detail="Input module interface is not initialized or of wrong type",
    )


def enabled_input_module(name: str) -> InputModule:
    """The input module `name`, provided the user has it enabled.

    Content from a module that is disabled — or was never installed — is absent
    rather than a server fault, so neither may reach the 500 `input_module`
    raises for an interface that has been torn down.
    """
    if name not in modules.enabled_input_modules:
        raise HTTPException(
            status_code=404, detail=f"Input module '{name}' is not available"
        )
    return input_module(name)


def input_module_from_id(entity_id: str | EntityId) -> InputModule:
    """Get the input module based on the entity ID."""
    if isinstance(entity_id, str):
        entityId = EntityId.from_string(entity_id)
    else:
        entityId = entity_id

    source = entityId.source
    if source not in modules.prepared_input_modules:
        raise HTTPException(
            status_code=404, detail=f"Input module '{source}' not found"
        )

    if source not in modules.enabled_input_modules:
        raise HTTPException(
            status_code=404, detail=f"Input module '{source}' is disabled"
        )

    interface = modules.prepared_input_modules[source].interface
    if isinstance(interface, InputModule):
        return interface

    raise HTTPException(
        status_code=500,
        detail="Input module interface is not initialized or of wrong type",
    )


def default_input_module() -> InputModule:
    """Get the default input module."""
    if not modules.prepared_input_modules:
        raise HTTPException(status_code=500, detail="No input modules available")

    default_module = modules.prepared_input_modules[
        next(iter(modules.enabled_input_modules))
    ]

    if isinstance(default_module.interface, InputModule):
        return default_module.interface

    raise HTTPException(
        status_code=500,
        detail="Default input module interface is not initialized or of wrong type",
    )


def extract_modules(sources: Optional[str]) -> List[InputModule]:
    """The input modules named by `sources`, or every enabled one when it is
    absent altogether."""
    if sources is None:
        return [
            module.interface
            for module_name, module in modules.prepared_input_modules.items()
            if module_name in modules.enabled_input_modules
            and isinstance(module.interface, InputModule)
        ]

    sources_split = _named_sources(sources)
    input_modules: list[InputModule] = []
    for source in sources_split:
        # Named explicitly or not, a disabled module is not one to reach:
        # asking for it by name is how the check used to be skipped.
        if source in modules.enabled_input_modules:
            interface = modules.prepared_input_modules[source].interface
            if isinstance(interface, InputModule):
                input_modules.append(interface)

    if not input_modules and not _all_browsable(sources_split):
        raise HTTPException(status_code=404, detail="No matching input modules found")

    return input_modules


def _all_browsable(names: Sequence[str]) -> bool:
    """True when every name is a source the server knows.

    A source that browses but streams nothing — collections — has no leg in
    suggestions or favourites, so asking for it there is an empty answer
    rather than a missing module.
    """
    registry = _browse_registry
    if registry is None:
        return False
    return all(registry.get(name) is not None for name in names)


async def create_app(
    overrides_file: str,
    config: KalinkaConfig,
    overrides: Dict[str, Any],
    *,
    bind_host: str | None = None,
):
    app = FastAPI(lifespan=lifespan)
    app.state.config = config
    app.state.bind_host = bind_host
    app.state.overrides_file = overrides_file
    app.state.overrides = dict(overrides)
    # Renderer services exist before the play queue: playback runs through a
    # renderer session, so the queue needs the registry and the session pool.
    async def _replace_renderer_session(old_session: RendererLink):
        await old_session.replace()

    renderer_prefs = RendererPreferences(
        os.path.join(paths.state_dir(), "renderers.json")
    )
    renderer_registry = RendererRegistry(
        replace_session=_replace_renderer_session, prefs=renderer_prefs
    )
    renderer_sessions = SessionPool(renderer_registry, get_server_id())
    renderer_registry.set_on_removed(renderer_sessions.handle_renderer_removed)
    renderer_configs = RendererConfigService(renderer_registry)
    renderer_upgrades = RendererUpgradeService(
        renderer_registry,
        lambda renderer_id: renderer_sessions.get(renderer_id) is not None,
    )
    app.state.renderer_registry = renderer_registry
    app.state.renderer_sessions = renderer_sessions
    app.state.renderer_configs = renderer_configs
    app.state.renderer_upgrades = renderer_upgrades

    player_context = await setup(
        config,
        app.state.overrides,
        renderer_registry,
        renderer_prefs,
        renderer_sessions,
        app.state.overrides_file,
    )
    logger.info("Input modules found: %s", list(modules.prepared_input_modules.keys()))
    app.state.player_context = player_context

    # Renderer topology rides the queue event bus, so clients stop polling
    # /renderer/list to notice a renderer coming or going.
    renderer_registry.set_on_changed(
        renderers=lambda rows: player_context.playqueue_eventbus.dispatch(
            RenderersChangedEvent(renderers=rows)
        ),
        current=lambda active, selected: player_context.playqueue_eventbus.dispatch(
            CurrentRendererChangedEvent(
                renderer_id=active, selected_renderer_id=selected
            )
        ),
    )

    # Search-suggestion engine: validated against the user's own library
    # (localfiles) when it is enabled — a discovery catalog like Jamendo has
    # everything, so validating against it proves nothing. Attestation runs
    # in the background for the server's lifetime; serving never blocks.
    library = None
    lf = modules.prepared_input_modules.get("localfiles")
    if (
        lf is not None
        and "localfiles" in modules.enabled_input_modules
        and isinstance(lf.interface, InputModule)
    ):
        library = lf.interface
    app.state.suggestions = SuggestionEngine(library, config.search)
    app.state.suggestions_task = asyncio.create_task(
        app.state.suggestions.refresh_loop()
    )

    # Collections are the server's own source: browsable and searchable by
    # name, with no audio of its own. Writes have their own API.
    app.state.collections = CollectionStore(
        os.path.join(paths.state_dir(), "collections.db")
    )

    def _plugin_sources() -> List[RegisteredSource]:
        entries: List[RegisteredSource] = []
        for name in modules.enabled_input_modules:
            plugin = modules.prepared_input_modules[name]
            if not isinstance(plugin.interface, InputModule):
                continue
            # The title /server/modules shows — reads the Pydantic `title`
            # declared on the config's `name` field, so a source is named the
            # same wherever it appears.
            config = plugin.plugin_context.config
            entries.append(
                RegisteredSource(
                    name=name,
                    title=config.__class__.model_fields["name"].title or config.name,
                    source=plugin.interface,
                )
            )
        return entries

    def _source_title(name: str) -> str:
        entry = _browse_registry.get(name) if _browse_registry else None
        return entry.title if entry is not None else name

    global _browse_registry
    collections_source = CollectionsSource(app.state.collections, _source_title)
    _browse_registry = BrowseSourceRegistry(
        _plugin_sources,
        [
            RegisteredSource(
                name=collections_source.module_name(),
                title=collections_source.display_name(),
                source=collections_source,
                builtin=True,
            )
        ],
    )

    # Composed catalog-card backgrounds, generated lazily off the browse path.
    # Both resolvers return None for absent sources (no raise): the first
    # browses the catalog being drawn, the second reads a child's cover off
    # disk, which only a plugin can do.
    def _art_browse_resolver(entity_id: EntityId) -> Optional[BrowseSource]:
        entry = _browse_registry.get(entity_id.source) if _browse_registry else None
        return entry.source if entry is not None else None

    def _art_resource_resolver(entity_id: EntityId) -> Optional[InputModule]:
        source = entity_id.source
        if source not in modules.enabled_input_modules:
            return None
        plugin = modules.prepared_input_modules.get(source)
        interface = plugin.interface if plugin is not None else None
        return interface if isinstance(interface, InputModule) else None

    app.state.catalog_art = CatalogArtService(
        os.path.join(paths.cache_dir(), "catalog_art"),
        _art_browse_resolver,
        _art_resource_resolver,
    )
    app.state.catalog_art_task = asyncio.create_task(app.state.catalog_art.run())

    # Hourly release check backing GET /server/update; reads the auto-upgrade
    # toggle live so a settings change applies without restart. Auto-upgrade
    # additionally requires stopped playback (None: nothing ever played).
    async def _playback_stopped() -> bool:
        playback = await app.state.player_context.playqueue.get_playback_state()
        return playback.state in (PlayerStateEnum.STOPPED, None)

    async def _renderers_ready() -> bool:
        """Renderers go first: a release can move the protocol they speak, and
        one left behind on another machine has to be reachable to be fixed."""
        return await renderer_upgrades.bring_forward(
            update_check.checker.latest_renderer
        )

    app.state.update_check_task = asyncio.create_task(
        update_check.checker.run(
            lambda: app.state.config.server.auto_upgrade,
            _playback_stopped,
            _renderers_ready,
        )
    )

    # Persist the overrides dict if plugin setup reconciled it — i.e. a
    # plugin mutated config fields that came from the overrides file, so
    # ``app.state.overrides`` now diverges from disk. (One-shot triggers
    # are handled separately, persist-first *before* setup, so they're not
    # what this covers.)
    if modules.overrides_dirty:
        try:
            save_overrides(app.state.overrides_file, app.state.overrides)
            logger.info(
                "Persisted reconciled overrides to %s", app.state.overrides_file,
            )
        except OSError as exc:
            logger.error(
                "Failed to persist reconciled overrides to %s: %s",
                app.state.overrides_file,
                exc,
            )

    # Plugin classes are fixed for the process lifetime, so the dynamic-
    # field registry and the schema_version are stable. Compute once
    # here and reuse on every request; the alternative (rebuilding on
    # every GET /server/config and twice per PUT) wastes measurable
    # work for a quantity that never changes.
    app.state.dynamic_field_registry = build_dynamic_field_registry(
        modules.prepared_input_modules, modules.prepared_devices,
    )
    # Options registry — resolvers for writable fields whose choices
    # depend on live system state (shares on the network, drives plugged
    # into the box). Sits alongside the dynamic-field registry but for
    # *choices* rather than *values*; bound to the plugins below, once
    # the schema that declares which fields want them exists.
    app.state.options_registry = OptionsRegistry()
    _initial_ok_in = {
        name: m.plugin_context.config
        for name, m in modules.prepared_input_modules.items()
        if m.health_state != ModuleHealthState.ERROR
    }
    _initial_err_in = {
        name: (m.plugin_context.config, m.error_message or "Unknown error")
        for name, m in modules.prepared_input_modules.items()
        if m.health_state == ModuleHealthState.ERROR
    }
    _initial_ok_dev = {
        name: d.plugin_context.config
        for name, d in modules.prepared_devices.items()
        if d.health_state != ModuleHealthState.ERROR
    }
    _initial_err_dev = {
        name: (d.plugin_context.config, d.error_message or "Unknown error")
        for name, d in modules.prepared_devices.items()
        if d.health_state == ModuleHealthState.ERROR
    }
    _initial_schema = build_presentation(
        base_config=config,
        input_modules=_initial_ok_in,
        devices=_initial_ok_dev,
        input_modules_with_errors=_initial_err_in,
        devices_with_errors=_initial_err_dev,
        dynamic_field_registry=app.state.dynamic_field_registry,
    )
    app.state.schema_version = _initial_schema.schema_version
    register_plugin_options(
        app.state.options_registry,
        _initial_schema,
        modules.prepared_input_modules,
        modules.prepared_devices,
    )
    app.state.dynamic_paths = frozenset(app.state.dynamic_field_registry.keys())
    # Volume and power are answered by whichever module owns the *active*
    # renderer, so the target is resolved per request rather than bound here.
    device_router = player_context.device_router
    assert device_router is not None  # setup() always builds one
    app.state.device_router = device_router
    # Every session opens with the volume policy its renderer's wiring implies.
    renderer_sessions.set_volume_policy(device_router.session_volume_policy)
    # Under /server so the browser-player mount at "/" cannot shadow it, and
    # check_dir off so a packaging slip costs the speaker test, not the boot.
    app.mount(
        TONE_ROUTE,
        StaticFiles(directory=TONE_DIR, check_dir=False),
        name=TONE_MOUNT_NAME,
    )
    test_tone = TonePlayer(
        renderer_registry,
        renderer_sessions,
        lambda rid: app.state.player_context.playqueue.release_renderer(rid),
    )
    app.state.test_tone = test_tone

    @app.get("/queue/list")
    async def read_queue_list(offset: int = 0, limit: int = 10):
        return await player_context.playqueue.list(offset=offset, limit=limit)

    @app.post("/queue/add")
    async def add_entity_to_queue(ids: list[str], index: Optional[int] = None):
        items: list[TrackInfo] = await track_infos_for(
            ids, browse_source_from_id, enabled_input_module
        )

        await player_context.playqueue.add(items, index)
        return {"message": "Items added to queue", "count": len(items)}

    @app.put("/queue/play")
    async def queue_play(index: Union[int, None] = None):
        await player_context.playqueue.play(index)
        return {"message": "Ok"}

    @app.put("/queue/pause")
    async def queue_pause(paused: bool = True):
        await player_context.playqueue.pause(paused)
        return {"message": "Ok"}

    @app.put("/queue/next")
    async def queue_next():
        await player_context.playqueue.next()
        return {"message": "Ok"}

    @app.put("/queue/prev")
    async def queue_prev():
        await player_context.playqueue.prev()
        return {"message": "Ok"}

    @app.put("/queue/stop")
    async def queue_stop():
        await player_context.playqueue.stop()
        return {"message": "Ok"}

    @app.put("/queue/current_track/seek")
    async def queue_seek(position_ms: int):
        value = await player_context.playqueue.seek(position_ms)
        return {"message": "Ok", "position_ms": value}

    @app.get("/browse")
    def browse_root(offset: int = 0, limit: int = 10):
        """Browse the root catalog."""

        items = []
        for entry in browse_registry().entries():
            entity_id = EntityId(
                id="root", type=EntityType.CATALOG, source=entry.name
            )
            items.append(
                BrowseItem(
                    id=entity_id,
                    name=entry.title,
                    url=f"/browse/{entity_id.to_string}",
                    can_browse=True,
                    can_add=False,
                    catalog=Catalog(
                        id=entity_id,
                        title=entry.title,
                        description=(
                            "" if entry.builtin else "Kalinka Input Module"
                        ),
                    ),
                )
            )

        result = BrowseItemList(
            offset=offset, limit=limit, total=len(items), items=items
        )
        return result.model_dump(exclude_unset=True)

    register_browse_routes(
        app,
        browse_source_from_id,
        parse_entity_id,
        app.state.catalog_art.decorate,
    )

    @app.get("/catalog/art/{file_name}")
    async def get_catalog_art(file_name: str):
        """Serve a generated catalog-card background. The name is fingerprinted,
        so the bytes at a URL never change — hence the immutable cache header."""
        path = app.state.catalog_art.art_file(file_name)
        if path is None:
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    register_search_routes(
        app,
        extract_browse_sources,
        extract_modules,
        lambda: app.state.config.search,
        app.state.catalog_art.decorate,
    )

    register_collection_routes(
        app, app.state.collections, browse_source_from_id, enabled_input_module
    )

    @app.get("/ai_search/suggestions")
    async def ai_search_suggestions(
        count: int = Query(
            default=8, ge=1, le=32,
            description="How many suggestions to return",
        ),
        tz_offset_min: Optional[int] = Query(
            default=None, ge=-720, le=840,
            description=(
                "Client's UTC offset in MINUTES, east positive (UTC+3 = 180, "
                "UTC-5 = -300; Dart: DateTime.now().timeZoneOffset.inMinutes) "
                "— so 'morning' means the listener's morning, not the "
                "server's. Omitted: the server's local clock decides."
            ),
        ),
    ) -> SuggestionList:
        """Ready-to-run ``/ai_search`` queries matched to the current moment
        (daypart + in-window holidays), validated against the local library,
        plus one experimental slot."""
        now = None
        if tz_offset_min is not None:
            now = datetime.now(timezone(timedelta(minutes=tz_offset_min)))
        return app.state.suggestions.suggest(count, now=now)

    @app.get("/indexer/status")
    async def indexer_status(sources: Optional[str] = None) -> dict:
        """Return pipeline progress (indexing / enrichment / embedding
        stages) for each configured input module."""
        input_modules: List[InputModule] = extract_modules(sources)
        result = {}
        for module in input_modules:
            if hasattr(module, "get_indexer_status"):
                try:
                    status = await module.get_indexer_status()
                    result[module.module_name()] = status
                except Exception:
                    pass
        return result

    @app.get("/queue/events")
    async def stream(request: Request):
        async def process_events():
            async with player_context.playqueue_eventbus.stream(
                list(PlayQueueEventType)
            ) as stream:
                try:
                    async for event in stream:
                        if await request.is_disconnected():
                            break

                        if event is not None:
                            yield event.model_dump_json() + "\n"

                except asyncio.CancelledError:
                    logger.debug("Event stream cancelled")
                    return
                except Exception as e:
                    logger.error(f"Error processing events: {e}")
                    yield json.dumps({"error": str(e)}) + "\n"

        return StreamingResponse(process_events(), media_type="text/event-stream")

    @app.get("/device/events")
    async def device_stream(request: Request):
        async def process_device_events():
            async with player_context.ext_device_eventbus.stream(
                list(ExtDeviceEventType)
            ) as stream:
                try:
                    async for event in stream:
                        if await request.is_disconnected():
                            break

                        if event is not None:
                            yield event.model_dump_json() + "\n"

                except asyncio.CancelledError:
                    logger.debug("Device event stream cancelled")
                    return
                except Exception as e:
                    logger.error(f"Error processing device events: {e}")
                    yield json.dumps({"error": str(e)}) + "\n"

        return StreamingResponse(
            process_device_events(), media_type="text/event-stream"
        )

    @app.get("/queue/state")
    async def state() -> PlaybackState:
        return await player_context.playqueue.get_playback_state()

    @app.get("/queue/mode")
    async def mode():
        return await player_context.playqueue.get_playback_mode()

    @app.put("/queue/mode")
    async def set_mode(
        shuffle: Optional[bool] = None,
        repeat_single: Optional[bool] = None,
        repeat_all: Optional[bool] = None,
    ):
        await player_context.playqueue.set_playback_mode(
            shuffle, repeat_single, repeat_all
        )
        return {"message": "Ok"}

    @app.put("/queue/clear")
    async def clear():
        await player_context.playqueue.clear()
        return {"message": "Ok"}

    @app.post("/queue/remove")
    async def remove(index: int):
        await player_context.playqueue.remove([index])
        return {"message": "Ok"}

    @app.put("/queue/move")
    async def queue_move(from_index: int, to_index: int):
        await player_context.playqueue.move(from_index, to_index)
        return {"message": "Ok"}

    @app.get("/device/list")
    async def device_supported_functions():
        device = device_router.current()
        if device is None:
            return {"message": "No device configured"}

        return device.supported_functions()

    @app.get("/device/get_volume")
    async def get_volume() -> DeviceVolume:
        device = device_router.current()
        if device is None:
            return DeviceVolume(supported=False)

        return await device.get_volume()

    @app.put("/device/set_volume")
    async def set_volume(volume: int):
        device = device_router.current()
        if device is None:
            return {"message": "No device configured"}

        await device.set_volume(volume)
        return {"message": "Ok"}

    @app.get("/favorite/list/{type}")
    async def list_favorite(
        type: SearchType,
        filter: str = "",
        sources: Optional[str] = None,
        offset: int = 0,
        limit: int = 10,
    ):
        """List favorites of a specific type."""
        input_modules: list[InputModule] = extract_modules(sources)

        return await k_way_merge_browse_items(
            [partial(module.list_favorite, type, filter) for module in input_modules],
            compared_value=lambda item: item.timestamp,
            offset=offset,
            limit=limit,
        )

    @app.put("/favorite/add/{id}")
    async def add_favorite(id: str):
        await input_module_from_id(id).add_to_favorite(id)
        return {"message": "Ok"}

    @app.delete("/favorite/remove/{id}")
    async def remove_favorite(id: str):
        await input_module_from_id(id).remove_from_favorite(id)
        return {"message": "Ok"}

    @app.get("/favorite/ids")
    async def get_favorite_ids(source: Optional[str] = None) -> FavoriteIds:
        input_modules: list[InputModule] = extract_modules(source)

        return await get_favorite_ids_merged(modules=input_modules)

    @app.get("/get/{entity_id}")
    async def entity_get(entity_id: str):
        try:
            entity_id_obj = EntityId.from_string(entity_id)

            return (
                await browse_source_from_id(entity_id_obj).get(entity_id_obj)
            ).model_dump(exclude_unset=True)

        except (ValueError, Exception) as e:
            logger.error(f"Failed to parse entity ID '{entity_id}': {str(e)}")
            raise HTTPException(
                status_code=400, detail=f"Invalid entity ID format: {str(e)}"
            )

    @app.post("/playlist/create")
    async def playlist_create(name: str, description: str, source: str = "localfiles"):
        return (
            await input_module(source).playlist_create(name, description)
        ).model_dump(exclude_unset=True)

    @app.put("/playlist/update")
    async def playlist_update(
        playlist_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        return (
            await input_module_from_id(playlist_id).playlist_update(
                playlist_id, name, description
            )
        ).model_dump(exclude_unset=True)

    @app.delete("/playlist/delete")
    async def playlist_delete(playlist_id: str):
        await input_module_from_id(playlist_id).playlist_delete(playlist_id)
        return {"message": "Ok"}

    @app.post("/playlist/add_tracks")
    async def playlist_add_tracks(
        playlist_id: str, track_ids: List[str], allow_duplicates: bool = True
    ):
        return (
            await input_module_from_id(playlist_id).playlist_add_tracks(
                playlist_id, track_ids, allow_duplicates
            )
        ).model_dump(exclude_unset=True)

    @app.get("/playlist/list")
    async def playlist_user_list(
        sources: Optional[str] = None, offset: int = 0, limit: int = 25
    ):
        listings: list[BrowseSource] = extract_browse_sources(sources)

        return await k_way_merge_browse_items(
            [source.playlist_user_list for source in listings],
            compared_value=lambda item: item.timestamp,
            offset=offset,
            limit=limit,
        )

    @app.delete("/playlist/remove_tracks")
    async def playlist_remove_tracks(playlist_id: str, playlist_track_ids: List[str]):
        return (
            await input_module_from_id(playlist_id).playlist_remove_tracks(
                playlist_id, playlist_track_ids
            )
        ).model_dump(exclude_unset=True)

    def _partition_modules_and_devices():
        successful_input_modules = {
            name: m.plugin_context.config
            for name, m in modules.prepared_input_modules.items()
            if m.health_state != ModuleHealthState.ERROR
        }
        failed_input_modules = {
            name: (m.plugin_context.config, m.error_message or "Unknown error")
            for name, m in modules.prepared_input_modules.items()
            if m.health_state == ModuleHealthState.ERROR
        }
        successful_devices = {
            name: d.plugin_context.config
            for name, d in modules.prepared_devices.items()
            if d.health_state != ModuleHealthState.ERROR
        }
        failed_devices = {
            name: (d.plugin_context.config, d.error_message or "Unknown error")
            for name, d in modules.prepared_devices.items()
            if d.health_state == ModuleHealthState.ERROR
        }
        return (
            successful_input_modules,
            failed_input_modules,
            successful_devices,
            failed_devices,
        )

    @app.get("/server/config/schema")
    def get_config_schema():
        ok_in, err_in, ok_dev, err_dev = _partition_modules_and_devices()
        schema = build_presentation(
            base_config=config,
            input_modules=ok_in,
            devices=ok_dev,
            input_modules_with_errors=err_in,
            devices_with_errors=err_dev,
            dynamic_field_registry=app.state.dynamic_field_registry,
        )
        return schema.model_dump(mode="json")

    @app.get("/server/config")
    async def get_config():
        ok_in, _err_in, ok_dev, _err_dev = _partition_modules_and_devices()
        values = await build_values(
            base_config=config,
            input_modules=ok_in,
            devices=ok_dev,
            dynamic_entries=app.state.dynamic_field_registry.values(),
        )
        # Resolve dynamic-options enums (ALSA devices etc.) per-request
        # so hot-plug is reflected without a schema bump. Each entry is
        # a list of {value, label} option specs.
        enum_options = await build_enum_options(app.state.options_registry)
        return {
            "schema_version": app.state.schema_version,
            "values": values,
            "enum_options": {
                path: [opt.model_dump(mode="json") for opt in opts]
                for path, opts in enum_options.items()
            },
        }

    @app.get("/server/version")
    def get_version_info():
        """Get version information for the server."""
        return {
            "server_version": get_version(),
            "api_version": get_rest_api_version(),
            "name": "kalinka-player",
        }

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
        if not await _renderers_ready():
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

    @app.put("/server/restart")
    async def restart_server(payload: Optional[Dict[str, Any]] = None):
        """Request a full systemd-driven restart of the server.

        Before triggering the restart, the server polls every loaded
        plugin's ``required_packages()`` and unions the result with an
        optional ``{"install": [...]}`` body. Any allowed keys land in
        ``/var/lib/kalinka/pending_installs.json`` so the bootstrap-time
        install runs automatically on the way back up. This keeps the
        optional-packages mechanism invisible from the user's perspective:
        flipping a sub-feature toggle and clicking restart just works,
        with a longer restart while the install completes.

        The explicit body is kept as a power-user override (and for
        future "retry failed install" affordances). Unknown keys in
        the body are rejected with 400; keys returned by plugins'
        ``required_packages()`` but absent from the registry are
        dropped with a log line (the plugin is misconfigured, but the
        restart itself should still proceed).
        """
        # Explicit body keys
        body_requested: list[str] = []
        if payload and isinstance(payload, dict):
            raw = payload.get("install") or []
            if raw and not isinstance(raw, list):
                raise HTTPException(
                    status_code=400, detail="'install' must be a list of keys"
                )
            body_requested = [str(k) for k in raw]

        # Auto-collect from plugins' required_packages(). Defensive: a
        # misbehaving plugin must not block the restart.
        auto_keys: list[str] = []
        for prepared in list(modules.prepared_input_modules.values()) + list(
            modules.prepared_devices.values()
        ):
            instance = prepared.plugin_instance
            if instance is None:
                continue
            try:
                keys = await instance.required_packages()
            except Exception as e:  # noqa: BLE001 — defensive
                logger.warning(
                    "Plugin %s required_packages() raised; ignoring: %s",
                    prepared.plugin_class.PLUGIN_ID,
                    e,
                )
                continue
            for k in keys or []:
                if k not in auto_keys:
                    auto_keys.append(str(k))

        # Union, body first (so explicit user intent ordering wins for
        # display) then auto-detected.
        combined: list[str] = []
        seen: set[str] = set()
        for k in list(body_requested) + list(auto_keys):
            if k not in seen:
                seen.add(k)
                combined.append(k)

        accepted: list[str] = []
        rejected: list[str] = []
        if combined:
            try:
                accepted, rejected = write_pending_installs(
                    combined,
                    modules.prepared_input_modules,
                    modules.prepared_devices,
                )
            except OSError as e:
                logger.error("Failed to write pending installs: %s", e)
                raise HTTPException(
                    status_code=500,
                    detail="Failed to record install request",
                ) from e
            # Body-provided keys must be valid (user-facing API). Keys
            # we auto-collected from plugins are best-effort — drop with
            # a log if the plugin is misconfigured.
            body_rejected = [k for k in rejected if k in body_requested]
            auto_rejected = [k for k in rejected if k not in body_requested]
            if auto_rejected:
                logger.warning(
                    "Plugin required_packages() returned keys not in the "
                    "registry; dropping: %s",
                    auto_rejected,
                )
            if body_rejected:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Unknown optional-package keys",
                        "rejected": body_rejected,
                        "accepted": accepted,
                    },
                )
            if accepted:
                logger.info(
                    "Queued optional packages for next-boot install: %s",
                    accepted,
                )

        # Touch the trigger file watched by kalinka-restart.path. systemd's
        # path unit fires the (root-owned) kalinka-restart.service oneshot,
        # which runs `systemctl restart kalinka.service`. We exit only when
        # systemd sends SIGTERM, letting the lifespan teardown run normally.
        trigger = Path(paths.run_dir()) / "restart-request"
        try:
            trigger.parent.mkdir(parents=True, exist_ok=True)
            trigger.touch()
        except OSError as e:
            logger.error("Failed to write restart trigger %s: %s", trigger, e)
            raise HTTPException(
                status_code=500, detail="Failed to request restart"
            ) from e
        return {"message": "restarting", "install_queued": accepted}

    @app.post("/server/test_tone")
    async def server_test_tone(
        request: Request, payload: Optional[Dict[str, Any]] = None
    ):
        """Play a test tone on one channel of a renderer.

        Body: `{"channel": "left"|"right"|"both", "renderer_id": "<id>"}`.
        Without `renderer_id` — the setup wizard sends a legacy `device`
        instead, naming an ALSA setting that no longer exists — the tone goes
        to the active renderer.

        Playback on that renderer stops first: the tone needs the renderer to
        itself, and a queue left running would be reported as playing while
        something else is audible.

        The tone URL is formed from the address the renderer dialed to register:
        the caller's own route to this server proves nothing about the
        renderer's. The request is the fallback when that address is unknown.

        Never answers 404 or 405. Older clients read either as "this server
        cannot play tones at all" and tell the user to upgrade, which would be
        the wrong advice for an unknown renderer.
        """
        payload = payload or {}
        channel = tone_channel(str(payload.get("channel", "both")))
        renderer_id = payload.get("renderer_id") or renderer_registry.active_id()
        if not renderer_id:
            raise HTTPException(
                status_code=503, detail="No renderer is connected"
            )
        record = renderer_registry.get(renderer_id)
        if record is None:
            raise HTTPException(
                status_code=409, detail="Unknown or disconnected renderer"
            )
        source_url = (
            tone_url(record.server_addr, channel)
            if record.server_addr is not None
            else str(request.url_for(TONE_MOUNT_NAME, path=tone_filename(channel)))
        )
        try:
            await test_tone.play(renderer_id, channel, source_url)
        except RendererBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (RendererUnavailable, SessionNotActive) as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except (SessionOpenFailed, asyncio.TimeoutError) as exc:
            raise HTTPException(
                status_code=504, detail=str(exc) or "the renderer did not answer"
            )
        return {"message": "Ok", "channel": channel}

    @app.get("/server/optional_packages")
    def get_optional_packages():
        """Return the catalog of plugin-declared optional packages.

        Each entry includes pip_spec, description, import_name, whether
        the package resolves in the current process, and whether the key
        is in the pending-installs queue. Which packages a *running*
        plugin currently needs is surfaced via
        ``GET /server/modules`` → ``missing_packages`` on each module.
        """
        return build_optional_packages_catalog(
            modules.prepared_input_modules,
            modules.prepared_devices,
        )

    async def _module_entry(prepared) -> Dict[str, Any]:
        """Roll-up live state for one prepared plugin.

        If the plugin failed to set up (or is disabled in config), the
        setup-time health_state and error_message are authoritative.
        Otherwise call plugin_instance.get_state() so plugins with
        internal sub-features can report WARNING + a message + the
        optional-package keys they're missing.
        """
        cfg = prepared.plugin_context.config
        entry: Dict[str, Any] = {
            "name": cfg.name,
            "title": cfg.__class__.model_fields["name"].title or cfg.name,
            "enabled": cfg.enabled,
            "state": prepared.health_state.value,
            "error_message": prepared.error_message,
            "missing_packages": [],
            "builtin": False,
            "capabilities": capabilities_of(prepared.interface),
        }
        if (
            prepared.health_state == ModuleHealthState.READY
            and prepared.plugin_instance is not None
        ):
            try:
                live = await prepared.plugin_instance.get_state()
                # Unconditional override: when health_state is READY the
                # setup-time error_message is always None, so we replace
                # it with whatever the live state says (which can also
                # legitimately be None when the plugin is happy).
                entry["state"] = live.state.value
                entry["error_message"] = live.message
                entry["missing_packages"] = list(live.missing_packages)
            except Exception as e:  # noqa: BLE001 — defensive
                logger.warning(
                    "Plugin %s get_state() raised; falling back to setup-time "
                    "state: %s",
                    cfg.name,
                    e,
                )
        return entry

    def _builtin_entries() -> List[Dict[str, Any]]:
        """Sources the server provides itself.

        They carry no config, no packages and no failure of their own, and are
        listed so a client can browse and search them — and skip the planes
        they do not have.
        """
        return [
            {
                "name": entry.name,
                "title": entry.title,
                "enabled": True,
                "state": ModuleHealthState.READY.value,
                "error_message": None,
                "missing_packages": [],
                "builtin": True,
                # Browsed and searched by name like any source, but with no
                # audio of its own there is nothing for it to suggest.
                "capabilities": [],
            }
            for entry in browse_registry().entries()
            if entry.builtin
        ]

    @app.get("/server/modules")
    async def list_modules():
        input_entries = [
            await _module_entry(m)
            for m in modules.prepared_input_modules.values()
        ]
        device_entries = [
            await _module_entry(d) for d in modules.prepared_devices.values()
        ]
        return {
            "input_modules": [*input_entries, *_builtin_entries()],
            "devices": device_entries,
        }

    def _config_targets() -> ConfigTargets:
        return ConfigTargets(
            base_config=app.state.config,
            input_modules=modules.prepared_input_modules,
            devices=modules.prepared_devices,
        )

    async def _judge(
        payload: Dict[str, Any],
    ) -> tuple[Dict[str, Any], list[ConfigIssue]]:
        """The changes in a config write body and everything wrong with them.

        The dry run and the save ask exactly this, so neither can accept
        what the other would refuse.
        """
        try:
            changes = changes_from_payload(
                payload, app.state.schema_version, app.state.dynamic_paths
            )
            return changes, await validate_changes(changes, _config_targets())
        except ConfigWriteError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=str(exc)
            ) from exc

    @app.post("/server/config/validate")
    async def validate_config_fields(payload: Dict[str, Any]):
        """Judge staged changes without keeping any of them.

        Body is the one `PUT /server/config` takes. The answer is what the
        save would say, so the settings page can show it while the user is
        still typing.
        """
        _changes, issues = await _judge(payload)
        return {"issues": [issue.model_dump(mode="json") for issue in issues]}

    @app.put("/server/config")
    async def set_config_fields(payload: Dict[str, Any]):
        """Apply staged changes.

        Body: `{"schema_version": "...", "changes": {"<dotted.path>": value, ...}}`.
        Paths are relative to one of the three roots: `base_config.*`,
        `input_modules.<name>.*`, or `devices.<name>.*`. No `root.` or
        `.fields.` wrappers.

        Refused whole when any change draws an error, so a batch the user
        staged together never lands in halves. What was only warned about is
        applied and reported, because a client that saved without a dry run
        has nowhere else to learn of it.
        """
        changes, issues = await _judge(payload)
        refused = blocking(issues)
        if refused:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": refused[0].message,
                    "issues": [issue.model_dump(mode="json") for issue in issues],
                },
            )

        applied: Dict[str, Any] = {}
        targets = _config_targets()
        try:
            for key, value in changes.items():
                logger.info("Setting config field %s to %r", key, value)
                try:
                    target = targets.resolve(key)
                    reason = apply_change(target.model, target.attrs, value)
                except ConfigWriteError as exc:
                    logger.warning("Invalid config key '%s': %s", key, exc)
                    raise HTTPException(
                        status_code=exc.status_code, detail=str(exc)
                    ) from exc
                if reason is not None:
                    raise HTTPException(status_code=400, detail=reason)
                applied[key] = value
                logger.info(
                    "Set %s to %r, saved: %r",
                    key,
                    value,
                    get_field_value(target.model, target.attrs),
                )
        finally:
            # A batch that draws an error is refused above, before anything
            # is written. Past that point a change can still fail only for a
            # reason the check could not reach, and what already went into
            # memory cannot be taken back out — so it is persisted, and a
            # restart matches what is running.
            if applied:
                app.state.overrides.update(applied)
                try:
                    save_overrides(app.state.overrides_file, app.state.overrides)
                except OSError as exc:
                    logger.error(
                        "Failed to persist overrides to %s: %s",
                        app.state.overrides_file,
                        exc,
                    )

        return {
            "message": "Ok",
            "schema_version": app.state.schema_version,
            "issues": [issue.model_dump(mode="json") for issue in issues],
        }

    @app.get("/resource/{file_name:path}")
    async def get_resource(file_name: str):
        file_path = None
        for module_name in modules.enabled_input_modules:
            file_path = await input_module(module_name).get_resource_path(file_name)
            if file_path:
                resolved_path = Path(file_path).resolve()
                if resolved_path.is_file():
                    mime_type, _ = mimetypes.guess_type(str(file_path))
                    return FileResponse(resolved_path, media_type=mime_type)

        if not file_path:
            raise HTTPException(status_code=404, detail="File not found")

        resolved_path = Path(file_path).resolve()
        if not resolved_path or not resolved_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        mime_type, _ = mimetypes.guess_type(str(file_path))

        return FileResponse(resolved_path, media_type=mime_type)

    register_content_route(app, enabled_input_module)

    @app.websocket("/queue/ws")
    async def queue_websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time playback control and event streaming."""
        await handle_queue_websocket_connection(
            websocket, player_context.playqueue_eventbus, player_context.playqueue
        )

    @app.websocket("/device/ws")
    async def device_websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for device control and event streaming."""
        await handle_device_websocket_connection(
            websocket, player_context.ext_device_eventbus, device_router.current
        )

    @app.websocket("/renderer/ws")
    async def renderer_websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for native renderers (binary protobuf)."""
        await handle_renderer_connection(
            websocket,
            config,
            renderer_registry,
            renderer_sessions,
            renderer_configs,
            renderer_upgrades,
        )

    def _volume_control_modules() -> List[str]:
        return volume_control_modules(modules.prepared_devices)

    def _device_module_label(name: str) -> str:
        prepared = modules.prepared_devices.get(name)
        config = getattr(getattr(prepared, "plugin_context", None), "config", None)
        if config is None:
            return name
        field = config.__class__.model_fields.get("name")
        return (field.title if field is not None else None) or config.name or name

    # The Core's own settings for a renderer, answered with the renderer's.
    core_renderer_settings = CoreRendererSettings(
        renderer_prefs,
        lambda: [
            (name, _device_module_label(name)) for name in _volume_control_modules()
        ],
        device_router.resync,
    )

    @app.get("/renderer/list")
    async def renderer_list():
        """Known renderers, their connection status, and which module controls
        each one's volume, with the modules available to be picked."""
        renderers = renderer_registry.list()
        behind = {
            candidate.renderer_id
            for candidate in renderer_upgrades.candidates(
                update_check.checker.latest_renderer
            )
        }
        for row in renderers:
            row["volume_control"] = renderer_prefs.volume_control(row["renderer_id"])
            row["update_available"] = row["renderer_id"] in behind
        return {
            "renderers": renderers,
            "volume_control_modules": _volume_control_modules(),
        }

    @app.post("/renderer/{renderer_id}/upgrade")
    async def renderer_upgrade(renderer_id: str):
        """Ask one renderer to install the latest published release.

        Answers as soon as the renderer has taken the request on; it then
        restarts and re-registers, which is what reports the outcome. Refused
        while a playback session is running on it, and for a renderer that
        cannot install a release of itself.
        """
        latest = update_check.checker.latest_renderer
        if not latest:
            raise HTTPException(
                status_code=409, detail="No renderer release is known yet"
            )
        try:
            detail = await renderer_upgrades.upgrade(renderer_id, latest)
        except RendererUnavailable as e:
            raise HTTPException(status_code=404, detail=str(e))
        except UpgradeRefused as e:
            raise HTTPException(status_code=409, detail=str(e))
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="The renderer did not answer")
        return {"renderer_id": renderer_id, "version": latest, "detail": detail}

    @app.get("/renderer/sessions")
    async def renderer_session_list():
        """Playback sessions this Core holds."""
        return {"server_id": get_server_id(), "sessions": renderer_sessions.list()}

    def _renderer_selection() -> Dict[str, Any]:
        return {
            "renderer_id": renderer_registry.active_id(),
            "selected_renderer_id": renderer_registry.selected_id,
        }

    @app.get("/renderer/active")
    async def renderer_active():
        """The renderer playback runs on: `renderer_id` is the effective one,
        `selected_renderer_id` the client's pin (null = automatic)."""
        return _renderer_selection()

    @app.put("/renderer/active")
    async def renderer_select(payload: Dict[str, Any]):
        """Pin playback to a renderer.

        Body: `{"renderer_id": "<id>"}`, or null to return to automatic
        (first connected). Playback moves with it and resumes from its current
        position. A target that is busy or unreachable fails the request and
        leaves playback and the selection where they were.
        """
        renderer_id = payload.get("renderer_id")
        if renderer_id is not None and renderer_registry.get(renderer_id) is None:
            raise HTTPException(status_code=404, detail="Unknown renderer")
        try:
            await app.state.player_context.playqueue.switch_renderer(renderer_id)
        except RendererBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except RendererUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except (SessionOpenFailed, asyncio.TimeoutError) as exc:
            raise HTTPException(
                status_code=504, detail=str(exc) or "the renderer did not answer"
            )
        await device_router.resync()
        return _renderer_selection()

    @app.put("/renderer/{renderer_id}/volume-control")
    async def renderer_volume_control(renderer_id: str, payload: Dict[str, Any]):
        """Delegate this renderer's volume to another device module.

        Body: `{"module": "<plugin id>"}`, or null to hand control back to the
        renderer itself — the default for every renderer. Delegation belongs to
        a renderer whose output is wired into that device (an amp downstream),
        which is why it is per renderer and not a module-wide setting.
        """
        if renderer_registry.get(renderer_id) is None:
            raise HTTPException(status_code=404, detail="Unknown renderer")
        module = payload.get("module")
        outcome = (
            await core_renderer_settings.apply(
                renderer_id, {DEVICE_MODULE_PATH: module or RENDERER_ITSELF}
            )
        )[0]
        if not outcome["applied"]:
            raise HTTPException(status_code=400, detail=outcome["error"])
        return {"renderer_id": renderer_id, "volume_control": module}

    # Renderer settings are the renderer's own, so they are not part of
    # /server/config: they need no session, several Cores may edit them, and a
    # renderer coming or going must not churn the server's schema_version.
    @app.get("/renderer/{renderer_id}/config")
    async def renderer_config_get(renderer_id: str):
        """Settings schema and values, fetched from the renderer on demand.

        What the Core keeps about the renderer — which device module its output
        is wired into — is answered alongside, so one page shows both.
        """
        snapshot = await _renderer_config_call(renderer_configs.get(renderer_id))
        return core_renderer_settings.merge_into(snapshot, renderer_id)

    @app.put("/renderer/{renderer_id}/config")
    async def renderer_config_put(renderer_id: str, payload: Dict[str, Any]):
        """Apply settings; the reply says what ended up in effect.

        Body: `{"changes": {"<path>": "<value>", ...}}`, with the paths as they
        came from the GET. Unlike /server/config there is no schema_version
        precondition: writes name individual paths, so a settings page that went
        stale cannot overwrite a field it did not touch.

        Core-owned paths are applied here and never reach the renderer, so the
        device mapping can be set while the renderer is offline.
        """
        changes = payload.get("changes", payload)
        if not isinstance(changes, dict) or not changes:
            raise HTTPException(status_code=400, detail="No changes given")
        if renderer_registry.get(renderer_id) is None:
            raise HTTPException(status_code=404, detail="Unknown renderer")
        ours, theirs = core_renderer_settings.split(changes)
        outcomes = await core_renderer_settings.apply(renderer_id, ours)
        if not theirs:
            return {
                "config_version": "",
                "effect": "instant",
                "outcomes": outcomes,
            }
        result = await _renderer_config_call(
            renderer_configs.update(renderer_id, theirs)
        )
        result["outcomes"] = outcomes + result["outcomes"]
        return result

    async def _renderer_config_call(awaitable):
        try:
            return await awaitable
        except RendererUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="renderer did not answer")

    # Browser player (optional kalinka-web package). Mounted last so every API
    # route above wins; check_dir=False resolves per request, so installing the
    # bundle after startup works without a server restart.
    app.mount(
        "/",
        WebUiStaticFiles(directory=paths.web_ui_dir(), html=True, check_dir=False),
        name="web-ui",
    )

    return app
