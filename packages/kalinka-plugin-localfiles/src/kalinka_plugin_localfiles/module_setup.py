import asyncio
import importlib.util
import logging
import logging.handlers
import multiprocessing
import shutil
import threading
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Optional

from kalinka_plugin_sdk import (
    ConfigIssue,
    ConfigOption,
    DynamicFieldDecl,
    IssueSeverity,
    ModuleConfig,
    ModuleHealthState,
    ModuleState,
    OptionalPackageSpec,
)
from kalinka_plugin_sdk.plugin import InputPluginContext, InputModulePlugin
from kalinka_plugin_sdk.inputmodule import InputModule

from .config_model import LocalFilesConfig
from .db_schema import init_db
from .storage import (
    LocatorError,
    RootStatus,
    StorageResolver,
    build_resolver,
    parse,
)
from .suggest import (
    LocalMountSuggester,
    RootSuggester,
    SmbHostDiscovery,
    SmbHostSuggester,
)
from .input_module_db import LocalFilesInputModuleDb
from .localfiles import LocalFilesInputModule
from .optional_packages import OPTIONAL_PACKAGES
from . import librarian
from . import embedder
from . import searcher


logger = logging.getLogger(__name__.split(".")[-1])

#: How long one music folder may take to answer before the settings page
#: gives up on it. Short, because every folder is probed on every poll and
#: the page waits for the slowest of them.
_PROBE_TIMEOUT_S = 2.0


@dataclass
class _SubfeatureBookkeeping:
    """Internal-to-the-plugin record of one sub-feature's runtime state.

    Not exposed on any API; rolled up into ModuleState by get_state().
    """

    title: str
    required: bool
    state: ModuleHealthState = ModuleHealthState.READY
    message: str = ""
    # Optional-package keys this sub-feature would have needed but couldn't
    # find — surfaced in the status message and used to suggest installs.
    missing_packages: tuple[str, ...] = field(default_factory=tuple)


def _is_importable(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError):
        return False


def _format_subfeature_status(sf: "_SubfeatureBookkeeping") -> str:
    """Render a sub-feature's state as the markdown the UI displays."""
    if sf.state == ModuleHealthState.READY:
        return f"**Ready** — {sf.title.lower()} is running normally."
    if sf.state == ModuleHealthState.DISABLED:
        msg = sf.message or "Disabled in configuration."
        return f"**Disabled** — {msg}"
    if sf.state == ModuleHealthState.WARNING:
        return f"**Warning** — {sf.message or 'Degraded operation.'}"
    if sf.state == ModuleHealthState.ERROR:
        return f"**Not available** — {sf.message or 'Unknown error.'}"
    return sf.message or ""


def _quiet_share_protocol_logs() -> None:
    """Keep ``smbprotocol``'s narration out of an ordinary service log.

    It reports every negotiate, tree connect and file open at INFO, which is
    most of the log once a share is indexed. A debug run is left alone, since
    that is where the detail is wanted.
    """
    if not logging.getLogger().isEnabledFor(logging.DEBUG):
        logging.getLogger("smbprotocol").setLevel(logging.WARNING)


def _mount_mismatch(status: RootStatus, stored_signature: Optional[str]) -> bool:
    """True when the folder's current mount is not the one the library was
    indexed from — a static share silently gave way to the local directory
    underneath it."""
    return bool(
        status.available
        and stored_signature
        and status.identity
        and status.identity != stored_signature
    )


