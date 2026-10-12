"""Epic-1 content-hash phase backfill coverage.

Mirrors the established pattern in test_context_history_migration_
normalization.py -- a real Alembic Config driving the actual migration chain
against a temp-file SQLite database, seeding via Core insert into tables
reflected at the seeded revision (not the ORM's current-head models, which
may not match an older schema shape).
"""

from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.services.library_storage import inline_text_ref
from backend.utils.content_hash import content_hash as calculate_content_hash

epic1 = import_module("backend.migrations.versions.v0_1.efcbcf724889_core_schema")
workspace_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.workspace_schema"
)
mcp_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.identity_access_schema"
)
media_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.library_media_schema"
)
public_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.context_schema"
)
library_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.library_policy"
)
content_hash_phase = import_module(
    "backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema.context_data"
)

ACTOR_ID = "00000000-0000-0000-0000-000000000010"

# What content_hash("---\ntitle: T\nlast_modified_at: 2026-05-04T00:00:00Z\n
# ---\nbody") produced under the OLD (stripping) algorithm -- the exact
# stale value a pre-fix server would have stored for this body. Frozen here,
# not recomputed, so a regression in the migration's own hash logic can't
# silently make this fixture agree with it by accident.
_AFFECTED_BODY = "---\ntitle: T\nlast_modified_at: 2026-05-04T00:00:00Z\n---\nbody"
_STALE_HASH_FOR_AFFECTED_BODY = (
    "2f4f2c68d3fa526cbf68ff8b4fb26a2cba020778e6f5f721cf257b606083d87c"
)


def _config(database_url: str) -> Config:
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _database_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{tmp_path / name}"


def _run_steps(engine, *steps) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for step in steps:
                step()


def _upgrade_to_content_hash_boundary(config: Config, database_url: str):
    command.upgrade(config, "bd62193d5cf6")
    engine = create_engine(database_url)
    _run_steps(
        engine,
        epic1._upgrade_base_schema,
        workspace_phase.upgrade,
        mcp_phase.upgrade,
        public_phase.upgrade,
        media_phase.upgrade,
        library_phase.upgrade,
    )
    return engine


def _insert_workspace(connection, workspaces, *, workspace_id, actor_id, slug, now):
    values = {
        "id": workspace_id.hex,
        "slug": slug,
        "title": "Content Hash Migration Workspace",
        "name": "Content Hash Migration Workspace",
        "description": None,
        "goal": "Migration coverage",
        "default_language": "en",
        "library_policy": {},
        "write_policy": {},
        "citation_policy": {},
        "agent_injection_policy": {},
        "review_policy": {},
        "lint_policy": {},
        "auto_fix_enabled": False,
        "visibility": "private",
        "created_by_actor_id": actor_id.hex,
        "owner_actor_id": actor_id.hex,
        "owner_user_id": None,
        "updated_at": now,
        "created_at": now,
    }
    if "directory_title_key" in workspaces.c:
        values["directory_title_key"] = "content hash migration workspace"
    connection.execute(workspaces.insert().values(**values))


def _insert_page(connection, pages, *, page_id, workspace_id, slug, now):
    connection.execute(
        pages.insert().values(
            id=page_id.hex,
            workspace_id=workspace_id.hex,
            slug=slug,
            title="Content Hash Migration",
            page_type="overview",
            tags=[],
            aliases=[],
            lifecycle_status="published",
            updated_at=now,
            created_at=now,
        )
    )


def _insert_version(
    connection,
    versions,
    *,
    version_id,
    page_id,
    workspace_id,
    body_md,
    content_hash,
    now,
):
    connection.execute(
        versions.insert().values(
            id=version_id.hex,
            page_id=page_id.hex,
            workspace_id=workspace_id.hex,
            parent_version_id=None,
            version_no=1,
            body_md=body_md,
            rendering_profile="gfm_mermaid",
            content_hash=content_hash,
            graph_status="linear",
            actor_type="human",
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
            created_at=now,
        )
    )


