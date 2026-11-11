"""Revision-owned Git conversion; independent of mutable runtime implementations.

Keep this helper with its revision. Source rows, UUIDs, bodies and block maps
remain in SQL; only published snapshots acquire a separate exact Git copy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pygit2
import sqlalchemy as sa
from sqlalchemy.engine import Connection

ATTRIBUTE = "context_repositories_root"
_SNAPSHOTS = ("published_snapshot", "superseded_snapshot")
_NAME = "dtxWiki"
_EMAIL = "git@noreply.dtxwiki.com"


def repository_root(attributes: dict) -> Path:
    value = attributes.get(ATTRIBUTE)
    if not isinstance(value, (str, Path)) or not Path(value).is_absolute():
        raise RuntimeError(
            "Context Git migration requires an absolute "
            "Config.attributes['context_repositories_root']; pass the configured "
            "data directory's repositories path explicitly. No changes applied."
        )
    return Path(value)


def _tables(connection: Connection):
    metadata = sa.MetaData()
    return (
        sa.Table("context_page", metadata, autoload_with=connection),
        sa.Table("context_page_version", metadata, autoload_with=connection),
    )


def _uuid(value) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _snapshot_rows(connection: Connection, versions, page_id):
    return list(
        connection.execute(
            sa.select(versions)
            .where(
                versions.c.page_id == page_id,
                versions.c.version_status.in_(_SNAPSHOTS),
            )
            .order_by(versions.c.version_no, versions.c.id)
        ).mappings()
    )


def preflight(connection: Connection) -> None:
    """Refuse ambiguous/missing published pointers before any new DDL or Git I/O."""
    pages, versions = _tables(connection)
    orphans = connection.scalar(
        sa.select(sa.func.count())
        .select_from(versions.outerjoin(pages, versions.c.page_id == pages.c.id))
        .where(versions.c.version_status.in_(_SNAPSHOTS), pages.c.id.is_(None))
    )
    if orphans:
        raise RuntimeError("Context Git migration refused orphaned published snapshots")
    for page in connection.execute(sa.select(pages)).mappings():
        rows = _snapshot_rows(connection, versions, page["id"])
        published = [
            row for row in rows if row["version_status"] == "published_snapshot"
        ]
        head_id = page["current_published_version_id"]
        if not rows and head_id is None:
            continue
        if (
            head_id is None
            or len(published) != 1
            or _uuid(published[0]["id"]) != _uuid(head_id)
            or any(
                _uuid(row["workspace_id"]) != _uuid(page["workspace_id"])
                for row in rows
            )
        ):
            raise RuntimeError(
                f"Context Git migration refused inconsistent published history for "
                f"page {_uuid(page['id'])}. Run the installed pre-upgrade version's "
                "context history repair --dry-run before upgrading, or restore "
                "a consistent backup. No automatic head selection was attempted."
            )
        if any(row["body_md"] is None for row in rows):
            raise RuntimeError("Context Git migration requires all original DB bodies")


def _created_seconds(value) -> int:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return int(stamp.astimezone(UTC).timestamp())


def _metadata(block_map) -> bytes | None:
    if block_map is None:
        return None
    if not isinstance(block_map, list) or any(
        not isinstance(entry, dict) for entry in block_map
    ):
        raise RuntimeError("Context Git migration block map must be a list of objects")
    return json.dumps(
        {"schema_version": 1, "block_map": block_map},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _write(
    repo, page_id: UUID, version_id: UUID, stamp, body: bytes, parents: list, block_map
):
    content = repo.create_blob(body)
    page_tree = repo.TreeBuilder()
    page_tree.insert("content.md", content, pygit2.GIT_FILEMODE_BLOB)
    metadata = _metadata(block_map)
    if metadata is not None:
        page_tree.insert(
            "metadata.json", repo.create_blob(metadata), pygit2.GIT_FILEMODE_BLOB
        )
    contexts = repo.TreeBuilder()
    contexts.insert(str(page_id), page_tree.write(), pygit2.GIT_FILEMODE_TREE)
    root = repo.TreeBuilder()
    root.insert("context", contexts.write(), pygit2.GIT_FILEMODE_TREE)
    signature = pygit2.Signature(_NAME, _EMAIL, _created_seconds(stamp), 0)
    return repo.create_commit(
        None,
        signature,
        signature,
        f"Context snapshot {version_id}\n",
        root.write(),
        parents,
    )


def _verify(
    repo, oid, page_id: UUID, version_id: UUID, stamp, body: bytes, parents, block_map
):
    commit = repo[oid]
    if not isinstance(commit, pygit2.Commit):
        raise RuntimeError("Context snapshot OID does not name a commit")
    tree = commit.tree
    context = repo[tree["context"].id]
    page = repo[context[str(page_id)].id]
    blob = repo[page["content.md"].id]
    metadata = _metadata(block_map)
    expected_files = {"content.md"}
    if metadata is not None:
        expected_files.add("metadata.json")
        entry = page["metadata.json"]
        archived = repo[entry.id]
        if (
            entry.filemode != pygit2.GIT_FILEMODE_BLOB
            or not isinstance(archived, pygit2.Blob)
            or archived.data != metadata
        ):
            raise RuntimeError("Context Git migration block map verification failed")
    signature = (_NAME, _EMAIL, _created_seconds(stamp), 0)
    if (
        len(tree) != 1
        or len(context) != 1
        or {entry.name for entry in page} != expected_files
        or page["content.md"].filemode != pygit2.GIT_FILEMODE_BLOB
        or not isinstance(blob, pygit2.Blob)
        or blob.data != body
        or commit.message != f"Context snapshot {version_id}\n"
        or commit.parent_ids != parents
        or (
            commit.author.name,
            commit.author.email,
            commit.author.time,
            commit.author.offset,
        )
        != signature
        or (
            commit.committer.name,
            commit.committer.email,
            commit.committer.time,
            commit.committer.offset,
        )
        != signature
    ):
        raise RuntimeError("Context Git migration exact snapshot verification failed")


def convert(connection: Connection, repositories_root: Path) -> None:
    """Stream one page at a time, preserving SQL source rows until PR 2."""
    pygit2.settings.enable_fsync_gitdir(True)
    pages, versions = _tables(connection)
    for page in connection.execute(sa.select(pages).order_by(pages.c.id)).mappings():
        rows = _snapshot_rows(connection, versions, page["id"])
        if not rows:
            continue
        page_id = _uuid(page["id"])
        head_id = _uuid(page["current_published_version_id"])
        rows.sort(key=lambda row: _uuid(row["id"]) == head_id)
        workspace_id = _uuid(page["workspace_id"])
        path = repositories_root / f"{workspace_id}.git"
        if path.is_symlink():
            raise RuntimeError("Context Git migration refuses repository symlinks")
        if path.exists():
            repo = pygit2.Repository(
                str(path), flags=pygit2.enums.RepositoryOpenFlag.NO_SEARCH
            )
            if not repo.is_bare:
                raise RuntimeError("Context Git migration requires bare repositories")
        else:
            existing = connection.scalar(
                sa.select(versions.c.id)
                .where(
                    versions.c.workspace_id == page["workspace_id"],
                    versions.c.commit_oid.is_not(None),
                )
                .limit(1)
            )
            if existing is not None:
                raise RuntimeError(
                    "Context Git migration repository is missing existing OIDs"
                )
            repositories_root.mkdir(parents=True, exist_ok=True)
            repo = pygit2.init_repository(str(path), bare=True)
        parents = []
        for row in rows:
            version_id = _uuid(row["id"])
            body = row["body_md"].encode("utf-8")
            expected = _write(
                repo,
                page_id,
                version_id,
                row["created_at"],
                body,
                parents,
                row["block_map"],
            )
            if row["commit_oid"] is not None and row["commit_oid"] != str(expected):
                raise RuntimeError(
                    "Context Git migration found an inconsistent existing OID"
                )
            _verify(
                repo,
                expected,
                page_id,
                version_id,
                row["created_at"],
                body,
                parents,
                row["block_map"],
            )
            connection.execute(
                versions.update()
                .where(versions.c.id == row["id"])
                .values(
                    commit_oid=str(expected),
                    content_byte_size=len(body),
                    content_format="markdown",
                    format_version=1,
                    media_type="text/markdown",
                    encoding="utf-8",
                )
            )
            parents = [expected]
        ref = f"refs/heads/context/{page_id}"
        repo.references.create(ref, parents[0], force=True)
        if repo.references[ref].target != parents[0]:
            raise RuntimeError(
                "Context Git migration published head verification failed"
            )


def require_database_bodies(connection: Connection) -> None:
    _pages, versions = _tables(connection)
    missing = connection.scalar(
        sa.select(sa.func.count())
        .select_from(versions)
        .where(versions.c.body_md.is_(None))
    )
    if missing:
        raise RuntimeError("Context Git downgrade refused: restore all DB bodies first")
