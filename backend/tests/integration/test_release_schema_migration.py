import re
import shutil
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models import Base, ContextPage, ContextPageVersion, PageLink
from backend.models.graph import GraphHistoryCoverage, GraphObservation
from backend.models.search_index import include_non_fts5_object
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.workspace_purge import purge_workspace_data
from backend.services.workspace_write import WorkspaceWriteService
from backend.tests.utils.migrated_sqlite_templates import shared_templates

INITIAL_REVISION = "bd62193d5cf6"
HEAD_REVISION = "efcbcf724889"
GRAPH_REVISION = "9aeab6944f25"


def _config(database_url: str) -> Config:
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _database_url(tmp_path, filename: str) -> str:
    return f"sqlite:///{tmp_path / filename}"


def _upgrade_head(tmp_path, filename: str):
    config = _config(_database_url(tmp_path, filename))
    command.upgrade(config, "head")
    return config, create_engine(config.get_main_option("sqlalchemy.url"))


def _insert_initial_release_workspace(connection, *, workspace_id, slug: str) -> None:
    """Seed a 0001 workspace without consulting the newer ORM table shape."""

    connection.execute(
        text(
            """
            INSERT INTO workspace (
                slug,
                title,
                name,
                description,
                goal,
                default_language,
                library_policy,
                write_policy,
                citation_policy,
                agent_injection_policy,
                review_policy,
                lint_policy,
                auto_fix_enabled,
                visibility,
                created_by_actor_id,
                owner_actor_id,
                owner_user_id,
                updated_at,
                id,
                created_at
            ) VALUES (
                :slug,
                :title,
                :title,
                NULL,
                'Migration test workspace',
                'en',
                '{}',
                '{}',
                '{}',
                '{}',
                '{}',
                '{}',
                0,
                'private',
                :actor_id,
                :actor_id,
                NULL,
                CURRENT_TIMESTAMP,
                :workspace_id,
                CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "actor_id": "00000000000000000000000000000010",
            "slug": slug,
            "title": slug.removeprefix("workspace-").upper(),
            "workspace_id": workspace_id.hex,
        },
    )


def _insert_initial_release_tree_node(
    connection,
    *,
    node_id,
    workspace_id,
    slug: str,
    sort_key: str,
    parent_id=None,
) -> None:
    """Seed a 0001 tree node without newer ORM-only columns or defaults."""

    connection.execute(
        text(
            """
            INSERT INTO workspace_tree_node (
                workspace_id,
                section,
                node_kind,
                parent_id,
                sort_key,
                title,
                slug,
                target_kind,
                target_id,
                is_system_node,
                can_have_children,
                primary_placement,
                visibility_status,
                created_by_actor_type,
                created_by_actor_id,
                updated_at,
                id,
                created_at
            ) VALUES (
                :workspace_id,
                'context',
                'folder',
                :parent_id,
                :sort_key,
                :title,
                :slug,
                NULL,
                NULL,
                0,
                1,
                1,
                'visible',
                NULL,
                NULL,
                CURRENT_TIMESTAMP,
                :node_id,
                CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "node_id": node_id.hex,
            "parent_id": parent_id.hex if parent_id is not None else None,
            "slug": slug,
            "sort_key": sort_key,
            "title": slug.replace("-", " ").title(),
            "workspace_id": workspace_id.hex,
        },
    )


def _insert_initial_release_page_link_source(
    connection,
    *,
    workspace_id,
    page_id,
    version_id,
) -> None:
    """Seed the 0001 source rows needed by a raw PageLink insert."""

    connection.execute(
        text(
            """
            INSERT INTO context_page (
                workspace_id,
                slug,
                title,
                current_version_id,
                page_type,
                tags,
                aliases,
                lifecycle_status,
                current_published_version_id,
                is_public,
                latest_draft_version_id,
                created_from_ask_session_id,
                created_from_ask_turn_ids,
                placement_parent_node_id,
                placement_reason,
                placement_confidence,
                created_by_actor_type,
                created_by_actor_id,
                updated_at,
                id,
                created_at
            ) VALUES (
                :workspace_id,
                'page-link-source',
                'Page Link Source',
                NULL,
                'concept',
                '[]',
                '[]',
                'draft',
                NULL,
                0,
                NULL,
                NULL,
                '[]',
                NULL,
                NULL,
                NULL,
                'human',
                NULL,
                CURRENT_TIMESTAMP,
                :page_id,
                CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "page_id": page_id.hex,
            "workspace_id": workspace_id.hex,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO context_page_version (
                page_id,
                parent_version_id,
                version_no,
                body_md,
                rendering_profile,
                content_hash,
                graph_status,
                actor_type,
                actor_id,
                actor_display_name,
                change_set_id,
                version_status,
                search_doc,
                created_by_actor_kind,
                created_by_actor_id,
                id,
                created_at
            ) VALUES (
                :page_id,
                NULL,
                1,
                '# Page Link Source',
                'markdown',
                'page-link-source-v1',
                'linear',
                'human',
                NULL,
                NULL,
                NULL,
                'draft',
                NULL,
                'human',
                'migration-test',
                :version_id,
                CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "page_id": page_id.hex,
            "version_id": version_id.hex,
        },
    )
    connection.execute(
        text(
            """
            UPDATE context_page
            SET current_version_id = :version_id,
                latest_draft_version_id = :version_id
            WHERE id = :page_id
            """
        ),
        {
            "page_id": page_id.hex,
            "version_id": version_id.hex,
        },
    )


def _schema_snapshot(engine) -> dict:
    """Return normalized SQLite schema facts for exact round-trip checks."""

    inspector = inspect(engine)

    def _options(value: dict | None) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((key, str(item)) for key, item in (value or {}).items()))

    def _sql(value) -> str | None:
        return None if value is None else " ".join(str(value).split())

    snapshot = {}
    for table_name in sorted(inspector.get_table_names()):
        primary_key = inspector.get_pk_constraint(table_name)
        snapshot[table_name] = {
            # Column order is not schema-significant and SQLite appends columns
            # restored by ``ALTER TABLE ADD COLUMN`` during downgrade.
            "columns": tuple(
                sorted(
                    (
                        column["name"],
                        str(column["type"]),
                        column.get("nullable"),
                        _sql(column.get("default")),
                        column.get("autoincrement"),
                        _sql(column.get("computed")),
                        _sql(column.get("identity")),
                    )
                    for column in inspector.get_columns(table_name)
                )
            ),
            "primary_key": (
                primary_key.get("name"),
                tuple(primary_key.get("constrained_columns") or ()),
            ),
            "foreign_keys": tuple(
                sorted(
                    [
                        (
                            foreign_key.get("name"),
                            tuple(foreign_key.get("constrained_columns") or ()),
                            foreign_key.get("referred_schema"),
                            foreign_key.get("referred_table"),
                            tuple(foreign_key.get("referred_columns") or ()),
                            _options(foreign_key.get("options")),
                        )
                        for foreign_key in inspector.get_foreign_keys(table_name)
                    ],
                    key=repr,
                )
            ),
            "indexes": tuple(
                sorted(
                    [
                        (
                            index.get("name"),
                            tuple(index.get("column_names") or ()),
                            bool(index.get("unique")),
                            _options(index.get("dialect_options")),
                        )
                        for index in inspector.get_indexes(table_name)
                    ],
                    key=repr,
                )
            ),
            "unique_constraints": tuple(
                sorted(
                    [
                        (
                            constraint.get("name"),
                            tuple(constraint.get("column_names") or ()),
                        )
                        for constraint in inspector.get_unique_constraints(table_name)
                    ],
                    key=repr,
                )
            ),
            "check_constraints": tuple(
                sorted(
                    [
                        (
                            constraint.get("name"),
                            _sql(constraint.get("sqltext")),
                        )
                        for constraint in inspector.get_check_constraints(table_name)
                    ],
                    key=repr,
                )
            ),
        }
    return snapshot


@pytest.fixture(scope="session")
def sqlite_templates(tmp_path_factory):
    return shared_templates(tmp_path_factory)


@pytest.fixture(scope="module")
def head_engine(sqlite_templates, tmp_path_factory):
    """One read-only migrated head schema for the schema-inspection tests."""
    database = tmp_path_factory.mktemp("initial-release-head") / "head.sqlite"
    engine = create_engine(sqlite_templates.clone("head", database))
    yield engine
    engine.dispose()


@contextmanager
def _captured_statements():
    statements: list[str] = []

    def capture(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", capture)


def _assert_epic1_additions_absent(inspector) -> None:
    """What 0001 has none of, checked at the baseline and after a downgrade."""
    tables = set(inspector.get_table_names())
    assert {"workspace_join_requests", "email_outbox"}.isdisjoint(tables)
    assert "workspace_visibility_check" not in {
        constraint["name"]
        for constraint in inspector.get_check_constraints("workspace")
    }
    assert "personal_workspace_id" not in {
        column["name"] for column in inspector.get_columns("users")
    }
    assert "join_policy" not in {
        column["name"] for column in inspector.get_columns("workspace")
    }


def _assert_each_table_rebuilt_at_most_once(statements: list[str]) -> None:
    counts = _batch_rebuild_counts(statements)
    assert counts
    assert {name: count for name, count in counts.items() if count > 1} == {}


def _batch_rebuild_counts(statements: list[str]) -> Counter[str]:
    pattern = re.compile(
        r'CREATE TABLE ["`]?_alembic_tmp_([A-Za-z0-9_]+)["`]?',
        re.IGNORECASE,
    )
    return Counter(
        match.group(1)
        for statement in statements
        if (match := pattern.search(statement)) is not None
    )


def test_release_head_matches_current_model_schema(head_engine):
    engine = head_engine
    with engine.connect() as connection:
        inspector = inspect(connection)
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
        table_names = set(inspector.get_table_names())
        context = MigrationContext.configure(
            connection, opts={"include_object": include_non_fts5_object}
        )
        diffs = compare_metadata(context, Base.metadata)
        consent_columns = {
            column["name"] for column in inspector.get_columns("oauth_consents")
        }
        refresh_indexes = {
            index["name"] for index in inspector.get_indexes("oauth_refresh_tokens")
        }
        semantic_overlay_uniques = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "library_semantic_overlay"
            )
        }
        version_columns = {
            column["name"] for column in inspector.get_columns("library_item_version")
        }

    assert version == GRAPH_REVISION
    assert set(Base.metadata.tables).issubset(table_names)
    assert diffs == []
    assert {"refresh_ttl_seconds", "sliding_enabled"}.issubset(consent_columns)
    assert "ix_oauth_refresh_tokens_user_client_expires_at" in refresh_indexes
    assert "selected_semantic_overlay_id" in version_columns
    assert "library_semantic_overlay_version_base_uq" in semantic_overlay_uniques


def test_tenancy_owns_the_library_item_version_multiformat_drop():
    """tenancy_schema must remain the one place these columns are removed.

    library_media_schema.downgrade() drops the same set behind a
    column-existence probe, because its own tests drive that module standalone
    with nothing else having run. In the orchestrated downgrade the probe is
    False and tenancy does the work -- but if an entry disappeared from
    tenancy's list the probe would flip True, silently compensate, and the
    round-trip schema comparison would still pass. This pins the list so the
    removal fails here instead of being absorbed there.
    """

    from backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema import (
        tenancy_schema,
    )

    assert tenancy_schema.LIBRARY_ITEM_VERSION_MULTIFORMAT_COLUMNS == (
        "selected_semantic_overlay_id",
        "selected_preview_attempt_id",
        "preview_status",
        "selected_conversion_attempt_id",
        "detection_json",
        "byte_size",
        "format_id",
        "detected_media_type",
        "declared_media_type",
        "original_filename",
        "source_blob_epoch",
        "source_blob_id",
        "source_blob_workspace_id",
        "sync_run_id",
    )


def test_core_leaves_no_migration_scaffolding(head_engine):
    """No `_dtxwiki_0002_*` object may survive into the finished schema.

    The revision publishes transient unique indexes so a reciprocal FK pair can
    be rebuilt, and retires them afterwards. Both retire calls are
    `if_exists=True`, so a scaffolding object that failed to be dropped would
    ship silently -- nothing else in the suite looks for one.
    """

    with head_engine.connect() as connection:
        surviving = connection.execute(
            text(
                "SELECT type, name FROM sqlite_master "
                "WHERE name LIKE '\\_dtxwiki\\_0002\\_%' ESCAPE '\\'"
            )
        ).fetchall()

    assert surviving == [], (
        "migration scaffolding survived into the finished schema: "
        f"{[(row[0], row[1]) for row in surviving]}"
    )


def test_core_revision_rolls_back_late_failure_and_retries(tmp_path, sqlite_templates):
    database_url = sqlite_templates.clone("base_0001", tmp_path / "retry.db")
    config = _config(database_url)
    engine = create_engine(database_url)
    workspace_a = uuid4()
    workspace_b = uuid4()
    node_a = uuid4()
    node_b = uuid4()
    with engine.begin() as connection:
        _insert_initial_release_workspace(
            connection,
            workspace_id=workspace_a,
            slug="workspace-a",
        )
        _insert_initial_release_workspace(
            connection,
            workspace_id=workspace_b,
            slug="workspace-b",
        )
        _insert_initial_release_tree_node(
            connection,
            node_id=node_b,
            workspace_id=workspace_b,
            slug="b-root",
            sort_key="a",
        )
        _insert_initial_release_tree_node(
            connection,
            node_id=node_a,
            workspace_id=workspace_a,
            slug="a-child",
            sort_key="b",
            parent_id=node_b,
        )
    before_snapshot = _schema_snapshot(engine)

    with pytest.raises(
        RuntimeError,
        match=r"workspace_tree_node\.\(parent_id\).*cross-workspace",
    ):
        command.upgrade(config, HEAD_REVISION)

    assert _schema_snapshot(engine) == before_snapshot
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            INITIAL_REVISION
        )

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workspace_tree_node SET parent_id = NULL WHERE id = :node_id"),
            {"node_id": node_a.hex},
        )
    command.upgrade(config, HEAD_REVISION)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            HEAD_REVISION
        )
    engine.dispose()


def test_core_revision_round_trips_from_initial_release(tmp_path, sqlite_templates):
    database_url = sqlite_templates.clone("base_0001", tmp_path / "round-trip.db")
    config = _config(database_url)
    engine = create_engine(database_url)
    try:
        before_snapshot = _schema_snapshot(engine)
        before = inspect(engine)
        assert "workspace_job_hint" not in before.get_table_names()
        assert {
            "media_blob_reference",
            "media_blob_gc_hold",
            "media_blob_gc_candidate",
        }.isdisjoint(before.get_table_names())
        assert "workspace_id" not in {
            column["name"] for column in before.get_columns("library_item_version")
        }
        assert {"block_map", "reconcile_manifest"}.isdisjoint(
            column["name"] for column in before.get_columns("context_page_version")
        )
        assert "search_doc" in {
            column["name"] for column in before.get_columns("context_page_version")
        }
        assert {"search_doc_fts", "page_name_trgm"}.isdisjoint(before.get_table_names())
        assert "email_verified_at" not in {
            column["name"] for column in before.get_columns("users")
        }
        assert "password_reset_tokens" not in before.get_table_names()
        _assert_epic1_additions_absent(before)
        # The original 0001 audit FK carried no ondelete action at all.
        assert {
            tuple(fk["constrained_columns"]): fk
            for fk in before.get_foreign_keys("audit_event")
        }[("workspace_id",)]["options"].get("ondelete") is None

        with _captured_statements() as statements:
            command.upgrade(config, HEAD_REVISION)
        _assert_each_table_rebuilt_at_most_once(statements)
        head_snapshot = _schema_snapshot(engine)
        after = inspect(engine)
        assert "workspace_job_hint" in after.get_table_names()
        assert {
            "media_blob_reference",
            "media_blob_gc_hold",
            "media_blob_gc_candidate",
        }.issubset(after.get_table_names())
        assert "workspace_id" in {
            column["name"] for column in after.get_columns("library_item_version")
        }
        assert {"block_map", "reconcile_manifest"}.issubset(
            column["name"] for column in after.get_columns("context_page_version")
        )
        assert {
            "last_read_version_id",
            "last_read_root_hash",
            "last_read_at",
        }.issubset(
            column["name"] for column in after.get_columns("mcp_chat_artifact_link")
        )
        # A-1: manual_retry_
        # disposition/disposition_reason now live on the 1:1
        # library_attempt_conversion_detail extension table, not directly
        # on the unified library_attempt.
        assert {"manual_retry_disposition", "disposition_reason"}.issubset(
            column["name"]
            for column in after.get_columns("library_attempt_conversion_detail")
        )
        mcp_read_fks = {
            foreign_key["name"]
            for foreign_key in after.get_foreign_keys("mcp_chat_artifact_link")
        }
        assert (
            "fk_mcp_chat_artifact_link_last_read_version_id_workspace_scoped"
            in mcp_read_fks
        )
        retry_checks = {
            check["name"]
            for check in after.get_check_constraints(
                "library_attempt_conversion_detail"
            )
        }
        assert {
            "library_attempt_conv_detail_manual_retry_disposition_check",
            "library_attempt_conversion_detail_retry_disposition_pair_check",
        }.issubset(retry_checks)
        email_kind_check = next(
            check["sqltext"]
            for check in after.get_check_constraints("email_outbox")
            if check["name"] == "email_outbox_message_kind_check"
        )
        assert "reconciliation_reminder" in email_kind_check

        with _captured_statements() as statements:
            command.downgrade(config, INITIAL_REVISION)
        _assert_each_table_rebuilt_at_most_once(statements)
        assert _schema_snapshot(engine) == before_snapshot
        downgraded = inspect(engine)
        assert "search_doc" in {
            column["name"] for column in downgraded.get_columns("context_page_version")
        }
        assert {
            "search_doc_fts",
            "page_name_trgm",
            "knowledge_coverage_gap",
        }.isdisjoint(downgraded.get_table_names())
        assert "edge_projection_version" not in {
            column["name"] for column in downgraded.get_columns("workspace")
        }
        assert {
            "retrieval_trace",
            "tried_lanes",
            "fallback_disclosure",
        }.isdisjoint({column["name"] for column in downgraded.get_columns("ask_turn")})
        assert "email_verified_at" not in {
            column["name"] for column in downgraded.get_columns("users")
        }
        assert "password_reset_tokens" not in downgraded.get_table_names()
        _assert_epic1_additions_absent(downgraded)
        assert {
            tuple(fk["constrained_columns"]): fk
            for fk in downgraded.get_foreign_keys("audit_event")
        }[("workspace_id",)]["options"].get("ondelete") is None

        command.upgrade(config, HEAD_REVISION)
        assert _schema_snapshot(engine) == head_snapshot
        with engine.connect() as connection:
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        assert revision == HEAD_REVISION
    finally:
        engine.dispose()


def test_page_link_syntax_downgrade_preserves_wikilink_projection(tmp_path):
    config, engine = _upgrade_head(tmp_path, "page-link-syntax-downgrade.db")
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory.begin() as session:
            workspace = seed_default_workspace(session)
            source = ContextPage(
                workspace_id=workspace.id,
                slug="source",
                title="Source",
                page_type="concept",
                tags=[],
                aliases=[],
                lifecycle_status="published",
            )
            target = ContextPage(
                workspace_id=workspace.id,
                slug="target",
                title="Target",
                page_type="concept",
                tags=[],
                aliases=[],
                lifecycle_status="published",
            )
            session.add_all((source, target))
            session.flush()
            source_version = ContextPageVersion(
                workspace_id=workspace.id,
                page_id=source.id,
                version_no=1,
                body_md="# Source",
                content_hash="source-v1",
                actor_type="human",
                version_status="published_snapshot",
                created_by_actor_kind="human",
                created_by_actor_id="migration-test",
            )
            target_version = ContextPageVersion(
                workspace_id=workspace.id,
                page_id=target.id,
                version_no=1,
                body_md="# Target",
                content_hash="target-v1",
                actor_type="human",
                version_status="published_snapshot",
                created_by_actor_kind="human",
                created_by_actor_id="migration-test",
            )
            session.add_all((source_version, target_version))
            session.flush()
            source.current_version_id = source_version.id
            source.current_published_version_id = source_version.id
            target.current_version_id = target_version.id
            target.current_published_version_id = target_version.id
            wikilink_created_at = datetime(2026, 9, 20, 1, tzinfo=UTC)
            context_url_created_at = datetime(2026, 9, 20, 2, tzinfo=UTC)
            session.add_all(
                (
                    PageLink(
                        workspace_id=workspace.id,
                        source_page_id=source.id,
                        source_version_id=source_version.id,
                        link_syntax="wikilink",
                        target_slug=target.slug,
                        target_page_id=target.id,
                        occurrence_count=1,
                        first_snippet="keep",
                        created_at=wikilink_created_at,
                    ),
                    PageLink(
                        workspace_id=workspace.id,
                        source_page_id=source.id,
                        source_version_id=source_version.id,
                        link_syntax="context_url",
                        target_slug=target.slug,
                        target_page_id=target.id,
                        occurrence_count=1,
                        first_snippet="drop",
                        created_at=context_url_created_at,
                    ),
                )
            )

        engine.dispose()
        command.downgrade(config, INITIAL_REVISION)
        engine = create_engine(config.get_main_option("sqlalchemy.url"))
        with engine.connect() as connection:
            inspector = inspect(connection)
            columns = {column["name"] for column in inspector.get_columns("page_links")}
            unique_columns = {
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints("page_links")
            }
            snippets = connection.scalars(
                text("SELECT first_snippet FROM page_links ORDER BY first_snippet")
            ).all()

        assert "link_syntax" not in columns
        assert ("source_page_id", "target_slug") in unique_columns
        assert snippets == ["keep"]
    finally:
        engine.dispose()


def test_page_link_syntax_upgrade_backfills_and_enforces_server_contract(tmp_path):
    database_url = _database_url(tmp_path, "page-link-syntax-upgrade.db")
    config = _config(database_url)
    command.upgrade(config, INITIAL_REVISION)

    workspace_id = uuid4()
    source_page_id = uuid4()
    source_version_id = uuid4()
    upgraded_link_id = uuid4()
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            _insert_initial_release_workspace(
                connection,
                workspace_id=workspace_id,
                slug="workspace-page-link-syntax",
            )
            _insert_initial_release_page_link_source(
                connection,
                workspace_id=workspace_id,
                page_id=source_page_id,
                version_id=source_version_id,
            )
            connection.execute(
                text(
                    """
                    INSERT INTO page_links (
                        workspace_id,
                        source_page_id,
                        source_version_id,
                        target_slug,
                        target_page_id,
                        occurrence_count,
                        first_snippet,
                        id,
                        created_at
                    ) VALUES (
                        :workspace_id,
                        :source_page_id,
                        :source_version_id,
                        'existing-target',
                        NULL,
                        1,
                        'pre-upgrade',
                        :link_id,
                        :created_at
                    )
                    """
                ),
                {
                    "created_at": datetime(2026, 9, 19, tzinfo=UTC),
                    "link_id": upgraded_link_id.hex,
                    "source_page_id": source_page_id.hex,
                    "source_version_id": source_version_id.hex,
                    "workspace_id": workspace_id.hex,
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, GRAPH_REVISION)
    engine = create_engine(database_url)
    defaulted_link_id = uuid4()
    try:
        with engine.begin() as connection:
            assert (
                connection.scalar(
                    text("SELECT link_syntax FROM page_links WHERE id = :link_id"),
                    {"link_id": upgraded_link_id.hex},
                )
                == "wikilink"
            )
            connection.execute(
                text(
                    """
                    INSERT INTO page_links (
                        workspace_id,
                        source_page_id,
                        source_version_id,
                        target_slug,
                        target_page_id,
                        occurrence_count,
                        first_snippet,
                        id,
                        created_at
                    ) VALUES (
                        :workspace_id,
                        :source_page_id,
                        :source_version_id,
                        'defaulted-target',
                        NULL,
                        1,
                        'post-upgrade default',
                        :link_id,
                        :created_at
                    )
                    """
                ),
                {
                    "created_at": datetime(2026, 9, 20, tzinfo=UTC),
                    "link_id": defaulted_link_id.hex,
                    "source_page_id": source_page_id.hex,
                    "source_version_id": source_version_id.hex,
                    "workspace_id": workspace_id.hex,
                },
            )
            assert (
                connection.scalar(
                    text("SELECT link_syntax FROM page_links WHERE id = :link_id"),
                    {"link_id": defaulted_link_id.hex},
                )
                == "wikilink"
            )

        with pytest.raises(IntegrityError, match="page_links_link_syntax_check"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO page_links (
                            workspace_id,
                            source_page_id,
                            source_version_id,
                            link_syntax,
                            target_slug,
                            target_page_id,
                            occurrence_count,
                            first_snippet,
                            id,
                            created_at
                        ) VALUES (
                            :workspace_id,
                            :source_page_id,
                            :source_version_id,
                            'markdown_link',
                            'invalid-target',
                            NULL,
                            1,
                            'invalid syntax',
                            :link_id,
                            :created_at
                        )
                        """
                    ),
                    {
                        "created_at": datetime(2026, 9, 21, tzinfo=UTC),
                        "link_id": uuid4().hex,
                        "source_page_id": source_page_id.hex,
                        "source_version_id": source_version_id.hex,
                        "workspace_id": workspace_id.hex,
                    },
                )
    finally:
        engine.dispose()


def test_initial_release_downgrade_returns_to_base(tmp_path, sqlite_templates):
    database_url = sqlite_templates.clone("head", tmp_path / "downgrade.db")
    command.downgrade(_config(database_url), "base")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())

        assert tables == {"alembic_version"}
    finally:
        engine.dispose()