def _insert_library_item(connection, items, *, item_id, workspace_id, uri, now):
    connection.execute(
        items.insert().values(
            id=item_id.hex,
            workspace_id=workspace_id.hex,
            connector_type="manual",
            format_hint="unknown",
            uri=uri,
            metadata={},
            trust_level="unknown",
            lifecycle_status="registered",
            created_at=now,
            updated_at=now,
        )
    )


def _insert_library_item_version(
    connection,
    versions,
    *,
    version_id,
    item_id,
    version_no,
    content_hash,
    raw_storage_ref,
    now,
):
    connection.execute(
        versions.insert().values(
            id=version_id.hex,
            library_item_id=item_id.hex,
            version_no=version_no,
            content_hash=content_hash,
            raw_storage_ref=raw_storage_ref,
            fetched_at=now,
            processing_status="completed",
            preview_status="unavailable",
            created_at=now,
            updated_at=now,
        )
    )


def _insert_chunk(
    connection,
    chunks,
    *,
    chunk_id,
    workspace_id,
    document_kind,
    document_id,
    source_version_id,
    source_content_hash,
    ordinal,
    now,
):
    connection.execute(
        chunks.insert().values(
            id=chunk_id.hex,
            workspace_id=workspace_id.hex,
            document_kind=document_kind,
            document_id=document_id.hex,
            content_scope="published",
            source_version_id=source_version_id.hex,
            ordinal=ordinal,
            char_start=0,
            char_end=len("chunk text"),
            chunk_text="chunk text",
            chunk_text_hash="0" * 64,
            token_count=2,
            source_content_hash=source_content_hash,
            created_at=now,
        )
    )


def test_migration_backfills_only_the_stale_frontmatter_shaped_hash(
    tmp_path: Path,
):
    """The lost-update repro's stored side: a row written before the fix has
    a stale hash that must be corrected, while an already-correct row (any
    body without that exact shape) must be left untouched byte-for-byte.
    Repeating the real backfill phase must leave both results unchanged.
    """
    database_url = _database_url(tmp_path, "content-hash-0013.db")
    config = _config(database_url)
    engine = _upgrade_to_content_hash_boundary(config, database_url)

    workspace_id = uuid4()
    actor_id = uuid4()
    page_id = uuid4()
    affected_version_id = uuid4()
    clean_body = "# Clean\n\nnothing special here.\n"
    clean_version_id = uuid4()
    correct_hash_for_clean_body = calculate_content_hash(clean_body)
    now = datetime.now(UTC)

    try:
        metadata = sa.MetaData()
        workspaces = sa.Table("workspace", metadata, autoload_with=engine)
        pages = sa.Table("context_page", metadata, autoload_with=engine)
        versions = sa.Table("context_page_version", metadata, autoload_with=engine)
        with engine.begin() as connection:
            _insert_workspace(
                connection,
                workspaces,
                workspace_id=workspace_id,
                actor_id=actor_id,
                slug="content-hash-migration-workspace",
                now=now,
            )
            _insert_page(
                connection,
                pages,
                page_id=page_id,
                workspace_id=workspace_id,
                slug="content-hash-migration-affected",
                now=now,
            )
            # Simulates a row written by the pre-fix server: the stored hash
            # is what the OLD (stripping) algorithm produced, which disagrees
            # with a fresh recompute under the fixed one.
            _insert_version(
                connection,
                versions,
                version_id=affected_version_id,
                page_id=page_id,
                workspace_id=workspace_id,
                body_md=_AFFECTED_BODY,
                content_hash=_STALE_HASH_FOR_AFFECTED_BODY,
                now=now,
            )
            clean_page_id = uuid4()
            _insert_page(
                connection,
                pages,
                page_id=clean_page_id,
                workspace_id=workspace_id,
                slug="content-hash-migration-clean",
                now=now,
            )
            _insert_version(
                connection,
                versions,
                version_id=clean_version_id,
                page_id=clean_page_id,
                workspace_id=workspace_id,
                body_md=clean_body,
                content_hash=correct_hash_for_clean_body,
                now=now,
            )

        _run_steps(engine, content_hash_phase.upgrade)

        engine.dispose()
        engine = create_engine(database_url)
        with engine.connect() as connection:
            versions = sa.Table(
                "context_page_version", sa.MetaData(), autoload_with=engine
            )
            affected_after_first = connection.execute(
                sa.select(versions.c.content_hash).where(
                    versions.c.id == affected_version_id.hex
                )
            ).one()
            clean_after_first = connection.execute(
                sa.select(versions.c.content_hash).where(
                    versions.c.id == clean_version_id.hex
                )
            ).one()

        _run_steps(engine, content_hash_phase.upgrade)

        with engine.connect() as connection:
            affected_after_second = connection.execute(
                sa.select(versions.c.content_hash).where(
                    versions.c.id == affected_version_id.hex
                )
            ).one()
            clean_after_second = connection.execute(
                sa.select(versions.c.content_hash).where(
                    versions.c.id == clean_version_id.hex
                )
            ).one()
    finally:
        engine.dispose()

    assert affected_after_first.content_hash != _STALE_HASH_FOR_AFFECTED_BODY
    assert affected_after_first.content_hash == calculate_content_hash(_AFFECTED_BODY)
    # The already-correct row's hash is untouched byte-for-byte, not merely
    # "still correct" -- proves the migration skips rows it does not need to
    # write, not that it happens to recompute the same value every time.
    assert clean_after_first.content_hash == correct_hash_for_clean_body
    assert affected_after_second.content_hash == affected_after_first.content_hash
    assert clean_after_second.content_hash == clean_after_first.content_hash


