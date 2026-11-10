"""Explicit Git composition and snapshot records for pre-existing test fixtures."""

import os
from pathlib import Path

from backend.app.context_version_storage import build_context_storage
from backend.context_versions.codecs import MarkdownCodec


def configure_test_context_storage(factory, root: Path | None = None):
    if root is None:
        # The autouse backend fixture always isolates this directory. Require
        # its explicit environment value rather than ever falling back home.
        root = Path(os.environ["DTXWIKI_DATA_DIR"]) / "repositories"
    store, writer = build_context_storage(root)
    factory.configure(
        info={
            **factory.kw.get("info", {}),
            "context_snapshot_store": store,
            "context_version_writer": writer,
        }
    )
    return factory


def seed_published_snapshot(session, version, *, parents=()):
    """Seed a fixture's already-published row before its tested operation."""
    session.flush()
    writer = session.info["context_version_writer"]
    record = writer.prepare_snapshot(
        workspace_id=version.workspace_id,
        page_id=version.page_id,
        version_id=version.id,
        created_at=version.created_at,
        payload=MarkdownCodec().encode(version.body_md),
        parents=parents,
        existing_oid=version.commit_oid,
    )
    version.commit_oid = record.commit_oid
    version.content_byte_size = record.content_byte_size
    if version.version_status == "published_snapshot":
        writer.align_head(version.workspace_id, version.page_id, record.commit_oid)
    return record
