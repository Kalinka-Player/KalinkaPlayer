#!/usr/bin/env python3
"""Single-writer librarian process.

Phase 2b of the enrichment redesign merges the indexer and the enricher into
one process so exactly one writer touches the library DB. The two worker loops
are unchanged — only the indexer→enricher "enrich"/"stop" handoff moves from a
cross-process ``multiprocessing.Queue`` to an in-process ``asyncio.Queue``. The
searcher still runs separately, so the nudge to it stays a multiprocessing
queue.
"""

import asyncio
import logging
import multiprocessing
import signal
from typing import Optional

from .config_model import LocalFilesConfig
from .worker_utils import configure_worker_logging, set_proc_title
from .indexer import indexer as indexer_mod
from .indexer.indexer_db import AsyncIndexerDb
from .enricher import enricher as enricher_mod
from .enricher.enricher_db import AsyncEnricherDb

logger = logging.getLogger("librarian")


async def async_main(
    config: LocalFilesConfig,
    searcher_nudge_queue: Optional[multiprocessing.Queue] = None,
    embedder_nudge_queue: Optional[multiprocessing.Queue] = None,
):
    """Run the indexer and enricher worker loops in one event loop."""
    shutdown_event = asyncio.Event()
    enricher_enabled = config.enricher.enabled

    # The indexer emits "enrich" onto this queue after every scan; the
    # enricher consumes it. In-process now, so no serialization or process
    # hop — the whole point of the merge. When the enricher is disabled the
    # queue is left unwired (None), so trigger_enricher_update no-ops instead
    # of growing a queue nobody drains.
    enrich_queue: Optional[asyncio.Queue] = (
        asyncio.Queue() if enricher_enabled else None
    )
    indexer_mod._enricher_queue = enrich_queue
    # A finished scan wakes the embedder directly: clap_audio no longer
    # waits for enrichment.
    indexer_mod._embedder_nudge_queue = embedder_nudge_queue

    # Both worker loops set _shutdown_event on a "stop" command; point them
    # at the librarian's event so either one flags the whole process.
    indexer_mod._shutdown_event = shutdown_event

    indexer_db = AsyncIndexerDb(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    indexer_task = indexer_mod.start_indexer(config, indexer_db)
    watcher_task = indexer_mod.start_file_watcher(config)

    enricher_task = None
    if enricher_enabled:
        enricher_mod._enricher_queue = enrich_queue
        enricher_mod._searcher_nudge_queue = searcher_nudge_queue
        enricher_mod._embedder_nudge_queue = embedder_nudge_queue
        enricher_mod._shutdown_event = shutdown_event
        enricher_task = enricher_mod.start_enricher(config, AsyncEnricherDb(config))

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        logger.info("Server cancelled, shutting down...")
    except Exception as e:
        logger.exception(f"Error in librarian main loop: {e}")
    finally:
        logger.info("Librarian initiating shutdown of workers...")
        if watcher_task and not watcher_task.done():
            await indexer_mod.stop_file_watcher()
        if indexer_task and not indexer_task.done():
            await indexer_mod.stop_indexer()
        if enricher_task and not enricher_task.done():
            await enricher_mod.stop_enricher()
        logger.info("Librarian shutdown complete.")


def main(
    config: LocalFilesConfig,
    logger_queue: multiprocessing.Queue,
    searcher_nudge_queue: Optional[multiprocessing.Queue] = None,
    embedder_nudge_queue: Optional[multiprocessing.Queue] = None,
):
    """Main entry point for the librarian daemon."""
    set_proc_title("kal-librarian")

    configure_worker_logging(logger_queue)

    try:
        asyncio.run(async_main(config, searcher_nudge_queue, embedder_nudge_queue))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received by asyncio.run. Exiting.")
    except Exception as e:
        logger.critical(f"Unhandled exception in asyncio.run: {e}", exc_info=True)
    finally:
        logger.info("Librarian daemon finished.")