def test_first_install_rolls_back_both_revisions_on_late_failure(tmp_path):
    """Exercise the actual two-revision chain, including the baseline DDL."""

    scripts = tmp_path / "migrations"
    shutil.copytree(
        resolve_migrations_dir(), scripts, ignore=shutil.ignore_patterns("__pycache__")
    )
    core = scripts / "versions/v0_1/efcbcf724889_core_schema.py"
    core.write_text(
        core.read_text()
        + "\n_original_upgrade = _upgrade_epic1\n"
        + "def _upgrade_epic1():\n"
        + "    _original_upgrade()\n"
        + '    raise RuntimeError("late first-install failure")\n'
    )
    url = f"sqlite+pysqlite:///{tmp_path / 'fresh.sqlite'}"
    config = _config(url)
    config.set_main_option("script_location", str(scripts))
    with pytest.raises(RuntimeError, match="late first-install failure"):
        command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_initial_release_preserves_key_foreign_key_actions(head_engine):
    engine = head_engine
    with engine.connect() as connection:
        inspector = inspect(connection)

        context_fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspector.get_foreign_keys("context_page")
        }
        library_candidate_fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspector.get_foreign_keys("library_discovery_candidate")
        }
        mcp_chat_fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspector.get_foreign_keys("mcp_chat_session")
        }
        workspace_membership_fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspector.get_foreign_keys("workspace_memberships")
        }

    # The final context_page table plan converts both to composite FKs
    # (workspace_id, <col>) -> context_page_version (workspace_id, id),
    # enforcing workspace scope. ondelete is deliberately NOT preserved as
    # SET NULL here: SQLite (and standard Postgres FK semantics) null out
    # EVERY column of a composite FK on cascade, including workspace_id --
    # which is NOT NULL -- so a real DELETE of the referenced version
    # always failed (empirically confirmed: "NOT NULL constraint failed:
    # context_page.workspace_id", not a clean FK error). Every real caller
    # already clears the pointer explicitly before deleting the version
    # row (see e.g. workspace_write.py's _delete_seed_workspace,
    # context_lifecycle.py, review_service.py), so the DB-level SET NULL
    # was dead weight; omitting ondelete makes SQLite's default (RESTRICT)
    # the actual, honest contract.
    assert (
        context_fks[("workspace_id", "current_published_version_id")]["options"].get(
            "ondelete"
        )
        is None
    )
    assert (
        context_fks[("workspace_id", "latest_draft_version_id")]["options"].get(
            "ondelete"
        )
        is None
    )
    # The final library_discovery_candidate table plan converts this to a
    # composite FK (workspace_id, accepted_library_item_id) -> library_item
    # (workspace_id, id), preserving ondelete and workspace scope.
    assert (
        library_candidate_fks[("workspace_id", "accepted_library_item_id")][
            "options"
        ].get("ondelete")
        == "CASCADE"
    )
    assert mcp_chat_fks[("workspace_id",)]["options"].get("ondelete") == ("CASCADE")
    assert mcp_chat_fks[("owner_user_id",)]["options"].get("ondelete") == ("SET NULL")
    assert (
        workspace_membership_fks[("user_id",)]["options"].get("ondelete") == "CASCADE"
    )
    assert (
        workspace_membership_fks[("workspace_id",)]["options"].get("ondelete")
        == "CASCADE"
    )