def test_migration_backfills_library_item_version_from_recoverable_inline_text(
    tmp_path: Path,
):
    """LibraryService._item_content_hash hashes raw_text through this same
    contract only when the caller supplied literal text and no explicit ref
    -- the exact condition that also makes LibraryService._raw_storage_ref
    store an "inline-text:" ref. That prefix is the provable signal; a
    ``file://`` ref (or no ref at all, e.g. a binary item hashed some other
    way) is not recoverable from the database and must be left alone.
    """
    database_url = _database_url(tmp_path, "content-hash-0013-library.db")
    config = _config(database_url)
    engine = _upgrade_to_content_hash_boundary(config, database_url)

    workspace_id = uuid4()
    actor_id = uuid4()
    now = datetime.now(UTC)

    affected_item_id = uuid4()
    affected_version_id = uuid4()

    clean_body = "# Clean library text\n\nnothing special here.\n"
    clean_item_id = uuid4()
    clean_version_id = uuid4()
    correct_hash_for_clean_body = calculate_content_hash(clean_body)

    unrecoverable_item_id = uuid4()
    unrecoverable_version_id = uuid4()
    # A file:// ref: this row's content_hash may or may not be a text hash,
    # but the text itself is not in the database -- reading the file from
    # inside a migration is exactly the guess this backfill must not make.
    unrecoverable_ref = "file:///not/actually/read.md"
    unrecoverable_stored_hash = "f" * 64

    try:
        metadata = sa.MetaData()
        workspaces = sa.Table("workspace", metadata, autoload_with=engine)
        items = sa.Table("library_item", metadata, autoload_with=engine)
        versions = sa.Table("library_item_version", metadata, autoload_with=engine)
        with engine.begin() as connection:
            _insert_workspace(
                connection,
                workspaces,
                workspace_id=workspace_id,
                actor_id=actor_id,
                slug="content-hash-migration-library-workspace",
                now=now,
            )
            _insert_library_item(
                connection,
                items,
                item_id=affected_item_id,
                workspace_id=workspace_id,
                uri="https://example.test/affected",
                now=now,
            )
            _insert_library_item_version(
                connection,
                versions,
                version_id=affected_version_id,
                item_id=affected_item_id,
                version_no=1,
                content_hash=_STALE_HASH_FOR_AFFECTED_BODY,
                raw_storage_ref=inline_text_ref(_AFFECTED_BODY),
                now=now,
            )
            _insert_library_item(
                connection,
                items,
                item_id=clean_item_id,
                workspace_id=workspace_id,
                uri="https://example.test/clean",
                now=now,
            )
            _insert_library_item_version(
                connection,
                versions,
                version_id=clean_version_id,
                item_id=clean_item_id,
                version_no=1,
                content_hash=correct_hash_for_clean_body,
                raw_storage_ref=inline_text_ref(clean_body),
                now=now,
            )
            _insert_library_item(
                connection,
                items,
                item_id=unrecoverable_item_id,
                workspace_id=workspace_id,
                uri="https://example.test/unrecoverable",
                now=now,
            )
            _insert_library_item_version(
                connection,
                versions,
                version_id=unrecoverable_version_id,
                item_id=unrecoverable_item_id,
                version_no=1,
                content_hash=unrecoverable_stored_hash,
                raw_storage_ref=unrecoverable_ref,
                now=now,
            )

        _run_steps(engine, content_hash_phase.upgrade)

        engine.dispose()
        engine = create_engine(database_url)
        with engine.connect() as connection:
            versions = sa.Table(
                "library_item_version", sa.MetaData(), autoload_with=engine
            )
            affected_row = connection.execute(
                sa.select(versions.c.content_hash).where(
                    versions.c.id == affected_version_id.hex
                )
            ).one()
            clean_row = connection.execute(
                sa.select(versions.c.content_hash).where(
                    versions.c.id == clean_version_id.hex
                )
            ).one()
            unrecoverable_row = connection.execute(
                sa.select(versions.c.content_hash).where(
                    versions.c.id == unrecoverable_version_id.hex
                )
            ).one()
    finally:
        engine.dispose()

    assert affected_row.content_hash != _STALE_HASH_FOR_AFFECTED_BODY
    assert affected_row.content_hash == calculate_content_hash(_AFFECTED_BODY)
    assert clean_row.content_hash == correct_hash_for_clean_body
    # Not recoverable from the database alone -- left exactly as seeded,
    # not guessed at.
    assert unrecoverable_row.content_hash == unrecoverable_stored_hash