def _format_root_status(
    status: RootStatus,
    stored_signature: Optional[str],
    scan_interval_minutes: int,
) -> str:
    """Render one music folder's mount status as the markdown the UI displays."""
    if _mount_mismatch(status, stored_signature):
        text = (
            f"**Not available** — `{status.root}`: the filesystem the library "
            f"was indexed from ({stored_signature}) is not mounted."
        )
    elif status.available:
        kind = (
            f"{status.fs_type} network share" if status.is_network else "local folder"
        )
        if status.is_autofs:
            kind += ", automounted"
        text = f"**Available** — `{status.root}` ({kind})."
    else:
        text = f"**Not available** — `{status.root}`: {status.reason}."
    notes = []
    if status.is_autofs:
        notes.append(
            "autofs mounts on demand and unmounts when idle, which can delay "
            "or fail the first playback after a pause. A static mount (or an "
            "autofs idle timeout of 0) avoids this."
        )
    if status.is_network:
        notes.append(
            "Changes made to the share by other machines are picked up by the "
            f"periodic rescan (every {scan_interval_minutes} min), not "
            "instantly."
        )
    if notes:
        text += "\n" + "\n".join(f"- {note}" for note in notes)
    return text


async def _probe_roots(
    resolver: StorageResolver, roots: list[str]
) -> list[RootStatus]:
    """Every root's availability, asked for at once.

    Serially would make the page wait for the sum of the timeouts, and an
    unreachable share is exactly the case where that is worst.
    """
    return await asyncio.gather(
        *(
            resolver.for_path(root).probe_root(root, timeout=_PROBE_TIMEOUT_S)
            for root in roots
        )
    )


def _judge_spelling(
    folders: list[str], report: bool
) -> tuple[list[ConfigIssue], list[tuple[int, str]]]:
    """Each folder's canonical form, and what is wrong with how it is written.

    Off the event loop, because canonicalising a local folder resolves it and
    one on a hung mount would hold up every request behind it.

    @param report Whether a misspelling is worth saying anything about. It is
        only when the user is editing the list; otherwise a folder that was
        already wrong would refuse a save that has nothing to do with it.
    @return The issues, and the ``(index, root)`` pairs worth probing.
    """
    issues: list[ConfigIssue] = []
    first_written_at: dict[str, int] = {}
    probing: list[tuple[int, str]] = []

    for index, folder in enumerate(folders):
        try:
            locator = parse(folder)
        except LocatorError as e:
            if report:
                issues.append(
                    ConfigIssue(path="music_folders", index=index, message=str(e))
                )
            continue
        # A share logs in with the SMB credentials alone for now; a folder
        # already naming its user keeps working, it just cannot be added.
        if locator.username is not None and report:
            issues.append(
                ConfigIssue(
                    path="music_folders",
                    index=index,
                    message=(
                        "a user name does not belong in a folder URL; write "
                        f"{replace(locator, username=None)} and set the user "
                        "in the SMB credentials"
                    ),
                )
            )
            continue
        root = str(locator)
        first = first_written_at.get(root)
        if first is not None:
            if report:
                issues.append(
                    ConfigIssue(
                        path="music_folders",
                        index=index,
                        message=f"the same folder as entry {first + 1}",
                    )
                )
            continue
        first_written_at[root] = index
        probing.append((index, root))
    return issues, probing


class _ResolverCache:
    """One storage resolver per credential set, kept rather than rebuilt.

    The registry that holds a hung folder to a single blocked worker thread
    lives on the storage instances, so a resolver built per call brings an
    empty one with it and strands a thread on every poll.

    @note One cache per purpose, never one shared: the resolver playback
        reads must not be swapped for one built from credentials the user
        has typed but not saved. Each keeps its own logins, so neither can
        answer for the other's password.

    @param release_replaced Whether a resolver being swapped out can have
        its connections taken away at once. True where nothing reads
        through it — each password typed into the settings page is a
        credential set of its own, and every one of them would otherwise
        leave a connection and the thread reading it behind. False where a
        track may still be streaming from it: a saved credential change is
        followed by a restart within seconds, which is a cheaper way to
        release those than closing a file somebody is reading.
    """

    def __init__(self, release_replaced: bool) -> None:
        self._release_replaced = release_replaced
        self._resolver: Optional[StorageResolver] = None
        self._key: Optional[str] = None

    def get(self, config: LocalFilesConfig) -> StorageResolver:
        key = config.smb.model_dump_json()
        if self._resolver is None or key != self._key:
            if self._release_replaced:
                self._release(self._resolver)
            self._resolver = build_resolver(config)
            self._key = key
        return self._resolver

    def close(self) -> None:
        self._release(self._resolver)
        self._resolver = None
        self._key = None

    @staticmethod
    def _release(resolver: Optional[StorageResolver]) -> None:
        """Hand a replaced resolver's connections back.

        On a thread of its own: this is reached from the event loop while
        the user edits a password, and disconnecting from a server that has
        stopped answering takes as long as the protocol allows.
        """
        if resolver is None:
            return
        threading.Thread(
            target=resolver.close, name="storage-release", daemon=True
        ).start()


