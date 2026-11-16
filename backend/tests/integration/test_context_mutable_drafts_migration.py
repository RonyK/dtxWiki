"""One populated draft-conversion traversal per supported dialect."""

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.context_versions.adapters.git_libgit2 import GitSnapshotStore
from backend.context_versions.application import ContextVersionWriter
from backend.context_versions.domain import ContentPayload
from backend.models import Job
from backend.models.context import ContextPageVersion
from backend.schemas.context_write import ReviewDecisionRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_review_disposition import context_version_review_metadata
from backend.services.review_service import ReviewService
from backend.tests.utils.postgres import drop_reflected_postgres_metadata
from backend.utils.content_hash import content_hash

PARENT = "e4f7a2c901bd"
REVISION = "c3d2e4f601ab"


@pytest.mark.release_migration
@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_mutable_draft_conversion_traversal(tmp_path, dialect):
    url = (
        f"sqlite+pysqlite:///{tmp_path / 'drafts.sqlite'}"
        if dialect == "sqlite"
        else os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    )
    if not url or not url.startswith(dialect):
        pytest.skip("requires a dedicated PostgreSQL migration test database")
    engine = sa.create_engine(url)
    assert not (set(sa.inspect(engine).get_table_names()) - {"alembic_version"})
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", url)
    root = tmp_path / "repositories"
    config.attributes["context_repositories_root"] = root
    now = datetime(2026, 11, 14, tzinfo=UTC)
    page_id, actor_id, change_id, review_id = (uuid4() for _ in range(4))
    published_id, editing_id, rejected_id, cycle_id, shared_id, discarded_id = (
        uuid4() for _ in range(6)
    )
    block_map = [{"id": "retained-block", "kind": "paragraph"}]
    withdrawn_id, withdrawn_change, withdrawn_review = (uuid4() for _ in range(3))
    reconcile_id, reconcile_change, reconcile_review, original_decision = (
        uuid4() for _ in range(4)
    )

    def key(value):
        return value.hex if dialect == "sqlite" else value

    try:
        command.upgrade(config, PARENT)
        with Session(engine) as session:
            workspace_id = seed_default_workspace(session).id
            session.commit()
        metadata = sa.MetaData()
        pages, versions, changes, operations, reviews = (
            sa.Table(name, metadata, autoload_with=engine)
            for name in (
                "context_page",
                "context_page_version",
                "change_set",
                "change_operation",
                "review_item",
            )
        )
        store = GitSnapshotStore(root)
        published_oid = store.commit(
            workspace_id=workspace_id,
            page_id=page_id,
            version_id=published_id,
            created_at=now,
            payload=ContentPayload(b"# Published\n", block_map=block_map),
            parents=[],
        )
        store.set_head(workspace_id, page_id, published_oid)
        with engine.begin() as connection:
            connection.execute(
                pages.insert().values(
                    id=key(page_id),
                    workspace_id=key(workspace_id),
                    slug="mutable-draft",
                    title="Page",
                    page_type="concept",
                    tags=["tag"],
                    aliases=[],
                    lifecycle_status="published",
                    created_at=now,
                    updated_at=now,
                    stale_candidate=False,
                    is_public=False,
                )
            )
            connection.execute(
                changes.insert().values(
                    id=key(change_id),
                    workspace_id=key(workspace_id),
                    created_at=now,
                    updated_at=now,
                    reason="rejected draft",
                    trigger_type="manual",
                    actor_type="human",
                    actor_id=key(actor_id),
                    status="rejected",
                    risk_level="low",
                    cite_call_skipped=False,
                )
            )
            connection.execute(
                reviews.insert().values(
                    id=key(review_id),
                    workspace_id=key(workspace_id),
                    change_set_id=key(change_id),
                    created_at=now,
                    updated_at=now,
                    review_type="context_update",
                    status="rejected",
                    risk_level="low",
                )
            )
            connection.execute(
                operations.insert().values(
                    id=key(uuid4()),
                    workspace_id=key(workspace_id),
                    change_set_id=key(change_id),
                    created_at=now,
                    target_type="context_page",
                    target_id=key(page_id),
                    operation="update",
                    diff_ref=f"context_page_version:{rejected_id}",
                )
            )
            seed = [
                (published_id, None, "published_snapshot", "# Published\n"),
                (editing_id, published_id, "editing", "# Work\n"),
                (rejected_id, rejected_id, "draft", "# Rejected\n"),
                (cycle_id, None, "draft", "# Cycle\n"),
                (shared_id, editing_id, "draft", "# Shared\n"),
                (discarded_id, published_id, "discarded", "# Private discard\n"),
                (reconcile_id, published_id, "draft", "# Reconciliation\n"),
                (withdrawn_id, withdrawn_id, "draft", "# Withdrawn\n"),
            ]
            for number, (version_id, parent_id, status, body) in enumerate(seed, 1):
                connection.execute(
                    versions.insert().values(
                        id=key(version_id),
                        workspace_id=key(workspace_id),
                        page_id=key(page_id),
                        version_no=number,
                        parent_version_id=key(parent_id) if parent_id else None,
                        version_status=status,
                        body_md=body,
                        content_hash=content_hash(body),
                        content_byte_size=len(body.encode()) if number == 1 else None,
                        commit_oid=published_oid if number == 1 else None,
                        graph_status="linear",
                        actor_type="human",
                        actor_id=key(actor_id),
                        created_by_actor_kind="human",
                        created_by_actor_id=str(actor_id),
                        rendering_profile="gfm_mermaid",
                        created_at=now + timedelta(minutes=number - 1),
                        last_saved_at=now + timedelta(minutes=number - 1),
                        editing_revision=1,
                        block_map=block_map,
                    )
                )
            connection.execute(
                versions.update()
                .where(versions.c.id == key(cycle_id))
                .values(parent_version_id=key(cycle_id))
            )
            connection.execute(
                changes.insert().values(
                    id=key(reconcile_change),
                    workspace_id=key(workspace_id),
                    created_at=now,
                    updated_at=now,
                    reason="old branch request",
                    trigger_type="manual",
                    actor_type="human",
                    actor_id=key(actor_id),
                    status="pending_review",
                    risk_level="high",
                    cite_call_skipped=False,
                )
            )
            connection.execute(
                reviews.insert().values(
                    id=key(reconcile_review),
                    workspace_id=key(workspace_id),
                    change_set_id=key(reconcile_change),
                    created_at=now,
                    updated_at=now,
                    review_type="context_reconciliation",
                    status="pending",
                    risk_level="high",
                )
            )
            connection.execute(
                operations.insert().values(
                    id=key(uuid4()),
                    workspace_id=key(workspace_id),
                    change_set_id=key(reconcile_change),
                    created_at=now,
                    target_type="context_page",
                    target_id=key(page_id),
                    operation="patch_draft_diverged_manifest",
                    diff_ref=f"context_page_version:{reconcile_id}",
                )
            )
            connection.execute(
                changes.insert().values(
                    id=key(withdrawn_change),
                    workspace_id=key(workspace_id),
                    created_at=now,
                    updated_at=now,
                    reason="withdrawn draft",
                    trigger_type="manual",
                    actor_type="human",
                    actor_id=key(actor_id),
                    status="withdrawn",
                    risk_level="low",
                    cite_call_skipped=False,
                )
            )
            connection.execute(
                reviews.insert().values(
                    id=key(withdrawn_review),
                    workspace_id=key(workspace_id),
                    change_set_id=key(withdrawn_change),
                    created_at=now,
                    updated_at=now,
                    review_type="context_update",
                    status="withdrawn",
                    risk_level="low",
                )
            )
            connection.execute(
                operations.insert().values(
                    id=key(uuid4()),
                    workspace_id=key(workspace_id),
                    change_set_id=key(withdrawn_change),
                    created_at=now,
                    target_type="context_page",
                    target_id=key(page_id),
                    operation="update",
                    diff_ref=f"context_page_version:{withdrawn_id}",
                )
            )
            decisions = sa.Table("review_decision", metadata, autoload_with=connection)
            connection.execute(
                decisions.insert().values(
                    id=key(original_decision),
                    workspace_id=key(workspace_id),
                    review_item_id=key(review_id),
                    created_at=now,
                    decision_type="reject",
                    actor_type="human",
                    actor_id=key(actor_id),
                    decision_reason="Original refusal",
                    resulting_review_status="rejected",
                    resulting_change_set_status="rejected",
                )
            )
            ledger = sa.Table(
                "mcp_write_idempotency", sa.MetaData(), autoload_with=connection
            )
            connection.execute(
                ledger.insert().values(
                    id=key(uuid4()),
                    workspace_id=key(workspace_id),
                    actor_kind="human",
                    actor_id=str(actor_id),
                    tool_name="begin_context_edit",
                    idempotency_key="existing-receipt",
                    request_fingerprint="unchanged-fingerprint",
                    result_payload={
                        "version_id": str(editing_id),
                        "version_status": "editing",
                        "latest_draft_version_id": str(editing_id),
                    },
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                )
            )
            connection.execute(
                pages.update()
                .where(pages.c.id == key(page_id))
                .values(
                    current_version_id=key(shared_id),
                    current_published_version_id=key(published_id),
                    latest_draft_version_id=key(shared_id),
                )
            )
        # The locator revision is already complete before this draft cutover.
        # Only retirement of the old draft reminder jobs belongs here.
        with Session(engine) as session:
            session.add_all(
                Job(
                    workspace_id=workspace_id,
                    job_type="context_reconciliation_reminder",
                    trigger_type="manual",
                    status=status,
                    target_ref={"review_item_id": str(reconcile_review)},
                    next_attempt_at=now,
                )
                for status in ("queued", "running")
            )
            session.commit()
        command.upgrade(config, REVISION)
        table = sa.Table("context_page_version", sa.MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            rows = {
                UUID(str(row["id"])): row
                for row in connection.execute(sa.select(table)).mappings()
            }
            assert rows[published_id]["commit_oid"] == published_oid
            assert (
                rows[editing_id]["version_status"]
                == rows[shared_id]["version_status"]
                == "draft"
            )
            assert UUID(str(rows[shared_id]["parent_version_id"])) == published_id
            assert rows[rejected_id]["version_status"] == "closed"
            assert rows[rejected_id]["body_md"] is None
            assert (
                store.read(
                    workspace_id=workspace_id,
                    page_id=page_id,
                    commit_oid=rows[rejected_id]["commit_oid"],
                    descriptor=ContentPayload(b"").descriptor,
                )
                == b"# Rejected\n"
            )
            assert store.read_parents(
                workspace_id=workspace_id,
                page_id=page_id,
                commit_oid=rows[rejected_id]["commit_oid"],
            ) == (published_oid,)
            assert UUID(str(rows[cycle_id]["parent_version_id"])) == rejected_id
            assert rows[discarded_id]["version_status"] == "discarded"
            assert rows[discarded_id]["body_md"] == "# Private discard\n"
            assert rows[shared_id]["page_metadata"]["title"] == "Page"
            assert (
                len(
                    list(
                        connection.execute(
                            sa.select(reviews).where(reviews.c.status == "pending")
                        )
                    )
                )
                == 4
            )
            pending_reviews = (
                connection.execute(
                    sa.select(reviews).where(reviews.c.status == "pending")
                )
                .mappings()
                .all()
            )
            inspections = [
                review
                for review in pending_reviews
                if (review["risk_signals"] or {})
                .get("migration_parent_repaired", {})
                .get("inspection_only")
            ]
            assert {
                review["risk_signals"]["migration_parent_repaired"]["version_id"]
                for review in inspections
            } == {str(rejected_id), str(withdrawn_id), str(cycle_id)}
            # The intent signal survives draft saves; the verdict is derived.
            assert all(
                review["risk_signals"]["caller_needs_review"] for review in inspections
            )
            normal = next(
                row
                for row in pending_reviews
                if UUID(str(row["id"])) == reconcile_review
            )
            assert normal["review_type"] == "context_update"
            assert (
                connection.scalar(
                    sa.select(operations.c.operation).where(
                        operations.c.change_set_id == key(reconcile_change)
                    )
                )
                == "update"
            )
            assert store.list_heads(workspace_id) == {page_id: published_oid}
            ledger_row = connection.execute(sa.select(ledger)).mappings().one()
            assert ledger_row["request_fingerprint"] == "unchanged-fingerprint"
            assert ledger_row["result_payload"] == {
                "version_id": str(editing_id),
                "version_status": "draft",
            }
            jobs = sa.Table("job", sa.MetaData(), autoload_with=connection)
            queued = (
                connection.execute(
                    sa.select(jobs).where(
                        jobs.c.idempotency_key == f"pr3-index:context_page:{page_id}"
                    )
                )
                .mappings()
                .one()
            )
            assert queued["job_type"] == "embed" and queued["status"] == "queued"
            assert queued["target_ref"] == {"kind": "context_page", "id": str(page_id)}

        writer = ContextVersionWriter(store, store)
        with Session(
            engine,
            info={"context_snapshot_store": store, "context_version_writer": writer},
        ) as session:
            reminders = session.scalars(
                sa.select(Job).where(Job.job_type == "context_reconciliation_reminder")
            ).all()
            assert len(reminders) == 2
            assert all(
                job.status == "completed" and job.next_attempt_at is None
                for job in reminders
            )
            service = ReviewService(context_version_writer=writer)
            ended_versions = session.scalars(
                sa.select(ContextPageVersion).where(
                    ContextPageVersion.id.in_((rejected_id, withdrawn_id))
                )
            ).all()
            before = context_version_review_metadata(
                session, ended_versions, workspace_id=workspace_id
            )
            assert (before[rejected_id].status, before[rejected_id].review_item_id) == (
                "rejected",
                review_id,
            )
            assert (
                before[withdrawn_id].status,
                before[withdrawn_id].review_item_id,
            ) == ("withdrawn", withdrawn_review)
            for inspection in inspections:
                acknowledged = service.apply_decision(
                    session=session,
                    workspace_id=workspace_id,
                    review_item_id=UUID(str(inspection["id"])),
                    request=ReviewDecisionRequest(decision_type="approve"),
                    actor_kind="human",
                    actor_id=str(actor_id),
                    actor_role="owner",
                    actor_provenance="browser_session",
                )
                assert acknowledged.status == "approved"
            assert (
                context_version_review_metadata(
                    session, ended_versions, workspace_id=workspace_id
                )
                == before
            )
            requested = service.apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=reconcile_review,
                request=ReviewDecisionRequest(
                    decision_type="request_changes",
                    decision_reason="Please inspect this draft",
                ),
                actor_kind="human",
                actor_id=str(actor_id),
                actor_role="owner",
                actor_provenance="browser_session",
            )
            assert requested.status == "changes_requested"
            session.commit()
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(reviews.c.status).where(reviews.c.id == key(review_id))
                )
                == "rejected"
            )
            assert (
                connection.scalar(
                    sa.select(decisions.c.decision_reason).where(
                        decisions.c.id == key(original_decision)
                    )
                )
                == "Original refusal"
            )
            assert connection.scalar(
                sa.select(pages.c.current_published_version_id).where(
                    pages.c.id == key(page_id)
                )
            ) == key(published_id)
        assert store.list_heads(workspace_id) == {page_id: published_oid}

        assert "uq_cpv_current_editing_per_author" not in {
            item["name"]
            for item in sa.inspect(engine).get_indexes("context_page_version")
        }
        assert "context_version_parent" in sa.inspect(engine).get_table_names()
        # This data conversion intentionally has no reverse writer. A refused
        # downgrade must leave the completed forward state unchanged.
        with pytest.raises(RuntimeError, match="pre-upgrade dtxWiki backup"):
            command.downgrade(config, PARENT)
        command.upgrade(config, REVISION)
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(reviews)
                    .where(reviews.c.status == "pending")
                )
                == 0
            )
    finally:
        if dialect == "postgresql":
            drop_reflected_postgres_metadata(engine)
        engine.dispose()