def test_migration_keeps_chunk_source_content_hash_a_faithful_copy(tmp_path: Path):
    """content_chunk.source_content_hash is only ever a copy of the version
    row it was chunked from -- for a context_page chunk that copy is
    unambiguous, but a library_item chunk MAY instead hold a semantic-
    overlay hash (a different value db_native.py writes in its place). A
    chunk is only updated when its current value equals the version's OLD
    (pre-backfill) hash exactly -- proof it really is a copy, not an
    overlay hash this migration must not disturb.
    """
    database_url = _database_url(tmp_path, "content-hash-0013-chunks.db")
    config = _config(database_url)
    engine = _upgrade_to_content_hash_boundary(config, database_url)

    workspace_id = uuid4()
    actor_id = uuid4()
    now = datetime.now(UTC)

    page_id = uuid4()
    context_version_id = uuid4()
    context_chunk_id = uuid4()

    library_item_id = uuid4()
    library_version_id = uuid4()
    library_chunk_id = uuid4()
    overlay_chunk_id = uuid4()
    overlay_hash = "a" * 64  # stands in for a semantic-overlay content hash,
    # deliberately NOT equal to the stale version hash, so it must survive
    # untouched.

    try:
        metadata = sa.MetaData()
        workspaces = sa.Table("workspace", metadata, autoload_with=engine)
        pages = sa.Table("context_page", metadata, autoload_with=engine)
        context_versions = sa.Table(
            "context_page_version", metadata, autoload_with=engine
        )
        items = sa.Table("library_item", metadata, autoload_with=engine)
        library_versions = sa.Table(
            "library_item_version", metadata, autoload_with=engine
        )
        chunks = sa.Table("content_chunk", metadata, autoload_with=engine)
        with engine.begin() as connection:
            _insert_workspace(
                connection,
                workspaces,
                workspace_id=workspace_id,
                actor_id=actor_id,
                slug="content-hash-migration-chunk-workspace",
                now=now,
            )
            _insert_page(
                connection,
                pages,
                page_id=page_id,
                workspace_id=workspace_id,
                slug="content-hash-migration-chunk-page",
                now=now,
            )
            _insert_version(
                connection,
                context_versions,
                version_id=context_version_id,
                page_id=page_id,
                workspace_id=workspace_id,
                body_md=_AFFECTED_BODY,
                content_hash=_STALE_HASH_FOR_AFFECTED_BODY,
                now=now,
            )
            # A context_page chunk: db_native.py always copies
            # context_page_version.content_hash verbatim, so this MUST track
            # the version's corrected hash.
            _insert_chunk(
                connection,
                chunks,
                chunk_id=context_chunk_id,
                workspace_id=workspace_id,
                document_kind="context_page",
                document_id=page_id,
                source_version_id=context_version_id,
                source_content_hash=_STALE_HASH_FOR_AFFECTED_BODY,
                ordinal=0,
                now=now,
            )
            _insert_library_item(
                connection,
                items,
                item_id=library_item_id,
                workspace_id=workspace_id,
                uri="https://example.test/chunk-source",
                now=now,
            )
            _insert_library_item_version(
                connection,
                versions=library_versions,
                version_id=library_version_id,
                item_id=library_item_id,
                version_no=1,
                content_hash=_STALE_HASH_FOR_AFFECTED_BODY,
                raw_storage_ref=inline_text_ref(_AFFECTED_BODY),
                now=now,
            )
            # A library_item chunk whose stored hash matches the version's
            # OLD hash: proven to be a plain copy, must be corrected.
            _insert_chunk(
                connection,
                chunks,
                chunk_id=library_chunk_id,
                workspace_id=workspace_id,
                document_kind="library_item",
                document_id=library_item_id,
                source_version_id=library_version_id,
                source_content_hash=_STALE_HASH_FOR_AFFECTED_BODY,
                ordinal=0,
                now=now,
            )
            # A library_item chunk whose stored hash does NOT match the
            # version's old hash at all: stands in for an overlay hash. Must
            # survive untouched -- there is no version-hash value it could
            # have been proven to copy.
            _insert_chunk(
                connection,
                chunks,
                chunk_id=overlay_chunk_id,
                workspace_id=workspace_id,
                document_kind="library_item",
                document_id=library_item_id,
                source_version_id=library_version_id,
                source_content_hash=overlay_hash,
                ordinal=1,
                now=now,
            )

        _run_steps(engine, content_hash_phase.upgrade)

        engine.dispose()
        engine = create_engine(database_url)
        with engine.connect() as connection:
            chunks = sa.Table("content_chunk", sa.MetaData(), autoload_with=engine)
            context_chunk_row = connection.execute(
                sa.select(chunks.c.source_content_hash).where(
                    chunks.c.id == context_chunk_id.hex
                )
            ).one()
            library_chunk_row = connection.execute(
                sa.select(chunks.c.source_content_hash).where(
                    chunks.c.id == library_chunk_id.hex
                )
            ).one()
            overlay_chunk_row = connection.execute(
                sa.select(chunks.c.source_content_hash).where(
                    chunks.c.id == overlay_chunk_id.hex
                )
            ).one()
    finally:
        engine.dispose()

    fresh_hash = calculate_content_hash(_AFFECTED_BODY)
    assert context_chunk_row.source_content_hash == fresh_hash
    assert library_chunk_row.source_content_hash == fresh_hash
    # Never touched: nothing proved this was a copy of the version hash.
    assert overlay_chunk_row.source_content_hash == overlay_hash