class KalinkaPluginLocalFiles(InputModulePlugin):
    REQUIRES_SDK = ">=3.3,<4"
    PLUGIN_ID = "localfiles"
    CONFIG_MODEL = LocalFilesConfig
    OPTIONAL_PACKAGES: ClassVar[dict[str, OptionalPackageSpec]] = OPTIONAL_PACKAGES

    # Dynamic-field declarations consumed by the server's schema builder.
    # Paths here are the keys passed to resolve_dynamic_field(). Section
    # ids are relative to the module — the server prepends the module's
    # full path prefix when locating the target section in the schema.
    DYNAMIC_FIELDS: ClassVar[dict[str, DynamicFieldDecl]] = {
        "enricher.status_view": DynamicFieldDecl(
            section_id="enricher",
            label="Status",
            widget="rich_text",
            value_type="str",
        ),
        "ai_search.status_view": DynamicFieldDecl(
            section_id="ai_search",
            label="Status",
            widget="rich_text",
            value_type="str",
        ),
        # Beside the API key: a key alone does nothing without the binary,
        # and the enricher's own status is one section further out than
        # anyone looks for this.
        "acoustid.status_view": DynamicFieldDecl(
            section_id="enricher.plugins.acoustid",
            label="Status",
            widget="rich_text",
            value_type="str",
        ),
        "storage.status_view": DynamicFieldDecl(
            section_id="",
            label="Music folders status",
            widget="rich_text",
            value_type="str",
        ),
    }

    def __init__(self):
        # The indexer and enricher share one "librarian" process (single DB
        # writer); the searcher and embedder stay separate.
        self._librarian_proc = None
        self._searcher_proc = None
        self._embedder_proc = None
        self._searcher_nudge_queue = multiprocessing.Queue()
        self._embedder_nudge_queue = multiprocessing.Queue()
        self._logging_queue = multiprocessing.Queue()
        self._search_request_queue = multiprocessing.Queue()
        self._search_response_queue = multiprocessing.Queue()
        self._text_encode_request_queue = multiprocessing.Queue()
        self._text_encode_response_queue = multiprocessing.Queue()
        self._log_listener = None
        self._inputmodule = None

        # Context captured at setup() so methods called by the server
        # later (get_state, required_packages, resolve_dynamic_field)
        # can read the *current* in-memory config — which the server
        # mutates via PUT /server/config without re-invoking setup.
        self._context: Optional[InputPluginContext] = None

        # What playback and the status page read, and — separately — what
        # folders the user has typed but not saved are judged with.
        self._live_resolvers = _ResolverCache(release_replaced=False)
        self._staged_resolvers = _ResolverCache(release_replaced=True)

        # Where the settings page's folder suggestions come from. The
        # discovery listens on the network for as long as the module is
        # loaded; the storages cannot hold it, because they are rebuilt in
        # every worker process and this belongs in one.
        self._discovery = SmbHostDiscovery()
        self._suggesters: list[RootSuggester] = []

        # Per-sub-feature state used by get_state() and resolve_dynamic_field().
        # Populated during setup() based on actual subprocess start outcomes
        # and import probing for optional packages.
        self._subfeatures: dict[str, _SubfeatureBookkeeping] = {
            "indexer": _SubfeatureBookkeeping(
                # Not required — direct file playback works against the
                # existing DB even if the indexer is down. A dead indexer
                # surfaces as a module-level WARNING, not an ERROR.
                title="File indexer",
                required=False,
            ),
            "enricher": _SubfeatureBookkeeping(
                title="Metadata enricher", required=False
            ),
            "ai_search": _SubfeatureBookkeeping(
                title="AI search", required=False
            ),
            "acoustid": _SubfeatureBookkeeping(title="AcoustID", required=False),
        }

    def get_interface(self) -> Optional[InputModule]:
        return self._inputmodule

    async def setup(self, context: InputPluginContext) -> None:
        self._context = context
        config = LocalFilesConfig(**context.config.model_dump())
        logger.info("Setting up localfiles input module")
        _quiet_share_protocol_logs()

        input_module_db = LocalFilesInputModuleDb(config)

        # "Rebuild library on next restart" is a one-shot trigger: the server
        # has already reset it (persist-first) before this boot, leaving the
        # armed value here for us to act on exactly once. Purge before any
        # worker opens the DB so the indexer rebuilds it from scratch.
        if config.rebuild_library:
            logger.warning(
                "Rebuild requested — purging DB and artwork before scan."
            )
            input_module_db.purge_all()

        # The queues are the module's "is there a searcher?" test, so hand
        # them over only when one will actually run: a request nobody reads
        # blocks ai_search() for its full 30 s timeout.
        search_queues = (
            (self._search_request_queue, self._search_response_queue)
            if config.ai_search.enabled
            else (None, None)
        )

        # The LocalFilesInputModule will use its own specialized DB, and
        # takes its storage from here so playback and the settings page
        # cannot end up on different credentials.
        self._inputmodule = LocalFilesInputModule(
            config,
            input_module_db,
            *search_queues,
            storage_source=self._current_resolver,
        )

        # Forward subprocess log records into the main logging pipeline so the
        # main kalinka-server handlers (and their levels/formatters) decide what
        # to emit. This avoids a parallel StreamHandler that would bypass the
        # server's log level filtering.
        class SubprocessForwardingHandler(logging.Handler):
            def emit(self, record):
                target = logging.getLogger(record.name)
                if target.isEnabledFor(record.levelno):
                    target.handle(record)

        handler = SubprocessForwardingHandler()

        # In Python 3.14, respect_handler_level defaults to True; we keep it
        # False for backward compatibility and to let the target logger handle
        # level filtering.
        self._log_listener = logging.handlers.QueueListener(
            self._logging_queue, handler, respect_handler_level=False
        )
        self._log_listener.start()

        # Centralised schema init — runs once in the main process before
        # any subprocess starts, so there is no lock contention.
        await init_db(config.db_path)

        # Indexer + enricher run in one process, wired by an in-process queue.
        # It nudges the searcher and the embedder as work lands; the enricher
        # leg only runs if config.enricher.enabled. Without AI search there is
        # no embedder to drain its queue, so it gets none rather than an
        # unbounded backlog of wake-ups.
        self._librarian_proc = multiprocessing.Process(
            target=librarian.main,
            args=(
                config,
                self._logging_queue,
                self._searcher_nudge_queue,
                self._embedder_nudge_queue if config.ai_search.enabled else None,
            ),
        )
        self._librarian_proc.start()

        # AI search is served by two processes: the searcher answers queries,
        # the embedder indexes the library and encodes query text for the
        # searcher over IPC, so only one process loads the ~600 MB CLAP model.
        # Neither is useful alone, so one config flag starts both.
        if config.ai_search.enabled:
            self._searcher_proc = multiprocessing.Process(
                target=searcher.main,
                args=(
                    config,
                    self._logging_queue,
                    self._search_request_queue,
                    self._search_response_queue,
                    self._searcher_nudge_queue,
                    self._embedder_nudge_queue,
                    self._text_encode_request_queue,
                    self._text_encode_response_queue,
                ),
            )
            self._searcher_proc.start()

            self._embedder_proc = multiprocessing.Process(
                target=embedder.main,
                args=(
                    config,
                    self._logging_queue,
                    self._embedder_nudge_queue,
                    self._text_encode_request_queue,
                    self._text_encode_response_queue,
                ),
            )
            self._embedder_proc.start()

        # Off the loop both ways: joining a multicast group and leaving it
        # again are socket work, and zeroconf refuses to do the leaving at
        # all when it is asked for it from inside an event loop.
        await asyncio.to_thread(self._discovery.start)
        self._suggesters = [
            LocalMountSuggester(),
            SmbHostSuggester(self._discovery),
        ]

        # Populate sub-feature state from what was (or wasn't) just started
        # and from import probing. This runs in the main process — it
        # doesn't catch later subprocess deaths, but covers the common
        # "package was never installed" failure path.
        await self._evaluate_subfeatures(config)

    # ------------------------------------------------------------------
    # Sub-feature bookkeeping (internal — never exposed directly)
    # ------------------------------------------------------------------

    async def _evaluate_subfeatures(self, config: LocalFilesConfig) -> None:
        """Decide initial state for each sub-feature based on config + imports.

        Best-effort: `is_alive()` immediately after `Process.start()` is
        racy on both edges (kernel may not have scheduled yet; or child
        may have already crashed during import). We yield briefly so the
        OS has a chance to schedule the children — a child that fails at
        import nearly always exits within the grace window, and a child
        that successfully imports settles into its main loop. Persistent
        crashes after this window are *not* caught here.
        """
        await asyncio.sleep(0.2)

        # Indexer: always starts (as the librarian process). Mark ERROR only
        # if that process failed to start.
        librarian_alive = (
            self._librarian_proc is not None and self._librarian_proc.is_alive()
        )
        idx = self._subfeatures["indexer"]
        if librarian_alive:
            idx.state = ModuleHealthState.READY
            idx.message = ""
        else:
            idx.state = ModuleHealthState.ERROR
            idx.message = "Librarian subprocess failed to start."

        # Enricher: DISABLED if config.enricher.enabled is False; else READY
        # (hard dependencies are guaranteed by the plugin's deb). The one
        # optional leg is generated album art, which needs numpy — a
        # missing numpy there is a WARNING (art generation off), not an
        # ERROR: metadata enrichment itself is unaffected.
        enr = self._subfeatures["enricher"]
        if not config.enricher.enabled:
            enr.state = ModuleHealthState.DISABLED
            enr.message = "Disabled in configuration."
        elif not librarian_alive:
            enr.state = ModuleHealthState.ERROR
            enr.message = "Librarian subprocess failed to start."
        elif config.enricher.plugins.procedural_artwork.enabled and not _is_importable(
            "numpy"
        ):
            enr.state = ModuleHealthState.WARNING
            enr.message = (
                "Generated album art unavailable. Missing package: `numpy`. "
                "Use **Restart with install** in the modules page to fetch it."
            )
            enr.missing_packages = ("numpy",)
        else:
            enr.state = ModuleHealthState.READY
            enr.message = ""

        # AcoustID: a key is what turns it on, and fpcalc is what lets it
        # run. The binary ships as a Recommends, so it can be absent on a
        # deliberately slim install — and a key set against a missing
        # binary is silent otherwise. Not offered as a missing_package:
        # that list drives a pip install, which cannot supply a binary.
        acoustid = self._subfeatures["acoustid"]
        if not config.enricher.enabled:
            acoustid.state = ModuleHealthState.DISABLED
            acoustid.message = "Metadata enrichment is disabled."
        elif not config.enricher.plugins.acoustid.api_key:
            acoustid.state = ModuleHealthState.DISABLED
            acoustid.message = "No API key configured."
        elif not shutil.which("fpcalc"):
            acoustid.state = ModuleHealthState.WARNING
            acoustid.message = (
                "The API key is set but `fpcalc` is not installed, so tracks "
                "are not identified by their audio. Install the "
                "**libchromaprint-tools** system package and restart."
            )
        else:
            acoustid.state = ModuleHealthState.READY
            acoustid.message = ""

        # AI search: the query and indexing halves report as one feature.
        # CLAP needs numpy + onnxruntime + soundfile + soxr + tokenizers
        # (soundfile/soxr replaced librosa).
        ai = self._subfeatures["ai_search"]
        if not config.ai_search.enabled:
            ai.state = ModuleHealthState.DISABLED
            ai.message = "Disabled in configuration."
        else:
            missing = [
                pkg
                for pkg, import_name in (
                    ("numpy", "numpy"),
                    ("onnxruntime", "onnxruntime"),
                    ("soundfile", "soundfile"),
                    ("soxr", "soxr"),
                    ("tokenizers", "tokenizers"),
                )
                if not _is_importable(import_name)
            ]
            dead = [
                name
                for name, proc in (
                    ("Searcher", self._searcher_proc),
                    ("Embedder", self._embedder_proc),
                )
                if proc is None or not proc.is_alive()
            ]
            if missing:
                ai.state = ModuleHealthState.ERROR
                ai.message = (
                    "AI search unavailable. Missing package(s): "
                    + ", ".join(f"`{m}`" for m in missing)
                    + ". Use **Restart with install** in the modules page to "
                    "fetch them."
                )
                ai.missing_packages = tuple(missing)
            elif dead:
                ai.state = ModuleHealthState.ERROR
                ai.message = f"Subprocess failed to start: {', '.join(dead)}."
            else:
                ai.state = ModuleHealthState.READY
                ai.message = ""

    def _resolver_for(self, config: LocalFilesConfig) -> StorageResolver:
        """The storage resolver for this configuration, kept across calls.

        Rebuilt only when the credentials change, because ``setup`` is not
        re-run when the server mutates the config in place.
        """
        return self._live_resolvers.get(config)

    def _current_resolver(self) -> StorageResolver:
        """The resolver for the configuration as it stands right now.

        Where the input module reads its storage from, so an edited share
        password reaches playback and the folder-status probe together. The
        workers in the other processes keep the credentials they started
        with until the module is restarted.
        """
        if self._context is None:
            raise RuntimeError("the localfiles plugin has not been set up")
        return self._resolver_for(
            LocalFilesConfig(**self._context.config.model_dump())
        )

    async def _music_folder_statuses(
        self,
    ) -> list[tuple[RootStatus, Optional[str]]]:
        """Live availability of each configured music folder, paired with the
        mount identity the indexer recorded for it (None when unrecorded).

        Evaluated on demand (module status, dynamic status field) rather than
        once at setup, so an unreachable share shows up — and clears —
        without a restart. Probing a local folder stats it, which also
        nudges a pending automount, and runs inside the service sandbox, so
        it catches paths hidden by systemd hardening too. Probing a share
        asks the server, so a wrong address or password surfaces here rather
        than as an empty library.
        """
        if self._context is None:
            return []
        config = LocalFilesConfig(**self._context.config.model_dump())
        resolver = self._resolver_for(config)
        # Off the loop: canonicalising a local folder resolves it, and a
        # folder on a hung mount would hold up every request behind it.
        folders = await asyncio.to_thread(
            resolver.canonical_roots, config.music_folders
        )
        statuses = await _probe_roots(resolver, folders)
        module = self._inputmodule
        if module is None or not module.db_manager.is_good():
            return [(status, None) for status in statuses]
        return [
            (status, module.db_manager.get_root_signature(status.root))
            for status in statuses
        ]

    # ------------------------------------------------------------------
    # SDK overrides
    # ------------------------------------------------------------------

    async def get_state(self) -> ModuleState:
        """Roll sub-feature states up into a single ModuleState."""
        # The plugin overall is ERROR if any *required* sub-feature is ERROR.
        # Otherwise WARNING if any non-required sub-feature is ERROR or any
        # sub-feature reports a WARNING (something the user might want to
        # fix). Otherwise READY.
        any_required_error = False
        any_optional_error = False
        broken_titles: list[str] = []
        degraded_titles: list[str] = []
        missing_packages: list[str] = []
        seen_packages: set[str] = set()
        for sf in self._subfeatures.values():
            if sf.state == ModuleHealthState.ERROR:
                if sf.required:
                    any_required_error = True
                    broken_titles.append(sf.title)
                else:
                    any_optional_error = True
                    broken_titles.append(sf.title)
            elif sf.state == ModuleHealthState.WARNING:
                degraded_titles.append(sf.title)
            # Aggregate missing-package keys across all sub-features
            # (regardless of state — a DISABLED sub-feature still tells
            # us what the user would need if they re-enable it).
            for pkg in sf.missing_packages:
                if pkg not in seen_packages:
                    seen_packages.add(pkg)
                    missing_packages.append(pkg)

        # A missing or unmounted music folder is the most silent
        # misconfiguration we have — the indexer just finds nothing.
        unavailable_roots = [
            status
            for status, stored in await self._music_folder_statuses()
            if not status.available or _mount_mismatch(status, stored)
        ]
        if unavailable_roots:
            degraded_titles.append("Music folder access")

        if any_required_error:
            state = ModuleHealthState.ERROR
        elif any_optional_error or degraded_titles:
            state = ModuleHealthState.WARNING
        else:
            state = ModuleHealthState.READY

        if state == ModuleHealthState.READY:
            return ModuleState(
                state=state, message=None, missing_packages=missing_packages
            )

        # One-line summary; the per-sub-feature detail lives in the
        # dynamic status_view fields on the relevant settings sections.
        parts = []
        if broken_titles:
            parts.append(f"{', '.join(broken_titles)} unavailable")
        if degraded_titles:
            parts.append(f"{', '.join(degraded_titles)} degraded")
        summary = (
            f"{'; '.join(parts)} — "
            "open the corresponding section below for details."
        )
        return ModuleState(
            state=state, message=summary, missing_packages=missing_packages
        )

    async def resolve_dynamic_field(self, path: str) -> Any:
        """Return rich-text status for each declared dynamic field."""
        if path == "storage.status_view":
            statuses = await self._music_folder_statuses()
            if not statuses or self._context is None:
                return "No music folders configured."
            config = LocalFilesConfig(**self._context.config.model_dump())
            return "\n\n".join(
                _format_root_status(status, stored, config.scan_interval_minutes)
                for status, stored in statuses
            )
        # Strip the "<subfeature>." prefix from a "<subfeature>.status_view" path.
        if path.endswith(".status_view"):
            sf_id = path[: -len(".status_view")]
            sf = self._subfeatures.get(sf_id)
            if sf is None:
                raise KeyError(path)
            return _format_subfeature_status(sf)
        raise KeyError(path)

    async def resolve_options(self, path: str) -> list[ConfigOption]:
        """Places the user could point a music folder at.

        Offers what each source already knows and asks it for more in the
        same breath, so a NAS that answers late is on the list the next
        time the page is read rather than making this call wait for it.
        """
        if path != "music_folders":
            raise KeyError(path)
        options: list[ConfigOption] = []
        for suggester in self._suggesters:
            suggester.refresh()
            options.extend(suggester.options())
        return options

    def _resolver_for_candidate(self, candidate: LocalFilesConfig) -> StorageResolver:
        """The resolver to judge ``candidate``'s folders with.

        Credentials the user has only staged must not reach the resolver
        playback reads, so they get a cache of their own. Unchanged
        credentials are judged with the live resolver instead, which is
        worth reaching for: it already knows which folders are hung.
        """
        if self._context is not None and candidate.smb == self._context.config.smb:
            return self._current_resolver()
        return self._staged_resolvers.get(candidate)

    async def validate_config(
        self, candidate: ModuleConfig, changed: frozenset[str]
    ) -> list[ConfigIssue]:
        """What is wrong with the folders the user has typed.

        How a folder is written is the user's mistake to fix, so it refuses
        the save. Whether it answers right now is not: a NAS that is switched
        off tonight is still the right folder to have configured, and the
        library keeps what it indexed under a root it cannot currently see.

        @note Only a folder list the user has just edited can be refused. A
            credential change re-asks whether the shares answer, but an entry
            that was already misspelled before this save is not the user's
            mistake to fix *now*, and refusing the batch over it would leave
            the password unsaveable.
        """
        folders_edited = "music_folders" in changed
        if not folders_edited and not any(
            path == "smb" or path.startswith("smb.") for path in changed
        ):
            return []

        config = LocalFilesConfig(**candidate.model_dump())
        issues, probing = await asyncio.to_thread(
            _judge_spelling, config.music_folders, folders_edited
        )

        resolver = self._resolver_for_candidate(config)
        statuses = await _probe_roots(resolver, [root for _index, root in probing])
        for (index, _root), status in zip(probing, statuses):
            if status.available:
                continue
            issues.append(
                ConfigIssue(
                    path="music_folders",
                    index=index,
                    severity=IssueSeverity.WARNING,
                    message=(
                        f"{status.reason}; it can be saved, but nothing is "
                        "indexed under it until it can be read"
                    ),
                )
            )
        return issues

    async def required_packages(self) -> list[str]:
        """Optional-package keys this plugin would need given the *current*
        in-memory config.

        Reads context.config (which reflects any staged PUT /server/config
        changes), not the config captured at setup. The server calls this
        when handling /server/restart so an install is auto-queued for
        sub-features the user just enabled but whose deps aren't yet
        importable.
        """
        if self._context is None:
            return []
        cfg = self._context.config

        missing: list[str] = []
        seen: set[str] = set()

        def need(key: str, import_name: str) -> None:
            if key in seen:
                return
            if not _is_importable(import_name):
                missing.append(key)
                seen.add(key)

        # Enricher: numpy for the generated-album-art leg.
        if cfg.enricher.enabled and cfg.enricher.plugins.procedural_artwork.enabled:
            need("numpy", "numpy")

        # AI search: numpy for mood ranking, plus the CLAP-side deps
        # (soundfile + soxr handle audio decode/resample, replacing librosa).
        if cfg.ai_search.enabled:
            need("numpy", "numpy")
            need("onnxruntime", "onnxruntime")
            need("soundfile", "soundfile")
            need("soxr", "soxr")
            need("tokenizers", "tokenizers")

        return missing

    def _shutdown_process(self, proc):
        """Shutdown a process by sending a termination signal"""

        # A sub-feature that was never started has no process — routine
        # since AI search is off by default, so not worth a warning.
        if proc is None:
            return
        if not proc.is_alive():
            logger.warning("Process already shut down.")
            return

        # Send shutdown command over the process's socket
        try:
            sleeping_time = 5
            proc.terminate()
            proc.join(timeout=sleeping_time)
            if proc.is_alive():
                logger.warning(
                    f"Process {proc.pid} did not shut down gracefully, killing it."
                )
                proc.kill()
                proc.join(timeout=sleeping_time)
            else:
                logger.info(f"Process {proc.pid} shut down successfully.")
        except Exception as e:
            logger.error(f"Error shutting down process {proc.pid}: {e}")

    async def shutdown(self) -> None:
        logger.info("Shutting down localfiles input module")

        await asyncio.to_thread(self._discovery.stop)
        for resolvers in (self._live_resolvers, self._staged_resolvers):
            resolvers.close()

        self._shutdown_process(self._librarian_proc)
        self._shutdown_process(self._searcher_proc)
        self._shutdown_process(self._embedder_proc)

        if self._log_listener is not None:
            self._log_listener.stop()

        # Ensure multiprocessing queues release their semaphores
        for q in (
            self._searcher_nudge_queue,
            self._embedder_nudge_queue,
            self._logging_queue,
            self._search_request_queue,
            self._search_response_queue,
            self._text_encode_request_queue,
            self._text_encode_response_queue,
        ):
            if q is not None:
                q.close()
                q.join_thread()
