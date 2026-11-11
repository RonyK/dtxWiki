"""Normalize active Context maps before the first Git snapshots are written.

Contract v1 uses the shared pure Markdown/block-map helpers: CommonMark with
tables, exact raw line coordinates and deterministic version-UUID identity
seeds. It deliberately imports no ORM, publication, review or writer service.
The revision owns active-version selection and SQL validation. Future changes to the
shared pure contract must retain this traversal.
"""

from __future__ import annotations

import json
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from backend.utils.block_identity import (
    heal_block_map,
    validated_block_map_for_version,
)


def _uuid(value) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _same(left, right) -> bool:
    try:
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
            right, sort_keys=True, allow_nan=False
        )
    except (TypeError, ValueError):
        return False


def normalize(body: str, stored, version_id) -> list[dict]:
    """Keep valid IDs and deterministically replace malformed legacy claims."""
    candidate = None
    if isinstance(stored, list):
        candidate = []
        for entry in stored:
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(field), str) for field in ("kind", "hash")
            ):
                continue
            entry = dict(entry)
            if "rows" in entry:
                rows = entry["rows"]
                entry["rows"] = (
                    [row for row in rows if isinstance(row, dict)]
                    if isinstance(rows, list)
                    else []
                )
            candidate.append(entry)
    normalized, _changed = heal_block_map(
        body, candidate, identity_seed=str(_uuid(version_id))
    )
    validated_block_map_for_version(normalized, body)
    return normalized


def _active_rows(page, rows):
    by_id = {_uuid(row["id"]): row for row in rows}
    published_id = page["current_published_version_id"]
    published_id = _uuid(published_id) if published_id is not None else None
    active = {}
    if published_id is not None:
        published = by_id.get(published_id)
        if (
            published is None
            or published["version_status"] != "published_snapshot"
            or published["graph_status"] != "linear"
        ):
            raise RuntimeError("block_map_stale: invalid current published pointer")
        active[published_id] = published
    start = page["latest_draft_version_id"]
    if start is None and published_id is None:
        start = page["current_version_id"]
    cursor = _uuid(start) if start is not None else None
    chain = {}
    while cursor is not None and cursor != published_id:
        row = by_id.get(cursor)
        if (
            row is None
            or cursor in chain
            or row["version_status"] not in {"draft", "editing"}
            or row["graph_status"] != "linear"
        ):
            # The application also excludes broken/alternative chains. Do not
            # guess a live candidate from version numbers or pending reviews.
            return list(active.values())
        chain[cursor] = row
        parent = row["parent_version_id"]
        cursor = _uuid(parent) if parent is not None else None
    if cursor == published_id:
        active.update(chain)
    return list(active.values())


def upgrade(connection: Connection) -> None:
    """Repair current published and live draft maps before Git conversion."""
    if connection.get_transaction() is None:
        raise RuntimeError("Active block-map normalization requires a transaction")
    metadata = sa.MetaData()
    pages = sa.Table("context_page", metadata, autoload_with=connection)
    versions = sa.Table("context_page_version", metadata, autoload_with=connection)
    for page in connection.execute(sa.select(pages).order_by(pages.c.id)).mappings():
        rows = list(
            connection.execute(
                sa.select(versions).where(
                    versions.c.workspace_id == page["workspace_id"],
                    versions.c.page_id == page["id"],
                )
            ).mappings()
        )
        for row in _active_rows(page, rows):
            body = row["body_md"]
            if not isinstance(body, str):
                raise RuntimeError("block_map_stale: active version has no SQL body")
            normalized = normalize(body, row["block_map"], row["id"])
            if not _same(row["block_map"], normalized):
                # A repair is not an author save. Reflection avoids ORM
                # onupdate hooks, preserving body/hash/status/timestamps/OIDs.
                connection.execute(
                    versions.update()
                    .where(versions.c.id == row["id"])
                    .values(block_map=normalized)
                )
            checked = connection.execute(
                sa.select(versions.c.block_map).where(versions.c.id == row["id"])
            ).scalar_one()
            validated_block_map_for_version(checked, body)
