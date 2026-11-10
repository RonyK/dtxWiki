"""Finish offline graph corpora with the same Git snapshots runtime requires."""

from pathlib import Path

import pygit2
from sqlalchemy import select, update

from backend.app.context_version_storage import build_context_storage
from backend.context_versions.adapters.git_libgit2 import (
    _fsync_directory,
    _fsync_file,
)
from backend.context_versions.domain import ContentDescriptor, ContentPayload
from backend.models.context import ContextPage, ContextPageVersion
from backend.services.context_version_storage import align_publication
from backend.services.ops.runtime_lock import RuntimeLock, lock_path


def seed_offline_context_snapshots(
    session_factory, *, repositories_root: Path, offline_guard: RuntimeLock
) -> None:
    """Record manually seeded published rows under an exclusive runtime guard.

    This fixture-only batch runs before web servers/workers start. A durable
    full pack replaces per-object fsync; packed refs are made durable before
    the caller may start a runtime. Only after all packs and refs are durable,
    apply runtime's current-body/history-map rules to the seeded rows.
    """
    root = repositories_root.resolve()
    if (
        not offline_guard.held
        or offline_guard.mode != "exclusive"
        or offline_guard.path.resolve() != lock_path(root.parent).resolve()
    ):
        raise RuntimeError(
            "offline Context fixture requires its exclusive runtime guard"
        )
    store, writer = build_context_storage(root)
    workspaces = set()
    pages_to_align = set()
    try:
        pygit2.settings.enable_fsync_gitdir(False)
        with session_factory.begin() as session:
            session.info.update(
                context_snapshot_store=store, context_version_writer=writer
            )
            page_ids = session.scalars(select(ContextPage.id).order_by(ContextPage.id))
            for page_id in page_ids.yield_per(100):
                page = session.get(ContextPage, page_id)
                versions = list(
                    session.scalars(
                        select(ContextPageVersion)
                        .where(
                            ContextPageVersion.page_id == page.id,
                            ContextPageVersion.version_status.in_(
                                ("published_snapshot", "superseded_snapshot")
                            ),
                        )
                        .order_by(ContextPageVersion.version_no)
                    )
                )
                if not versions:
                    continue
                current = next(
                    (
                        version
                        for version in versions
                        if version.id == page.current_published_version_id
                    ),
                    None,
                )
                if (
                    current is None
                    or current.version_status != "published_snapshot"
                    or sum(v.version_status == "published_snapshot" for v in versions)
                    != 1
                ):
                    raise RuntimeError("offline Context fixture has inconsistent heads")
                if any(
                    version.version_status == "superseded_snapshot"
                    or version._body_md is None
                    for version in versions
                ):
                    pages_to_align.add(page.id)
                versions.sort(key=lambda version: version.id == current.id)
                parents = []
                for version in versions:
                    descriptor = ContentDescriptor(
                        content_format=version.content_format,
                        format_version=version.format_version,
                        media_type=version.media_type,
                        encoding=version.encoding,
                    )
                    record = writer.prepare_snapshot(
                        workspace_id=page.workspace_id,
                        page_id=page.id,
                        version_id=version.id,
                        created_at=version.created_at,
                        payload=ContentPayload(
                            version.body_md.encode("utf-8"), descriptor
                        ),
                        parents=parents,
                        existing_oid=version.commit_oid,
                    )
                    session.execute(
                        update(ContextPageVersion)
                        .where(
                            ContextPageVersion.id == version.id,
                        )
                        .values(
                            commit_oid=record.commit_oid,
                            content_byte_size=record.content_byte_size,
                            last_saved_at=version.last_saved_at,
                        )
                    )
                    parents = [record.commit_oid]
                writer.align_head(page.workspace_id, page.id, parents[0])
                workspaces.add(page.workspace_id)

            # Enabling fsync does not retroactively synchronize the loose writes.
            pygit2.settings.enable_fsync_gitdir(True)
            for workspace_id in sorted(workspaces, key=str):
                roots = set(
                    session.scalars(
                        select(ContextPageVersion.commit_oid).where(
                            ContextPageVersion.workspace_id == workspace_id,
                            ContextPageVersion.commit_oid.is_not(None),
                        )
                    )
                )
                with store.management_guard(workspace_id):
                    packed = store.pack(workspace_id, roots)
                    repository_path = store.repository_path(workspace_id)
                    repository = pygit2.Repository(repository_path)
                    try:
                        repository.refdb.compress()
                    finally:
                        repository.free()
                    _sync_repository_metadata(repository_path)
                    _verify_workspace(session, store, workspace_id)
                    store.prune_packed(workspace_id, packed)
            # No SQL source copy is released during the fsync-disabled batch.
            # The complete packs and refs above are durable before this shared
            # runtime operation verifies bytes and releases historical copies.
            for page_id in sorted(pages_to_align, key=str):
                page = session.get(ContextPage, page_id)
                current = session.get(
                    ContextPageVersion, page.current_published_version_id
                )
                align_publication(session, page, current.commit_oid, writer)
    finally:
        pygit2.settings.enable_fsync_gitdir(True)


def _sync_repository_metadata(root: Path) -> None:
    for name in ("HEAD", "config", "packed-refs"):
        path = root / name
        if path.is_file():
            _fsync_file(path)
    references = root / "refs"
    if references.is_dir():
        for path in references.rglob("*"):
            if path.is_file():
                _fsync_file(path)
        for path in sorted(references.rglob("*"), reverse=True):
            if path.is_dir():
                _fsync_directory(path)
        _fsync_directory(references)
    _fsync_directory(root)
    _fsync_directory(root.parent)
    _fsync_directory(root.parent.parent)


def _verify_workspace(session, store, workspace_id) -> None:
    for version in session.scalars(
        select(ContextPageVersion).where(
            ContextPageVersion.workspace_id == workspace_id,
            ContextPageVersion.commit_oid.is_not(None),
        )
    ).yield_per(100):
        content = store.read(
            workspace_id=workspace_id,
            page_id=version.page_id,
            commit_oid=version.commit_oid,
            descriptor=ContentDescriptor(
                content_format=version.content_format,
                format_version=version.format_version,
                media_type=version.media_type,
                encoding=version.encoding,
            ),
        )
        if content != version.body_md.encode("utf-8"):
            raise RuntimeError("offline Context fixture Git bytes differ from DB")
