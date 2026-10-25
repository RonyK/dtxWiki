from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.migrations.revision_modules.v0_1.content_event_backfill_v1 import (
    backfill_content_event_times,
)
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.audit import AuditEvent
from backend.models.context import ContextPage, ContextPageVersion
from backend.scripts.seed_dev import seed_default_workspace
from backend.tests.integration.test_release_schema_migration import _config
from backend.tests.utils.migrated_sqlite_templates import shared_templates

FIRST_EVENT = datetime(2026, 1, 3, 4, tzinfo=UTC)
SECOND_EVENT = FIRST_EVENT + timedelta(days=12)


@pytest.mark.parametrize("hard_break", [False, True])
def test_upgrade_from_previous_head_backfills_only_proven_content_events(
    tmp_path, tmp_path_factory, hard_break
):
    database = tmp_path / "tree-metadata.sqlite"
    url = shared_templates(tmp_path_factory).clone("head", database)
    engine = create_engine(url)
    ids = {}
    private_version_ids = []
    with Session(engine) as session, session.begin():
        workspace = seed_default_workspace(session)
        for kind in ("context_page", "agent_page"):
            if kind == "context_page":
                page = ContextPage(
                    workspace_id=workspace.id,
                    title="Renamed",
                    slug="legacy-context",
                    lifecycle_status="published",
                )
            else:
                page = AgentPage(
                    workspace_id=workspace.id,
                    title="Renamed",
                    slug="legacy-agent",
                    category="skill",
                    lifecycle_status="active",
                )
            session.add(page)
            session.flush()
            if kind == "context_page":
                old = ContextPageVersion(
                    workspace_id=workspace.id,
                    page_id=page.id,
                    version_no=1,
                    body_md="---\ntitle: Original\n---\nLine one\nLine two",
                    content_hash="one",
                    actor_type="human",
                    created_by_actor_kind="human",
                    created_by_actor_id="test",
                    version_status="superseded_snapshot",
                )
                latest = ContextPageVersion(
                    workspace_id=workspace.id,
                    page_id=page.id,
                    version_no=2,
                    body_md="---\ntitle: Renamed\n---\nLine one"
                    + ("  " if hard_break else "")
                    + "\nLine two",
                    content_hash="two",
                    actor_type="human",
                    created_by_actor_kind="human",
                    created_by_actor_id="test",
                    version_status="published_snapshot",
                )
            else:
                old = AgentPageVersion(
                    workspace_id=workspace.id,
                    agent_page_id=page.id,
                    version_no=1,
                    body_markdown="Line one\nLine two",
                    frontmatter={"title": "Original"},
                    actor_type="human",
                    version_status="superseded_snapshot",
                )
                latest = AgentPageVersion(
                    workspace_id=workspace.id,
                    agent_page_id=page.id,
                    version_no=2,
                    body_markdown="Line one"
                    + ("  " if hard_break else "")
                    + "\nLine two",
                    frontmatter={"title": "Renamed"},
                    actor_type="human",
                    version_status="active_snapshot",
                )
            session.add_all([old, latest])
            session.flush()
            if kind == "context_page":
                private = ContextPageVersion(
                    workspace_id=workspace.id,
                    page_id=page.id,
                    version_no=3,
                    body_md="Private unreachable edit. " * 4096,
                    content_hash="private",
                    actor_type="human",
                    created_by_actor_kind="human",
                    created_by_actor_id="private-author",
                    version_status="editing",
                )
            else:
                private = AgentPageVersion(
                    workspace_id=workspace.id,
                    agent_page_id=page.id,
                    version_no=3,
                    body_markdown="Private unreachable draft. " * 4096,
                    actor_type="human",
                    version_status="draft",
                )
            session.add(private)
            session.flush()
            private_version_ids.append(private.id.hex)
            if kind == "context_page":
                page.current_version_id = page.current_published_version_id = latest.id
                previous_key, action = (
                    "previous_published_version_id",
                    "context_page.draft.publish",
                )
            else:
                page.current_active_version_id = latest.id
                previous_key, action = (
                    "previous_active_version_id",
                    "agent_page.activated",
                )
            ids[kind] = page.id
            for version, previous, timestamp in (
                (old, None, FIRST_EVENT),
                (latest, old.id, SECOND_EVENT),
            ):
                session.add(
                    AuditEvent(
                        workspace_id=workspace.id,
                        actor_kind="human",
                        actor_id="test",
                        action=action,
                        target_kind=kind,
                        target_id=page.id,
                        created_at=timestamp,
                        payload={
                            "version_id": str(version.id),
                            previous_key: str(previous) if previous else None,
                        },
                    )
                )
        unknown = ContextPage(
            workspace_id=workspace.id,
            title="No publication proof",
            slug="legacy-unknown",
            lifecycle_status="draft",
        )
        session.add(unknown)
        session.flush()
        ids["unknown"] = unknown.id
    engine.dispose()
    config = _config(url)
    # The actual previous release schema is the upgrade's starting point;
    # ORM/model construction above is only a convenient legacy data fixture.
    command.downgrade(config, "efcbcf724889")
    history_reads = []
    version_read_parameters = []
    audit_plans = []

    def record(_connection, _cursor, statement, parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select ") and any(
            f"from {table}" in normalized
            for table in ("context_page_version", "agent_page_version", "audit_event")
        ):
            history_reads.append(normalized)
            if "from audit_event" in normalized:
                audit_plans.append(
                    _connection.exec_driver_sql(
                        "EXPLAIN QUERY PLAN " + statement, parameters
                    ).all()
                )
            else:
                version_read_parameters.append(parameters)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        command.upgrade(config, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    assert history_reads
    assert all(
        " where " in statement and " limit " in statement for statement in history_reads
    ), history_reads
    assert audit_plans and all(
        "ix_tree_rail_backfill_audit_target" in str(plan) and "<expr>=?" in str(plan)
        for plan in audit_plans
    ), audit_plans
    assert all(
        private_id not in str(version_read_parameters)
        for private_id in private_version_ids
    ), version_read_parameters
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            assert session.get(ContextPage, ids["context_page"]).content_changed_at == (
                SECOND_EVENT if hard_break else FIRST_EVENT
            )
            assert session.get(AgentPage, ids["agent_page"]).content_changed_at == (
                SECOND_EVENT if hard_break else FIRST_EVENT
            )
            assert session.get(ContextPage, ids["unknown"]).content_changed_at is None
        inspector = inspect(engine)
        assert "ix_tree_rail_backfill_audit_target" not in {
            index["name"] for index in inspector.get_indexes("audit_event")
        }
        assert "user_favorite" in inspector.get_table_names()
        assert set(
            inspector.get_pk_constraint("user_favorite")["constrained_columns"]
        ) == {"workspace_id", "user_id", "target_kind", "target_id"}
    finally:
        engine.dispose()


def _minimal_backfill_tables():
    """Only final columns the data backfill needs, with real UUID/JSON types."""
    metadata = sa.MetaData()
    pages = sa.Table(
        "context_page",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid()),
        sa.Column("current_published_version_id", sa.Uuid()),
        sa.Column("content_changed_at", sa.DateTime(timezone=True)),
    )
    versions = sa.Table(
        "context_page_version",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid()),
        sa.Column("page_id", sa.Uuid()),
        sa.Column("body_md", sa.Text()),
        sa.Column("rendering_profile", sa.Text()),
    )
    sa.Table("library_item", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        "agent_page",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid()),
        sa.Column("current_active_version_id", sa.Uuid()),
        sa.Column("content_changed_at", sa.DateTime(timezone=True)),
    )
    sa.Table(
        "agent_page_version", metadata, sa.Column("id", sa.Uuid(), primary_key=True)
    )
    audits = sa.Table(
        "audit_event",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid()),
        sa.Column("target_kind", sa.Text()),
        sa.Column("target_id", sa.Uuid()),
        sa.Column("action", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("payload", sa.JSON()),
    )
    return metadata, pages, versions, audits