def test_audit_event_workspace_fk_ondelete_is_set_null(head_engine):
    # WS-02: an ordinary admin action (member add/remove, access change)
    # emits an audit_event row a user has no way to clear, so treating it as
    # a hard-delete blocker made workspace deletion permanently unreachable
    # after the first such action. ``bd62193d5cf6`` created
    # ``fk_audit_event_workspace_id_workspace`` with no ON DELETE action;
    # this revision recreates it as ``ON DELETE SET NULL`` to match the ORM
    # model (backend/models/audit.py). A fixture-based test that only runs
    # ``Base.metadata.create_all()`` cannot catch a missing DDL step here --
    # create_all() always reflects the current model, migration or not --
    # which is exactly how the earlier missing migration went unnoticed;
    # this inspects the actually migrated schema instead.
    with head_engine.connect() as connection:
        audit_fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(connection).get_foreign_keys("audit_event")
        }
    assert audit_fks[("workspace_id",)]["options"].get("ondelete") == "SET NULL"


def _observation(workspace_id, *, sequence: int, key: str) -> GraphObservation:
    return GraphObservation(
        workspace_id=workspace_id,
        operation="node_created",
        workspace_sequence=sequence,
        source_kind="context_page",
        source_id=uuid4(),
        occurred_at=datetime.now(UTC),
        idempotency_key=key,
    )


