"""Revision-owned validation and SQL body removal, independent of runtime code."""

from __future__ import annotations

import json
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pygit2
import sqlalchemy as sa
from sqlalchemy.engine import Connection

_SNAPSHOTS = ("published_snapshot", "superseded_snapshot")
_DESCRIPTOR = ("markdown", 1, "text/markdown", "utf-8")


def repository_root(attributes: dict) -> Path:
    value = attributes.get("context_repositories_root")
    if not isinstance(value, (str, Path)) or not Path(value).is_absolute():
        raise RuntimeError(
            "Context Git body migration requires an absolute "
            "Config.attributes['context_repositories_root']; no changes applied"
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


def _content_hash(body: str) -> str:
    # The public hash contract as it existed for this revision. Exact bytes are
    # checked separately; canonical equivalence never authorizes SQL deletion.
    canonical = (
        unicodedata.normalize("NFC", body).replace("\r\n", "\n").replace("\r", "\n")
    )
    lines = [line.rstrip(" \t") for line in canonical.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _seconds(stamp) -> int:
    instant = (
        stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp))
    )
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return int(instant.astimezone(UTC).timestamp())


@contextmanager
def _repository(root: Path, workspace_id):
    path = root / f"{_uuid(workspace_id)}.git"
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(
            "Context Git body migration requires the original repository"
        )
    repository = pygit2.Repository(
        str(path), flags=pygit2.enums.RepositoryOpenFlag.NO_SEARCH
    )
    try:
        if not repository.is_bare:
            raise RuntimeError("Context Git body migration requires a bare repository")
        yield repository
    finally:
        repository.free()


def _read(root: Path, row) -> tuple[str, list[dict] | None]:
    """Prove the original version identity, descriptor and exact stored content."""
    try:
        descriptor = tuple(
            row[key]
            for key in ("content_format", "format_version", "media_type", "encoding")
        )
        if descriptor != _DESCRIPTOR or not row["commit_oid"]:
            raise ValueError("missing snapshot mapping or unsupported descriptor")
        page_id, version_id = _uuid(row["page_id"]), _uuid(row["id"])
        with _repository(root, row["workspace_id"]) as repository:
            commit = repository[row["commit_oid"]]
            if (
                not isinstance(commit, pygit2.Commit)
                or str(commit.id) != row["commit_oid"]
            ):
                raise ValueError("snapshot OID is not the full commit identity")
            context_entry = commit.tree["context"]
            context_tree = repository[context_entry.id]
            page_entry = context_tree[str(page_id)]
            page_tree = repository[page_entry.id]
            content_entry = page_tree["content.md"]
            blob = repository[content_entry.id]
            names = {entry.name for entry in page_tree}
            if (
                len(commit.tree) != 1
                or len(context_tree) != 1
                or names
                not in (
                    {"content.md"},
                    {"content.md", "metadata.json"},
                )
                or context_entry.filemode != pygit2.GIT_FILEMODE_TREE
                or page_entry.filemode != pygit2.GIT_FILEMODE_TREE
                or content_entry.filemode != pygit2.GIT_FILEMODE_BLOB
                or not isinstance(blob, pygit2.Blob)
            ):
                raise ValueError("snapshot tree does not belong to this page")
            block_map = None
            if "metadata.json" in names:
                metadata_entry = page_tree["metadata.json"]
                metadata_blob = repository[metadata_entry.id]
                if (
                    metadata_entry.filemode != pygit2.GIT_FILEMODE_BLOB
                    or not isinstance(metadata_blob, pygit2.Blob)
                ):
                    raise ValueError("snapshot metadata is not a file")
                metadata = json.loads(metadata_blob.data.decode("utf-8"))
                if (
                    not isinstance(metadata, dict)
                    or set(metadata) != {"schema_version", "block_map"}
                    or type(metadata["schema_version"]) is not int
                    or metadata["schema_version"] != 1
                    or not isinstance(metadata["block_map"], list)
                    or not all(
                        isinstance(entry, dict) for entry in metadata["block_map"]
                    )
                ):
                    raise ValueError("snapshot metadata schema is invalid")
                block_map = metadata["block_map"]
                if row["block_map"] is not None and json.dumps(
                    block_map, sort_keys=True, allow_nan=False
                ) != json.dumps(row["block_map"], sort_keys=True, allow_nan=False):
                    raise ValueError("snapshot block map differs from SQL")
            data = blob.data
            body = data.decode("utf-8")
            signature = (
                "dtxWiki",
                "git@noreply.dtxwiki.com",
                _seconds(row["created_at"]),
                0,
            )
            if (
                len(data) != row["content_byte_size"]
                or _content_hash(body) != row["content_hash"]
                or commit.message != f"Context snapshot {version_id}\n"
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
                or (
                    row["body_md"] is not None
                    and data != row["body_md"].encode("utf-8")
                )
            ):
                raise ValueError("snapshot metadata or exact bytes differ")
            return body, block_map
    except (
        KeyError,
        ValueError,
        TypeError,
        UnicodeError,
        OSError,
        pygit2.GitError,
    ) as exc:
        raise RuntimeError(
            f"Context Git body migration verification failed for version {row['id']}"
        ) from exc


