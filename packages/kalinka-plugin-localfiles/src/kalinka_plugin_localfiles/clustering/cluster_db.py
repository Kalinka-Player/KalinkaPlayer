"""Data access for album_cluster, membership_constraint and entity_id_alias."""

import os
from typing import Any, Dict, List, Optional

import aiosqlite

from ..config_model import LocalFilesConfig
from ..db_connection import LibraryDb
from ..worker_utils import retry_db_locked


@retry_db_locked
class AsyncClusterDb(LibraryDb):
    """Reads/writes the Phase-1 clustering tables."""

    def __init__(self, config: LocalFilesConfig):
        self.db_path = os.path.expanduser(config.db_path)

    # -- id aliases ----------------------------------------------------------

    async def resolve_id(self, entity_id: str) -> str:
        """Return the current id for a (possibly aliased) id. One hop —
        aliases are flattened on write."""
        async with self._open() as conn:
            cur = await conn.execute(
                "SELECT current_id FROM entity_id_alias WHERE old_id = ?",
                (entity_id,),
            )
            row = await cur.fetchone()
            return row[0] if row else entity_id

    async def add_alias(
        self, old_id: str, current_id: str, entity_type: str
    ) -> None:
        """Redirect ``old_id`` to ``current_id``, keeping the table single-hop.

        If ``current_id`` is itself aliased, the new row points at the final
        target; existing aliases that pointed at ``old_id`` are repointed to
        that target too. So A->B then merge(B into C) leaves both A and B
        pointing directly at C.
        """
        if old_id == current_id:
            return
        async with self._open() as conn:
            cur = await conn.execute(
                "SELECT current_id FROM entity_id_alias WHERE old_id = ?",
                (current_id,),
            )
            row = await cur.fetchone()
            target = row[0] if row else current_id
            await conn.execute(
                "UPDATE entity_id_alias SET current_id = ? WHERE current_id = ?",
                (target, old_id),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO entity_id_alias "
                "(old_id, current_id, entity_type) VALUES (?, ?, ?)",
                (old_id, target, entity_type),
            )
            await conn.commit()

    # -- clusters ------------------------------------------------------------

    async def get_cluster(self, album_id: str) -> Optional[Dict[str, Any]]:
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM album_cluster WHERE album_id = ?", (album_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def upsert_cluster(
        self,
        album_id: str,
        primary_folder: str,
        grouping_conf: float = 1.0,
        grouping_basis: Optional[str] = None,
        kind: Optional[str] = None,
        needs_review: bool = False,
        review_reason: Optional[str] = None,
        bump_generation: bool = False,
    ) -> None:
        """Create or update a cluster row. ``generation`` is incremented on an
        update when ``bump_generation`` is set (a re-cluster, for hysteresis)."""
        gen_clause = (
            "generation = album_cluster.generation + 1"
            if bump_generation
            else "generation = album_cluster.generation"
        )
        async with self._open() as conn:
            await conn.execute(
                f"""
                INSERT INTO album_cluster
                    (album_id, primary_folder, grouping_conf, grouping_basis,
                     kind, generation, needs_review, review_reason)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(album_id) DO UPDATE SET
                    primary_folder = excluded.primary_folder,
                    grouping_conf  = excluded.grouping_conf,
                    grouping_basis = excluded.grouping_basis,
                    kind           = excluded.kind,
                    needs_review   = excluded.needs_review,
                    review_reason  = excluded.review_reason,
                    {gen_clause}
                """,
                (
                    album_id,
                    primary_folder,
                    grouping_conf,
                    grouping_basis,
                    kind,
                    int(needs_review),
                    review_reason,
                ),
            )
            await conn.commit()

    async def delete_cluster(self, album_id: str) -> None:
        async with self._open() as conn:
            await conn.execute(
                "DELETE FROM album_cluster WHERE album_id = ?", (album_id,)
            )
            await conn.commit()

    # -- membership constraints ---------------------------------------------

    async def get_constraints_for_tracks(
        self, track_ids: List[str]
    ) -> List[Dict[str, Any]]:
        if not track_ids:
            return []
        placeholders = ", ".join("?" for _ in track_ids)
        async with self._open() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                f"SELECT * FROM membership_constraint WHERE track_id "
                f"IN ({placeholders})",
                track_ids,
            )
            return [dict(r) for r in await cur.fetchall()]