def _seed_unchanged_chain(connection, pages, versions, audits, count):
    page_id, workspace_id = UUID(int=10**12), UUID(int=10**13)
    version_ids = [UUID(int=number + 1) for number in range(count)]
    connection.execute(
        pages.insert(),
        {
            "id": page_id,
            "workspace_id": workspace_id,
            "current_published_version_id": version_ids[-1],
        },
    )
    connection.execute(
        versions.insert(),
        [
            {
                "id": version_id,
                "workspace_id": workspace_id,
                "page_id": page_id,
                "body_md": "Unchanged visible content.",
                "rendering_profile": "gfm_mermaid",
            }
            for version_id in version_ids
        ],
    )
    connection.execute(
        audits.insert(),
        [
            {
                "id": version_id,
                "workspace_id": workspace_id,
                "target_kind": "context_page",
                "target_id": page_id,
                "action": "context_page.draft.publish",
                "created_at": FIRST_EVENT + timedelta(seconds=number),
                "payload": {
                    # Both accepted UUID spellings must be seekable.
                    "version_id": str(version_id) if number % 2 else version_id.hex,
                    "previous_published_version_id": (
                        str(version_ids[number - 1]) if number else None
                    ),
                },
            }
            for number, version_id in enumerate(version_ids)
        ],
    )
    return page_id, workspace_id, version_ids