def _ledger_rows(session, workspace_id) -> int:
    return session.scalar(
        text(
            "SELECT COUNT(*) FROM graph_observation WHERE workspace_id = :workspace_id"
        ),
        {"workspace_id": workspace_id.hex},
    )


def test_graph_observation_migration_enforces_append_only_at_database_boundary(
    tmp_path,
):
    _, engine = _upgrade_head(tmp_path, "graph-observation-guards.db")
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory.begin() as session:
            workspace = seed_default_workspace(session)
            observation = _observation(workspace.id, sequence=1, key="guard-1")
            session.add(observation)
            session.flush()
            observation_id = observation.id

        with engine.begin() as connection:
            with pytest.raises(DBAPIError, match="append-only"):
                connection.execute(
                    text(
                        "UPDATE graph_observation SET action_kind = 'edited' "
                        "WHERE id = :observation_id"
                    ),
                    {"observation_id": observation_id.hex},
                )

        with engine.begin() as connection:
            with pytest.raises(DBAPIError, match="append-only"):
                connection.execute(
                    text("DELETE FROM graph_observation WHERE id = :observation_id"),
                    {"observation_id": observation_id.hex},
                )
    finally:
        engine.dispose()


def test_graph_observation_guard_allows_workspace_cascade_delete(tmp_path):
    _, engine = _upgrade_head(tmp_path, "graph-observation-cascade.db")
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys = ON"))
            with session_factory(bind=connection) as session:
                workspace = seed_default_workspace(session)
                workspace_id = workspace.id
                session.add(_observation(workspace_id, sequence=1, key="cascade-1"))
                session.flush()

                # The ledger is append-only while its parent exists, but the
                # real workspace lifecycle path must still be able to invoke
                # the declared ON DELETE CASCADE.
                media_intent = WorkspaceWriteService._begin_media_delete(
                    session=session,
                    workspace_id=workspace_id,
                )
                WorkspaceWriteService._delete_seed_workspace(
                    session,
                    workspace_id,
                    media_intent=media_intent,
                )
                assert _ledger_rows(session, workspace_id) == 0
    finally:
        engine.dispose()


