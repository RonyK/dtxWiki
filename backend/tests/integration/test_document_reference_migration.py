"""One additive reference-schema traversal per supported database dialect."""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.document_reference import (
    ContextCitationPublication,
    ContextDocumentReference,
    ContextPublishedCitation,
)
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.ops.alembic_migration_graph import schema_release_head
from backend.tests.integration.test_release_schema_migration import _config
from backend.tests.utils.migrated_sqlite_templates import shared_templates

PARENT = "7c2bde1490af"
REVISION = "c58f1d2e890a"
TABLES = {
    "context_document_reference",
    "context_published_citation",
    "context_citation_publication",
}


def _sqlstate(error):
    original = error.orig
    structured = original.args[0] if original.args else None
    return (
        getattr(original, "sqlstate", None)
        or getattr(original, "pgcode", None)
        or (structured.get("C") if isinstance(structured, dict) else None)
    )


@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_reference_schema_round_trip_preserves_sources_without_backfill(
    dialect, tmp_path, tmp_path_factory
):
    # Real dialect constraints and transactional DDL require this integration
    # traversal. XOR, duplicate and workspace isolation share its seeded rows.
    if dialect == "sqlite":
        url = shared_templates(tmp_path_factory).clone(
            "rev_efcbcf724889", tmp_path / "references.sqlite"
        )
    else:
        url = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
        if not url or not url.startswith("postgresql"):
            pytest.skip("requires a dedicated PostgreSQL migration database")
    engine = sa.create_engine(url)
    if dialect == "sqlite":

        @sa.event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

    config, migrated = _config(url), False
    try:
        if dialect == "postgresql":
            assert not (set(sa.inspect(engine).get_table_names()) - {"alembic_version"})
        # Start on this revision's own ancestry. Downgrading a merged head
        # would leave the independent Chats index branch stamped alongside
        # PARENT, correctly triggering the production multiple-head guard.
        command.upgrade(config, PARENT)
        migrated = True
        assert TABLES.isdisjoint(sa.inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert set(
                connection.scalars(sa.text("SELECT version_num FROM alembic_version"))
            ) == {PARENT}
        with Session(engine) as session, session.begin():
            workspace = seed_default_workspace(session)
            foreign = seed_default_workspace(session, slug="foreign-references")
            source = ContextPage(
                workspace_id=workspace.id, slug="source-reference", title="Source"
            )
            target = ContextPage(
                workspace_id=workspace.id, slug="target-reference", title="Target"
            )
            hidden = ContextPage(
                workspace_id=foreign.id, slug="foreign-reference", title="Foreign"
            )
            session.add_all([source, target, hidden])
            session.flush()
            version = ContextPageVersion(
                workspace_id=workspace.id,
                page_id=source.id,
                body_md="# Source\n",
                content_hash="unchanged",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="migration",
            )
            session.add(version)
            session.flush()
            workspace_id, version_id, target_id, foreign_id = (
                workspace.id,
                version.id,
                target.id,
                hidden.id,
            )
        command.upgrade(config, REVISION)
        assert TABLES <= set(sa.inspect(engine).get_table_names())
        for table in ("context_document_reference", "context_published_citation"):
            indexes = sa.inspect(engine).get_indexes(table)
            assert {
                tuple(index["column_names"])
                for index in indexes
                if index["column_names"][0] == "workspace_id"
            } == {
                ("workspace_id", column)
                for column in ("context_page_id", "library_item_id", "agent_page_id")
            }
        with Session(engine) as session, session.begin():
            assert (
                session.scalar(
                    sa.select(sa.func.count()).select_from(ContextDocumentReference)
                )
                == 0
            )
            assert session.get(ContextPageVersion, version_id).body_md == "# Source\n"
            row = ContextDocumentReference(
                workspace_id=workspace_id,
                owner_version_id=version_id,
                target_kind="context_page",
                context_page_id=target_id,
            )
            session.add(row)
            session.flush()
            with (
                pytest.raises(sa.exc.DBAPIError) as duplicate_error,
                session.begin_nested(),
            ):
                session.add(
                    ContextDocumentReference(
                        workspace_id=workspace_id,
                        owner_version_id=version_id,
                        target_kind="context_page",
                        context_page_id=target_id,
                    )
                )
                session.flush()
            if dialect == "postgresql":
                assert _sqlstate(duplicate_error.value) == "23505"
            else:
                assert isinstance(duplicate_error.value, sa.exc.IntegrityError)
            with (
                pytest.raises(sa.exc.DBAPIError) as foreign_error,
                session.begin_nested(),
            ):
                session.add(
                    ContextDocumentReference(
                        workspace_id=workspace_id,
                        owner_version_id=version_id,
                        target_kind="context_page",
                        context_page_id=foreign_id,
                    )
                )
                session.flush()
            if dialect == "postgresql":
                assert _sqlstate(foreign_error.value) == "23503"
            else:
                assert isinstance(foreign_error.value, sa.exc.IntegrityError)
            with pytest.raises(sa.exc.DBAPIError) as xor_error, session.begin_nested():
                session.add(
                    ContextDocumentReference(
                        workspace_id=workspace_id,
                        owner_version_id=version_id,
                        target_kind="context_page",
                    )
                )
                session.flush()
            if dialect == "postgresql":
                assert _sqlstate(xor_error.value) == "23514"
            else:
                assert isinstance(xor_error.value, sa.exc.IntegrityError)
            session.add(
                ContextCitationPublication(
                    workspace_id=workspace_id,
                    version_id=version_id,
                    fingerprint="frozen",
                )
            )
            session.add(
                ContextPublishedCitation(
                    workspace_id=workspace_id,
                    source_version_id=version_id,
                    target_kind="context_page",
                    context_page_id=target_id,
                )
            )
        command.downgrade(config, PARENT)
        assert TABLES.isdisjoint(sa.inspect(engine).get_table_names())
        # Rolling back references must retain the preceding co-access schema.
        assert "co_access_pair" in sa.inspect(engine).get_table_names()
        command.upgrade(config, REVISION)
        with Session(engine) as session:
            assert session.get(ContextPageVersion, version_id).body_md == "# Source\n"
            for model in (
                ContextDocumentReference,
                ContextPublishedCitation,
                ContextCitationPublication,
            ):
                assert (
                    session.scalar(sa.select(sa.func.count()).select_from(model)) == 0
                )
        # The same traversal must still converge with the parallel index
        # branch to the declared single head without rewriting source bytes.
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert set(
                connection.scalars(sa.text("SELECT version_num FROM alembic_version"))
            ) == {schema_release_head("0.2.0")}
        with Session(engine) as session:
            assert session.get(ContextPageVersion, version_id).body_md == "# Source\n"
    finally:
        try:
            if dialect == "postgresql" and migrated:
                command.downgrade(config, "base")
        finally:
            engine.dispose()