def test_upgrade_publication_chain_work_grows_linearly():
    measurements = []
    for count in (256, 1024):
        engine = create_engine("sqlite:///:memory:")
        metadata, pages, versions, audits = _minimal_backfill_tables()
        metadata.create_all(engine)
        try:
            with engine.begin() as connection:
                _seed_unchanged_chain(connection, pages, versions, audits, count)
                ticks = [0]

                def progress(ticks=ticks):
                    ticks[0] += 1

                raw = connection.connection.driver_connection
                raw.set_progress_handler(progress, 100)
                try:
                    # The release's data helper must create the lookup index
                    # used by the actual lookup, then remove it afterwards.
                    backfill_content_event_times(connection)
                finally:
                    raw.set_progress_handler(None, 0)
                measurements.append(ticks[0] * 100)
                assert (
                    connection.exec_driver_sql(
                        "SELECT content_changed_at FROM context_page"
                    ).scalar_one()
                    == "2026-01-03 04:00:00.000000"
                )
                assert "ix_tree_rail_backfill_audit_target" not in {
                    index["name"]
                    for index in inspect(connection).get_indexes("audit_event")
                }
        finally:
            engine.dispose()
    # Four times the valid predecessor chain may do modest index-building
    # overhead, but must not repeat the quadratic audit scan (~16x work).
    assert 0 < measurements[0] and measurements[1] < measurements[0] * 6, measurements