def verify(
    connection: Connection, root: Path, *, require_database_bodies: bool
) -> None:
    """Finish the complete read-only validation pass before any DDL or removal."""
    pages, versions = _tables(connection)
    for row in connection.execute(
        sa.select(versions).execution_options(yield_per=100)
    ).mappings():
        if require_database_bodies and row["body_md"] is None:
            raise RuntimeError(
                "Context Git body migration requires every original SQL body"
            )
        needs_git = (
            row["version_status"] in _SNAPSHOTS
            or row["body_md"] is None
            or row["commit_oid"] is not None
        )
        if not needs_git:
            continue
        page = connection.execute(
            sa.select(pages.c.workspace_id).where(pages.c.id == row["page_id"])
        ).scalar_one_or_none()
        if page is None or _uuid(page) != _uuid(row["workspace_id"]):
            raise RuntimeError(
                "Context Git body migration found foreign snapshot ownership"
            )
        _read(root, row)
    for page in connection.execute(
        sa.select(pages).execution_options(yield_per=100)
    ).mappings():
        if page["current_published_version_id"] is None:
            continue
        head = (
            connection.execute(
                sa.select(versions).where(
                    versions.c.id == page["current_published_version_id"]
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            head is None
            or _uuid(head["page_id"]) != _uuid(page["id"])
            or head["version_status"] != "published_snapshot"
        ):
            raise RuntimeError(
                "Context Git body migration found an invalid published pointer"
            )
        with _repository(root, page["workspace_id"]) as repository:
            ref = f"refs/heads/context/{_uuid(page['id'])}"
            if str(repository.references[ref].resolve().target) != head["commit_oid"]:
                raise RuntimeError(
                    "Context Git body migration found a mismatched published head"
                )


def release_historical_bodies(connection: Connection, root: Path) -> None:
    _pages, versions = _tables(connection)
    historical = (
        sa.select(versions)
        .where(versions.c.version_status == "superseded_snapshot")
        .execution_options(yield_per=100)
    )
    for row in connection.execute(historical).mappings():
        _body, archived_map = _read(root, row)
        # Existing content-only commits are immutable. Their SQL map remains
        # the only exact identity copy, regardless of current branch activity.
        changes = {"body_md": None}
        if archived_map is not None:
            changes["block_map"] = None
        connection.execute(
            versions.update().where(versions.c.id == row["id"]).values(**changes)
        )


def restore_database_bodies(connection: Connection, root: Path) -> None:
    """Second pass after verify: restore bytes without retaining a full corpus."""
    _pages, versions = _tables(connection)
    missing = (
        sa.select(versions)
        .where(versions.c.body_md.is_(None))
        .execution_options(yield_per=100)
    )
    for row in connection.execute(missing).mappings():
        body, archived_map = _read(root, row)
        changes = {"body_md": body}
        if archived_map is not None:
            changes["block_map"] = archived_map
        connection.execute(
            versions.update().where(versions.c.id == row["id"]).values(**changes)
        )
