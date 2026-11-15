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


def merge_preview_binding(client, publish_url, *, headers=None):
    """Obtain the exact reviewed result for a test's explicit publish request."""
    page_url, version_path = publish_url.rsplit("/versions/", 1)
    version_id = version_path.split("/", 1)[0]
    response = client.post(
        page_url + "/merge-preview",
        json={"version_id": version_id},
        headers=headers or {},
    )
    assert response.status_code == 200, response.text
    preview = response.json()["data"]
    assert preview["result"] is not None, preview
    result_hash = preview["result"]["result_hash"]
    return {
        "expected_editing_revision": preview["editing_revision"],
        "expected_published_version_id": preview["published_version_id"],
        "result_hash": result_hash,
        "idempotency_key": f"publish:{version_id}:{result_hash}",
    }


def historical_context_version_model(engine):
    """Map only columns in a historical fixture, with the current bind adapters.

    Historical downgrade tests must stop before the irreversible shared-draft
    cutover. This local mapper keeps their original data construction independent
    of columns added after the historical schema they actually exercise.
    """
    from sqlalchemy import MetaData, Table
    from sqlalchemy.orm import registry

    from backend.models.context import ContextPageVersion

    table = Table("context_page_version", MetaData(), autoload_with=engine)
    for column in table.c:
        source = ContextPageVersion.__table__.c.get(column.name)
        if source is not None:
            column.type = source.type
            column.default = source.default
    mapper_registry = registry()
    model = type("HistoricalContextPageVersion", (), {})
    mapper_registry.map_imperatively(model, table)
    return model


def review_preview_binding(client, publish_url):
    """Bind approval to the displayed source revision, head and merge result."""
    return {
        key: value
        for key, value in merge_preview_binding(client, publish_url).items()
        if key != "idempotency_key"
    }