@pytest.mark.parametrize(
    "broken_proof",
    [
        "missing_previous",
        "invalid_previous",
        "missing_version",
        "version_workspace",
        "version_target",
        "audit_workspace",
        "audit_target",
        "audit_kind",
        "audit_action",
        "cycle",
    ],
)
def test_upgrade_keeps_unproven_publication_times_unknown(broken_proof):
    engine = create_engine("sqlite:///:memory:")
    metadata, pages, versions, audits = _minimal_backfill_tables()
    metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            _, _, version_ids = _seed_unchanged_chain(
                connection, pages, versions, audits, 2
            )
            previous_key = "previous_published_version_id"
            if broken_proof in {"missing_previous", "invalid_previous", "cycle"}:
                payload = {"version_id": str(version_ids[-1])}
                if broken_proof == "invalid_previous":
                    payload[previous_key] = "invalid-uuid"
                elif broken_proof == "cycle":
                    payload[previous_key] = str(version_ids[-1])
                connection.execute(
                    audits.update()
                    .where(audits.c.id == version_ids[-1])
                    .values(payload=payload)
                )
            elif broken_proof == "missing_version":
                connection.execute(
                    versions.delete().where(versions.c.id == version_ids[0])
                )
            elif broken_proof.startswith("version_"):
                column = (
                    "workspace_id" if broken_proof.endswith("workspace") else "page_id"
                )
                connection.execute(
                    versions.update()
                    .where(versions.c.id == version_ids[0])
                    .values({column: UUID(int=999)})
                )
            else:
                column, value = {
                    "audit_workspace": ("workspace_id", UUID(int=999)),
                    "audit_target": ("target_id", UUID(int=999)),
                    "audit_kind": ("target_kind", "agent_page"),
                    "audit_action": ("action", "context_page.draft.save"),
                }[broken_proof]
                connection.execute(
                    audits.update()
                    .where(audits.c.id == version_ids[-1])
                    .values({column: value})
                )
            backfill_content_event_times(connection)
            assert (
                connection.exec_driver_sql(
                    "SELECT content_changed_at FROM context_page"
                ).scalar_one()
                is None
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("same_timestamp", [False, True])
def test_upgrade_uses_newest_exact_version_event_across_uuid_spellings(same_timestamp):
    engine = create_engine("sqlite:///:memory:")
    metadata, pages, versions, audits = _minimal_backfill_tables()
    metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            page_id, workspace_id, version_ids = _seed_unchanged_chain(
                connection, pages, versions, audits, 2
            )
            timestamp = FIRST_EVENT + timedelta(seconds=1 if same_timestamp else 2)
            connection.execute(
                audits.insert(),
                {
                    "id": UUID(int=999),
                    "workspace_id": workspace_id,
                    "target_kind": "context_page",
                    "target_id": page_id,
                    "action": "context_page.draft.publish",
                    "created_at": timestamp,
                    "payload": {
                        "version_id": version_ids[-1].hex,
                        "previous_published_version_id": None,
                    },
                },
            )
            # A malformed, arbitrarily large key cannot match either UUID
            # form and must remain safe to index on PostgreSQL as well.
            connection.execute(
                audits.insert(),
                {
                    "id": UUID(int=1000),
                    "workspace_id": workspace_id,
                    "target_kind": "context_page",
                    "target_id": page_id,
                    "action": "context_page.draft.publish",
                    "created_at": SECOND_EVENT,
                    "payload": {"version_id": str(version_ids[-1]) + "x" * 8192},
                },
            )
            backfill_content_event_times(connection)
            assert connection.exec_driver_sql(
                "SELECT content_changed_at FROM context_page"
            ).scalar_one() == timestamp.replace(tzinfo=None).isoformat(
                sep=" ", timespec="microseconds"
            )
    finally:
        engine.dispose()


def test_backfill_completes_beyond_one_page_batch(tmp_path, tmp_path_factory):
    database = tmp_path / "tree-metadata-multiple-batches.sqlite"
    url = shared_templates(tmp_path_factory).clone("head", database)
    engine = create_engine(url)
    with Session(engine) as session, session.begin():
        workspace = seed_default_workspace(session)
        pages = [
            AgentPage(
                workspace_id=workspace.id,
                title=f"Page {number}",
                slug=f"page-{number}",
                category="skill",
                lifecycle_status="active",
            )
            for number in range(130)
        ]
        session.add_all(pages)
        session.flush()
        versions = [
            AgentPageVersion(
                workspace_id=workspace.id,
                agent_page_id=page.id,
                version_no=1,
                body_markdown="Published body.",
                actor_type="human",
                version_status="active_snapshot",
            )
            for page in pages
        ]
        session.add_all(versions)
        session.flush()
        for page, version in zip(pages, versions, strict=True):
            page.current_active_version_id = version.id
            session.add(
                AuditEvent(
                    workspace_id=workspace.id,
                    actor_kind="human",
                    actor_id="test",
                    action="agent_page.activated",
                    target_kind="agent_page",
                    target_id=page.id,
                    created_at=FIRST_EVENT,
                    payload={
                        "version_id": str(version.id),
                        "previous_active_version_id": None,
                    },
                )
            )
        page_ids = [page.id for page in pages]
    engine.dispose()
    config = _config(url)
    command.downgrade(config, "efcbcf724889")
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            timestamps = session.scalars(
                select(AgentPage.content_changed_at).where(AgentPage.id.in_(page_ids))
            ).all()
            assert len(timestamps) == 130
            assert set(timestamps) == {FIRST_EVENT}
    finally:
        engine.dispose()
