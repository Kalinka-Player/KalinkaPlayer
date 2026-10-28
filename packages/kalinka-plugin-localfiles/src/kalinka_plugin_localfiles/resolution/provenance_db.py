"""Claim and provenance persistence, shared by both writers.

``metadata_claims`` and ``resolved_origin`` are written from both halves of
the library pipeline — the indexer records that a value came out of a path,
the enricher records which source won a contest — so the SQL for both tables
lives here instead of in either DB manager.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import aiosqlite

from ..db_connection import LibraryDb
from ..worker_utils import retry_db_locked
from .resolver import GUESSED


@retry_db_locked
class ProvenanceDb(LibraryDb):
    """The claim and provenance tables, inherited by both DB managers.

    @note ``retry_db_locked`` walks ``vars(cls)`` rather than the MRO, so a
        subclass's own decorator never reaches these methods; it is applied
        here so they are wrapped wherever they are mixed in.
    """

    async def record_claim(
        self,
        entity_type: str,
        entity_id: str,
        field: str,
        value: str | int,
        source: str,
        tier: str,
    ) -> None:
        """Record one field-level claim (upsert per entity/field/source).

        ``value`` is str for text fields, int for numeric origin/era fields
        (year, original_year); SQLite stores it in the TEXT column either way.
        """
        async with self._open() as conn:
            await conn.execute(
                """
                INSERT INTO metadata_claims
                    (entity_type, entity_id, field, value, source, tier, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_type, entity_id, field, source) DO UPDATE SET
                    value = excluded.value,
                    tier = excluded.tier,
                    created_at = excluded.created_at
                """,
                (entity_type, entity_id, field, value, source, tier, int(time.time())),
            )
            await conn.commit()

    async def get_claims(
        self, entity_type: str, entity_id: str, field: str
    ) -> List[Dict[str, Any]]:
        """All claims for one entity field."""
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT value, source, tier FROM metadata_claims "
                "WHERE entity_type=? AND entity_id=? AND field=?",
                (entity_type, entity_id, field),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def record_resolved_origin(
        self,
        entity_type: str,
        entity_id: str,
        field: str,
        source: str,
        tier: str,
        evidence_ref: Optional[str] = None,
        keep_stronger: bool = False,
    ) -> None:
        """Record where a resolved field value came from (upsert per field).

        @param keep_stronger Leave an existing origin alone unless it is itself
            a guess. Artist and album rows are shared by many files, so without
            it a folder with one untagged track among tagged ones would have
            its provenance decided by the order the scan happened to read them.
        """
        params = [
            entity_type,
            entity_id,
            field,
            source,
            tier,
            evidence_ref,
            int(time.time()),
        ]
        guard = ""
        if keep_stronger:
            guard = " WHERE resolved_origin.tier = ?"
            params.append(GUESSED)
        async with self._open() as conn:
            await conn.execute(
                f"""
                INSERT INTO resolved_origin
                    (entity_type, entity_id, field, source, tier, evidence_ref,
                     resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_type, entity_id, field) DO UPDATE SET
                    source = excluded.source,
                    tier = excluded.tier,
                    evidence_ref = excluded.evidence_ref,
                    resolved_at = excluded.resolved_at{guard}
                """,
                params,
            )
            await conn.commit()

    async def get_resolved_origin(
        self, entity_type: str, entity_id: str, field: str
    ) -> Optional[Dict[str, Any]]:
        """The stored origin of one resolved field, or None if never recorded."""
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT source, tier, evidence_ref FROM resolved_origin "
                "WHERE entity_type=? AND entity_id=? AND field=?",
                (entity_type, entity_id, field),
            )
            row = await cur.fetchone()
            return dict(row) if row else None