def test_graph_observation_teardown_empties_ledger_without_fk_enforcement(
    tmp_path,
):
    """The explicit teardown delete passes the trigger once the parent is gone.

    Without ``PRAGMA foreign_keys`` the cascade never fires, which is the shape
    every plain ``sessionmaker`` fixture in this suite runs under. The teardown
    therefore deletes the ledger itself, after the workspace row, and both the
    database trigger and the ORM guard must admit exactly that statement.
    """

    _, engine = _upgrade_head(tmp_path, "graph-observation-teardown.db")
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory.begin() as session:
            workspace = seed_default_workspace(session)
            workspace_id = workspace.id
            session.add(_observation(workspace_id, sequence=1, key="teardown-1"))
            session.flush()
            media_intent = WorkspaceWriteService._begin_media_delete(
                session=session,
                workspace_id=workspace_id,
            )
            WorkspaceWriteService._delete_seed_workspace(
                session,
                workspace_id,
                media_intent=media_intent,
            )
            assert _ledger_rows(session, workspace_id) == 0
    finally:
        engine.dispose()


def test_workspace_purge_then_teardown_clears_graph_ledger_tables(tmp_path):
    """The production delete order against the migrated schema and triggers.

    ``purge_workspace_data`` must not touch ``graph_observation`` (the trigger
    would abort the whole purge while the workspace row exists) while still
    deleting ``graph_history_coverage`` like any other scoped table; the
    terminal teardown then empties the ledger.
    """

    _, engine = _upgrade_head(tmp_path, "graph-purge-order.db")
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory.begin() as session:
            workspace = seed_default_workspace(session)
            workspace_id = workspace.id
            session.add(_observation(workspace_id, sequence=1, key="purge-1"))
            session.add(
                GraphHistoryCoverage(
                    workspace_id=workspace_id,
                    stream_kind="context_page",
                    stream_id=uuid4(),
                    basis="observations",
                    status="partial",
                    extractor_version="test",
                )
            )
            session.flush()

            deleted = purge_workspace_data(session, workspace_id)
            assert deleted["graph_history_coverage"] == 1
            assert "graph_observation" not in deleted
            assert _ledger_rows(session, workspace_id) == 1
            assert (
                session.scalar(
                    select(GraphHistoryCoverage).where(
                        GraphHistoryCoverage.workspace_id == workspace_id
                    )
                )
                is None
            )

            media_intent = WorkspaceWriteService._begin_media_delete(
                session=session,
                workspace_id=workspace_id,
            )
            WorkspaceWriteService._delete_seed_workspace(
                session,
                workspace_id,
                media_intent=media_intent,
            )
            assert _ledger_rows(session, workspace_id) == 0
    finally:
        engine.dispose()
