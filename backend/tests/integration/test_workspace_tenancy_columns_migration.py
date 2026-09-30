"""Verify the final Epic-1 phase backfill for indirect workspace ownership.

Backfill ``workspace_id`` onto every table that previously reached its workspace
only by joining through a parent.

Mirrors the established pattern (test_context_history_migration_
normalization.py, test_context_content_hash_migration.py): a real Alembic
Config creates the 0001 baseline against a temp-file SQLite database, then the
Epic-1 phase helpers build the pre-final boundary. Rows are seeded through Core
tables reflected at that boundary, not through current-head ORM models that
already carry the columns the final phase adds.

Two independent workspaces are seeded so a backfill that accidentally
cross-wires parent and child (join predicate typo, wrong table) shows up as a
wrong-but-present workspace_id, not merely a missing one.
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

ACTOR_ID = "00000000-0000-0000-0000-000000000010"

epic1 = import_module("backend.migrations.versions.0002_epic1")
workspace_phase = import_module(
    "backend.migrations.revision_0002.workspace_schema"
)
mcp_phase = import_module("backend.migrations.revision_0002.identity_access_schema")
media_phase = import_module("backend.migrations.revision_0002.library_media_schema")
public_phase = import_module(
    "backend.migrations.revision_0002.context_schema"
)
library_phase = import_module(
    "backend.migrations.revision_0002.library_policy"
)
content_hash_phase = import_module(
    "backend.migrations.revision_0002.context_data"
)
final_phase = import_module("backend.migrations.revision_0002.tenancy_schema")


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


def _upgrade_to_pre_final_boundary(config: Config, engine) -> None:
    command.upgrade(config, "0001_initial_release")
    _run_steps(
        engine,
        epic1._upgrade_base_schema,
        workspace_phase.upgrade,
        mcp_phase.upgrade,
        public_phase.upgrade,
        media_phase.upgrade,
        library_phase.upgrade,
        content_hash_phase.upgrade,
    )


def _insert_workspace(connection, workspaces, *, workspace_id, actor_id, slug, now):
    values = {
        "id": workspace_id.hex,
        "slug": slug,
        "title": "Tenancy Migration Workspace",
        "name": "Tenancy Migration Workspace",
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
        values["directory_title_key"] = slug.replace("-", " ")
    connection.execute(workspaces.insert().values(**values))


def _insert_page(connection, pages, *, page_id, workspace_id, slug, now):
    connection.execute(
        pages.insert().values(
            id=page_id.hex,
            workspace_id=workspace_id.hex,
            slug=slug,
            title="Tenancy Migration Page",
            page_type="overview",
            tags=[],
            aliases=[],
            lifecycle_status="draft",
            updated_at=now,
            created_at=now,
        )
    )


def _insert_page_version(
    connection, versions, *, version_id, page_id, workspace_id, now
):
    connection.execute(
        versions.insert().values(
            id=version_id.hex,
            page_id=page_id.hex,
            workspace_id=workspace_id.hex,
            parent_version_id=None,
            version_no=1,
            body_md="# Tenancy Migration\n\nbody",
            rendering_profile="gfm_mermaid",
            content_hash="0" * 64,
            graph_status="linear",
            actor_type="human",
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
            created_at=now,
        )
    )


def _insert_page_provenance(connection, table, *, page_id, now):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            created_at=now,
            page_id=page_id.hex,
            source_kind="ask_turn",
            source_id=uuid4().hex,
        )
    )


def _insert_context_claim(connection, table, *, page_version_id, now):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            created_at=now,
            page_version_id=page_version_id.hex,
            claim_text="a claim",
            location_in_page={},
            claim_type="fact",
            confidence="unknown",
            status="active",
            is_core=False,
        )
    )


def _insert_context_citation(
    connection,
    table,
    *,
    citation_type,
    library_item_version_id=None,
    library_artifact_id=None,
    cited_context_page_version_id=None,
    now,
):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            created_at=now,
            target_kind="context_claim",
            target_id=uuid4().hex,
            target_location={},
            library_item_version_id=(
                library_item_version_id.hex if library_item_version_id else None
            ),
            library_artifact_id=(
                library_artifact_id.hex if library_artifact_id else None
            ),
            cited_context_page_version_id=(
                cited_context_page_version_id.hex
                if cited_context_page_version_id
                else None
            ),
            location={},
            citation_type=citation_type,
            verification_status="weak",
            created_via="cite_tool",
        )
    )


def _insert_chunk(connection, chunks, *, chunk_id, workspace_id, now):
    connection.execute(
        chunks.insert().values(
            id=chunk_id.hex,
            workspace_id=workspace_id.hex,
            document_kind="context_page",
            document_id=uuid4().hex,
            content_scope="published",
            source_version_id=None,
            ordinal=0,
            char_start=0,
            char_end=10,
            chunk_text="chunk text",
            chunk_text_hash="0" * 64,
            token_count=2,
            created_at=now,
        )
    )


def _insert_chunk_embedding(connection, table, *, chunk_id, now):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            created_at=now,
            chunk_id=chunk_id.hex,
            provider="local",
            model="test-model",
            dim=1,
            embedding=b"\x00\x00\x00\x00",
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


def _insert_library_item_version(connection, versions, *, version_id, item_id, now):
    connection.execute(
        versions.insert().values(
            id=version_id.hex,
            library_item_id=item_id.hex,
            version_no=1,
            content_hash="0" * 64,
            raw_storage_ref="inline-text:0" * 5,
            fetched_at=now,
            processing_status="completed",
            preview_status="unavailable",
            created_at=now,
            updated_at=now,
        )
    )


# A-1: library_run and
# library_attempt now carry workspace_id directly from creation (denormalized
# from library_item_version at INSERT time by application code, same as
# context_media_asset already did before this rename) rather than getting it
# via this phase's generic parent-chain backfill -- so unlike the retired
# library_conversion_run/attempt and library_preview_run/attempt tables they
# replace, these two are seeded here only as required FK-target data for
# library_artifact/library_projection_outbox, not as a backfill assertion
# subject themselves. library_artifact was already this way before the
# rename too (it just didn't previously require a real attempt/blob chain to
# exist at all -- the legacy nullable-attempt shape retired by the same
# change).
def _insert_library_run(
    connection, table, *, run_id, workspace_id, library_item_version_id, stage, now
):
    connection.execute(
        table.insert().values(
            id=run_id.hex,
            created_at=now,
            workspace_id=workspace_id.hex,
            library_item_version_id=library_item_version_id.hex,
            stage=stage,
            run_seq=1,
            reason="initial",
            status="completed",
            provider_chain_json=[],
            policy_revision="v1",
            output_schema_version="v1",
            idempotency_key=uuid4().hex,
            updated_at=now,
        )
    )


def _insert_library_attempt(
    connection,
    table,
    *,
    attempt_id,
    run_id,
    workspace_id,
    library_item_version_id,
    stage,
    now,
):
    connection.execute(
        table.insert().values(
            id=attempt_id.hex,
            created_at=now,
            workspace_id=workspace_id.hex,
            run_id=run_id.hex,
            library_item_version_id=library_item_version_id.hex,
            stage=stage,
            provider_id="provider",
            provider_version="1",
            provider_spi_version="1",
            config_hash="0" * 64,
            input_sha256="0" * 64,
            output_schema_version="v1",
            status="completed",
            chain_index=0,
            transport_retry_count=0,
            execution_generation=1,
            warnings_json=[],
            provenance_json={},
            metrics_json={},
        )
    )


def _insert_media_write_intent(connection, table, *, workspace_id, now):
    connection.execute(
        table.insert().values(workspace_id=workspace_id.hex, updated_at=now)
    )


def _insert_stored_blob(connection, table, *, workspace_id, blob_id, sha256, now):
    connection.execute(
        table.insert().values(
            workspace_id=workspace_id.hex,
            id=blob_id.hex,
            sha256=sha256,
            created_at=now,
            updated_at=now,
        )
    )


def _insert_blob_incarnation(connection, table, *, workspace_id, blob_id, now):
    connection.execute(
        table.insert().values(
            workspace_id=workspace_id.hex,
            blob_id=blob_id.hex,
            blob_epoch=1,
            byte_size=4,
            storage_key=f"blob/{blob_id.hex}/1",
            state="available",
            created_at=now,
            available_at=now,
        )
    )


def _insert_library_artifact(
    connection,
    table,
    *,
    artifact_id,
    workspace_id,
    library_item_version_id,
    attempt_id,
    stage,
    blob_id,
    now,
):
    connection.execute(
        table.insert().values(
            id=artifact_id.hex,
            created_at=now,
            workspace_id=workspace_id.hex,
            library_item_version_id=library_item_version_id.hex,
            attempt_id=attempt_id.hex,
            stage=stage,
            blob_workspace_id=workspace_id.hex,
            blob_id=blob_id.hex,
            blob_epoch=1,
            role="markdown",
            kind="markdown",
            media_type="text/markdown",
            content_sha256="0" * 64,
            byte_size=4,
            ordinal=0,
        )
    )


def _insert_projection_outbox(
    connection,
    table,
    *,
    library_item_version_id,
    conversion_attempt_id,
    now,
):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            created_at=now,
            library_item_version_id=library_item_version_id.hex,
            conversion_attempt_id=conversion_attempt_id.hex,
            stage="conversion",
            projection_kind="fts",
            generation=1,
            status="completed",
            idempotency_key=uuid4().hex,
            retry_count=0,
            updated_at=now,
        )
    )


def test_migration_backfills_workspace_id_from_the_correct_parent_chain(
    tmp_path: Path,
):
    database_url = _database_url(tmp_path, "tenancy-columns.db")
    config = _config(database_url)
    engine = create_engine(database_url)
    _upgrade_to_pre_final_boundary(config, engine)

    now = datetime.now(UTC)
    actor_id = uuid4()

    # Two independent workspaces so a cross-wired join shows up as a wrong
    # value rather than merely a missing one.
    ws_a = uuid4()
    ws_b = uuid4()

    page_a = uuid4()
    version_a = uuid4()
    page_b = uuid4()
    version_b = uuid4()

    chunk_a = uuid4()

    item_a = uuid4()
    item_version_a = uuid4()
    run_a = uuid4()
    attempt_a = uuid4()
    artifact_a = uuid4()

    item_b = uuid4()
    item_version_b = uuid4()

    try:
        metadata = sa.MetaData()
        workspaces = sa.Table("workspace", metadata, autoload_with=engine)
        pages = sa.Table("context_page", metadata, autoload_with=engine)
        page_versions = sa.Table("context_page_version", metadata, autoload_with=engine)
        page_provenance = sa.Table("page_provenance", metadata, autoload_with=engine)
        context_claim = sa.Table("context_claim", metadata, autoload_with=engine)
        context_citation = sa.Table("context_citation", metadata, autoload_with=engine)
        content_chunk = sa.Table("content_chunk", metadata, autoload_with=engine)
        chunk_embedding = sa.Table("chunk_embedding", metadata, autoload_with=engine)
        library_item = sa.Table("library_item", metadata, autoload_with=engine)
        library_item_version = sa.Table(
            "library_item_version", metadata, autoload_with=engine
        )
        library_run = sa.Table("library_run", metadata, autoload_with=engine)
        library_attempt = sa.Table("library_attempt", metadata, autoload_with=engine)
        library_artifact = sa.Table("library_artifact", metadata, autoload_with=engine)
        library_projection_outbox = sa.Table(
            "library_projection_outbox", metadata, autoload_with=engine
        )
        media_write_intent = sa.Table(
            "workspace_media_write_intent", metadata, autoload_with=engine
        )
        stored_blob = sa.Table("stored_blob", metadata, autoload_with=engine)
        blob_incarnation = sa.Table(
            "blob_incarnation", metadata, autoload_with=engine
        )

        with engine.begin() as connection:
            _insert_workspace(
                connection,
                workspaces,
                workspace_id=ws_a,
                actor_id=actor_id,
                slug="tenancy-migration-a",
                now=now,
            )
            _insert_workspace(
                connection,
                workspaces,
                workspace_id=ws_b,
                actor_id=actor_id,
                slug="tenancy-migration-b",
                now=now,
            )

            _insert_page(
                connection,
                pages,
                page_id=page_a,
                workspace_id=ws_a,
                slug="tenancy-a",
                now=now,
            )
            _insert_page_version(
                connection,
                page_versions,
                version_id=version_a,
                page_id=page_a,
                workspace_id=ws_a,
                now=now,
            )
            _insert_page(
                connection,
                pages,
                page_id=page_b,
                workspace_id=ws_b,
                slug="tenancy-b",
                now=now,
            )
            _insert_page_version(
                connection,
                page_versions,
                version_id=version_b,
                page_id=page_b,
                workspace_id=ws_b,
                now=now,
            )

            _insert_page_provenance(
                connection, page_provenance, page_id=page_a, now=now
            )
            _insert_page_provenance(
                connection, page_provenance, page_id=page_b, now=now
            )
            _insert_context_claim(
                connection, context_claim, page_version_id=version_a, now=now
            )
            _insert_context_claim(
                connection, context_claim, page_version_id=version_b, now=now
            )

            _insert_chunk(
                connection, content_chunk, chunk_id=chunk_a, workspace_id=ws_a, now=now
            )
            _insert_chunk_embedding(
                connection, chunk_embedding, chunk_id=chunk_a, now=now
            )

            _insert_library_item(
                connection,
                library_item,
                item_id=item_a,
                workspace_id=ws_a,
                uri="file:///a",
                now=now,
            )
            _insert_library_item_version(
                connection,
                library_item_version,
                version_id=item_version_a,
                item_id=item_a,
                now=now,
            )
            _insert_library_item(
                connection,
                library_item,
                item_id=item_b,
                workspace_id=ws_b,
                uri="file:///b",
                now=now,
            )
            _insert_library_item_version(
                connection,
                library_item_version,
                version_id=item_version_b,
                item_id=item_b,
                now=now,
            )

            # library_run/library_attempt/library_artifact carry workspace_id
            # from creation now (A-1), so workspace_id=ws_a is supplied
            # directly here rather than left for backfill -- see the
            # helpers' shared docstring-style comment above.
            blob_a = uuid4()
            _insert_media_write_intent(
                connection, media_write_intent, workspace_id=ws_a, now=now
            )
            _insert_stored_blob(
                connection,
                stored_blob,
                workspace_id=ws_a,
                blob_id=blob_a,
                sha256="1" * 64,
                now=now,
            )
            _insert_blob_incarnation(
                connection, blob_incarnation, workspace_id=ws_a, blob_id=blob_a, now=now
            )
            _insert_library_run(
                connection,
                library_run,
                run_id=run_a,
                workspace_id=ws_a,
                library_item_version_id=item_version_a,
                stage="conversion",
                now=now,
            )
            _insert_library_attempt(
                connection,
                library_attempt,
                attempt_id=attempt_a,
                run_id=run_a,
                workspace_id=ws_a,
                library_item_version_id=item_version_a,
                stage="conversion",
                now=now,
            )
            _insert_library_artifact(
                connection,
                library_artifact,
                artifact_id=artifact_a,
                workspace_id=ws_a,
                library_item_version_id=item_version_a,
                attempt_id=attempt_a,
                stage="conversion",
                blob_id=blob_a,
                now=now,
            )
            _insert_projection_outbox(
                connection,
                library_projection_outbox,
                library_item_version_id=item_version_a,
                conversion_attempt_id=attempt_a,
                now=now,
            )

            # One context_citation row per XOR branch, all in workspace A.
            _insert_context_citation(
                connection,
                context_citation,
                citation_type="raw_source",
                library_item_version_id=item_version_a,
                now=now,
            )
            _insert_context_citation(
                connection,
                context_citation,
                citation_type="derived_artifact",
                library_artifact_id=artifact_a,
                now=now,
            )
            _insert_context_citation(
                connection,
                context_citation,
                citation_type="context_page",
                cited_context_page_version_id=version_a,
                now=now,
            )

        _run_steps(engine, final_phase.upgrade)

        engine.dispose()
        engine = create_engine(database_url)
        with engine.connect() as connection:

            def scalar(table_name, id_col, id_value):
                table = sa.Table(table_name, sa.MetaData(), autoload_with=engine)
                return connection.execute(
                    sa.select(table.c.workspace_id).where(
                        table.c[id_col] == id_value.hex
                    )
                ).scalar_one()

            assert scalar("context_page_version", "id", version_a) == ws_a.hex
            assert scalar("context_page_version", "id", version_b) == ws_b.hex

            page_prov_a = connection.execute(
                sa.text("SELECT workspace_id FROM page_provenance WHERE page_id = :p"),
                {"p": page_a.hex},
            ).scalar_one()
            assert page_prov_a == ws_a.hex

            claim_a = connection.execute(
                sa.text(
                    "SELECT workspace_id FROM context_claim WHERE page_version_id = :v"
                ),
                {"v": version_a.hex},
            ).scalar_one()
            assert claim_a == ws_a.hex

            assert scalar("chunk_embedding", "chunk_id", chunk_a) == ws_a.hex

            assert scalar("library_item_version", "id", item_version_a) == ws_a.hex
            assert scalar("library_item_version", "id", item_version_b) == ws_b.hex
            # library_run/library_attempt/library_artifact are NOT part of
            # this phase's backfill any more (A-1: workspace_id is set
            # directly at creation) -- these three assertions confirm the
            # final phase leaves their already-correct workspace_id alone
            # rather than testing a backfill that no longer applies to them.
            assert scalar("library_artifact", "id", artifact_a) == ws_a.hex
            assert scalar("library_run", "id", run_a) == ws_a.hex
            assert scalar("library_attempt", "id", attempt_a) == ws_a.hex
            assert (
                connection.execute(
                    sa.text(
                        "SELECT workspace_id FROM library_projection_outbox "
                        "WHERE conversion_attempt_id = :a"
                    ),
                    {"a": attempt_a.hex},
                ).scalar_one()
                == ws_a.hex
            )

            citations = connection.execute(
                sa.text("SELECT citation_type, workspace_id FROM context_citation")
            ).all()
            assert {row.citation_type: row.workspace_id for row in citations} == {
                "raw_source": ws_a.hex,
                "derived_artifact": ws_a.hex,
                "context_page": ws_a.hex,
            }

            for table_name in (
                "context_page_version",
                "page_provenance",
                "chunk_embedding",
                "library_item_version",
                "library_artifact",
                "context_claim",
                "context_citation",
                "library_run",
                "library_attempt",
                "library_projection_outbox",
            ):
                table = sa.Table(table_name, sa.MetaData(), autoload_with=engine)
                assert not table.c.workspace_id.nullable, table_name
    finally:
        engine.dispose()
