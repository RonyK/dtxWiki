import json

import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session, sessionmaker

from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import (
    ContextPage,
    ContextPageVersion,
    PageLink,
    PageNameIndex,
)
from backend.models.job import Job
from backend.models.review import ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.repositories import context_page_repo
from backend.schemas.context_write import PublishContextPageRequest
from backend.schemas.tree import TreeNodeUpdate
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_write_service import ContextWriteService
from backend.services.domain_read import DomainReadService
from backend.services.page_service import (
    PageConflictError,
    PageNotFoundError,
    PageService,
)
from backend.services.search_service import SearchService
from backend.services.tree_service import TreeService


def make_service(change_audit_recorder=None) -> PageService:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory.begin() as session:
        seed_default_workspace(session)
    return PageService(
        session_factory,
        change_audit_recorder=change_audit_recorder,
    )


def _page_service_audit_rows(session: Session) -> list[AuditEvent]:
    """Exclude only the audit created before PageService exercises begin."""

    return session.scalars(
        select(AuditEvent)
        .where(AuditEvent.action != "context_media.legacy_cutover.completed")
        .order_by(AuditEvent.created_at, AuditEvent.id)
    ).all()


def test_page_service_creates_reads_lists_and_audits_page():
    service = make_service()

    created = service.write_page(
        workspace_slug="default",
        slug="hello",
        title="Hello",
        body_md="# Hello",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    read = service.read_page(page_id=created.id)
    listed = service.list_pages(workspace_slug="default")

    assert read.body_md == "# Hello"
    assert read.version_id == created.version_id
    assert listed.pages[0].slug == "hello"

    with service.session_factory() as session:
        audit_rows = _page_service_audit_rows(session)

    assert [row.action for row in audit_rows] == ["page.create"]
    assert audit_rows[0].actor_kind == "human"


def test_page_service_first_touch_race_converges_on_existing_slug(monkeypatch):
    service = make_service()
    winner = service.write_page(
        workspace_slug="default",
        slug="raced-page",
        title="Raced Page",
        body_md="# Raced Page\n\nFirst writer.",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    original = context_page_repo.get_page_by_slug_for_update
    calls = 0

    def stale_first_lookup(session, workspace_id, slug):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original(session, workspace_id, slug)

    monkeypatch.setattr(
        context_page_repo,
        "get_page_by_slug_for_update",
        stale_first_lookup,
    )

    updated = service.write_page(
        workspace_slug="default",
        slug="raced-page",
        title="Raced Page",
        body_md="# Raced Page\n\nSecond writer.",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=winner.version_id,
        base_content_hash=winner.content_hash,
        actor_provenance="browser_session",
    )

    assert calls >= 2
    assert updated.id == winner.id
    assert updated.version_id != winner.version_id
    assert updated.body_md.endswith("Second writer.")


def test_legacy_page_publish_enqueues_embed_for_created_version():
    service = make_service()

    created = service.write_page(
        workspace_slug="default",
        slug="embed-me",
        title="Embed me",
        body_md="# Embed me",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    with service.session_factory() as session:
        jobs = session.scalars(select(Job).order_by(Job.created_at)).all()

    assert len(jobs) == 1
    assert jobs[0].job_type == "embed"
    assert jobs[0].trigger_type == "context_published"
    assert jobs[0].target_ref == {"kind": "context_page", "id": str(created.id)}


def test_page_service_auto_fix_normalizes_save_path():
    # I7: the regular page editor save path (write_page/update_page) normalizes
    # the body when the workspace has Auto-fix on, and leaves it as authored off.
    service = make_service()

    # off (default): authored body is preserved.
    off = service.write_page(
        workspace_slug="default",
        slug="raw",
        title="Raw",
        body_md="# Raw\n\n* a\n\n\n\nend   \n",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    assert "* a" in off.body_md
    assert "- a" not in off.body_md

    with service.session_factory.begin() as session:
        seed_default_workspace(session).auto_fix_enabled = True

    # on: write_page normalizes (markers, blank runs, trailing whitespace).
    created = service.write_page(
        workspace_slug="default",
        slug="fixed",
        title="Fixed",
        body_md="# Fixed\n\n* a\n\n\n\nend   \n",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    assert "- a" in created.body_md
    assert "* a" not in created.body_md
    assert "end   " not in created.body_md

    # on: update_page normalizes too.
    updated = service.update_page(
        page_id=created.id,
        title="Fixed",
        body_md="# Fixed\n\n+ b\n\nmore   \n",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )
    assert "- b" in updated.body_md
    assert "+ b" not in updated.body_md


def test_page_service_lists_pages_without_per_page_version_queries():
    service = make_service()
    for index in range(3):
        service.write_page(
            workspace_slug="default",
            slug=f"page-{index}",
            title=f"Page {index}",
            body_md=f"# Page {index}",
            actor_kind="human",
            actor_id="dev-local-user",
            actor_provenance="browser_session",
        )
    engine = service.session_factory.kw["bind"]
    version_statements: list[str] = []

    def count_version_selects(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().lower().startswith(
            "select"
        ) and "context_page_version" in statement.lower():
            version_statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_version_selects)
    try:
        listed = service.list_pages(workspace_slug="default", limit=3)
    finally:
        event.remove(engine, "before_cursor_execute", count_version_selects)

    assert len(listed.pages) == 3
    assert len(version_statements) == 1


def test_page_service_updates_with_linear_parent_and_audit_row():
    service = make_service()
    created = service.write_page(
        workspace_slug="default",
        slug="hello",
        title="Hello",
        body_md="# Hello",
        actor_kind="mcp_client",
        actor_id="dev-local-mcp",
        actor_provenance="browser_session",
    )

    with service.session_factory() as session:
        created_audits = _page_service_audit_rows(session)
    assert [audit.action for audit in created_audits] == ["page.create"]
    created_audit_id = created_audits[0].id

    updated = service.write_page(
        workspace_slug="default",
        slug="hello",
        title="Hello v2",
        body_md="# Hello v2",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )

    assert updated.version_id != created.version_id
    assert updated.parent_version_id == created.version_id
    assert updated.content_hash != created.content_hash

    with service.session_factory() as session:
        audits = {audit.id: audit for audit in _page_service_audit_rows(session)}

    # The two service calls establish the order; wall-clock timestamps do not,
    # because the clock may step back between the writes.
    assert len(audits) == 2
    assert audits[created_audit_id].action == "page.create"
    update_audits = [audit for key, audit in audits.items() if key != created_audit_id]
    assert [audit.action for audit in update_audits] == ["page.update"]


def test_page_service_emits_change_review_audit_bundle_for_writes():
    service = make_service()
    created = service.write_page(
        workspace_slug="default",
        slug="hello",
        title="Hello",
        body_md="# Hello",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    with service.session_factory() as session:
        created_audits = _page_service_audit_rows(session)
    assert [audit.action for audit in created_audits] == ["page.create"]
    created_audit_id = created_audits[0].id

    updated = service.write_page(
        workspace_slug="default",
        slug="hello",
        title="Hello v2",
        body_md="# Hello v2",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )

    with service.session_factory() as session:
        change_sets = session.scalars(select(ChangeSet)).all()
        operations = session.scalars(select(ChangeOperation)).all()
        reviews = session.scalars(select(ReviewItem)).all()
        audits = _page_service_audit_rows(session)

    assert [change.status for change in change_sets] == ["applied", "applied"]
    # A metadata patch is staged whenever the write actually carries one, not
    # only on the review path, because comparing two versions has to be able to
    # say the title or tags moved. That makes the ledger record the metadata
    # change alongside the body write. The operations table has no order of its
    # own, so each operation is matched by what it targets, not by position.
    assert sorted(
        (
            operation.operation,
            operation.target_type,
            operation.target_id,
            operation.diff_ref,
        )
        for operation in operations
    ) == [
        (
            "create",
            "context_page",
            created.id,
            f"context_page_version:{created.version_id}",
        ),
        (
            "metadata_patch",
            "context_page",
            created.id,
            f"context_page_version:{updated.version_id}",
        ),
        (
            "update",
            "context_page",
            created.id,
            f"context_page_version:{updated.version_id}",
        ),
    ]
    assert [review.status for review in reviews] == ["approved", "approved"]
    # The create audit was observed before the update ran, so it is matched by
    # identity: the clock may step back between the writes, which would
    # reorder a created_at listing.
    audits_by_id = {audit.id: audit for audit in audits}
    assert len(audits) == 2
    assert audits_by_id[created_audit_id].action == "page.create"
    update_audits = [audit for audit in audits if audit.id != created_audit_id]
    assert [audit.action for audit in update_audits] == ["page.update"]


class FailingRecorder:
    def record_context_page_write(self, session, workspace_id, actor_kind, **_):
        session.add(
            ChangeSet(
                workspace_id=workspace_id,
                trigger_type="manual_edit",
                actor_type=actor_kind,
                status="applied",
            )
        )
        session.flush()
        raise RuntimeError("forced recorder failure")


def test_page_service_rolls_back_write_when_change_audit_recorder_fails():
    service = make_service(change_audit_recorder=FailingRecorder())

    with pytest.raises(RuntimeError, match="forced recorder failure"):
        service.write_page(
            workspace_slug="default",
            slug="rollback",
            title="Rollback",
            body_md="# Rollback",
            actor_kind="human",
            actor_id="dev-local-user",
            actor_provenance="browser_session",
        )

    with service.session_factory() as session:
        assert session.scalar(
            select(ContextPage).where(ContextPage.slug == "rollback")
        ) is None
        assert session.scalars(select(ContextPageVersion)).all() == []
        assert session.scalars(select(ChangeSet)).all() == []
        assert _page_service_audit_rows(session) == []


def test_page_service_rejects_stale_update_without_overwriting():
    service = make_service()
    created = service.write_page(
        workspace_slug="default",
        slug="hello",
        title="Hello",
        body_md="# Hello",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    updated = service.write_page(
        workspace_slug="default",
        slug="hello",
        title="Hello v2",
        body_md="# Hello v2",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )

    try:
        service.write_page(
            workspace_slug="default",
            slug="hello",
            title="Stale write",
            body_md="# Stale",
            actor_kind="human",
            actor_id="dev-local-user",
            base_version_id=created.version_id,
            base_content_hash=created.content_hash,
            actor_provenance="browser_session",
        )
    except PageConflictError as exc:
        conflict = exc
    else:
        raise AssertionError("Expected stale update to raise PageConflictError")

    assert conflict.error_code == "stale_base_version"
    assert conflict.server_version_id == updated.version_id
    assert conflict.server_content_hash == updated.content_hash
    assert service.read_page(page_id=created.id).body_md == "# Hello v2"


def test_page_service_rejects_version_id_belonging_to_another_page():
    service = make_service()
    page_a = service.write_page(
        workspace_slug="default",
        slug="page-a",
        title="Page A",
        body_md="# A",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    page_b = service.write_page(
        workspace_slug="default",
        slug="page-b",
        title="Page B",
        body_md="# B",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )

    with pytest.raises(PageNotFoundError):
        service.read_page(page_id=page_a.id, version_id=page_b.version_id)


def test_page_service_rejects_base_version_id_belonging_to_another_page():
    service = make_service()
    page_a = service.write_page(
        workspace_slug="default",
        slug="page-a",
        title="Page A",
        body_md="# A",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    page_b = service.write_page(
        workspace_slug="default",
        slug="page-b",
        title="Page B",
        body_md="# B",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )

    with pytest.raises(PageNotFoundError):
        service.update_page(
            page_id=page_a.id,
            title="Page A v2",
            body_md="# A v2",
            actor_kind="human",
            actor_id="dev-local-user",
            base_version_id=page_b.version_id,
            base_content_hash=page_b.content_hash,
            actor_provenance="browser_session",
        )

    assert service.read_page(page_id=page_a.id).body_md == "# A"


def test_wikilink_pin_survives_rename_rescan_and_restore():
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from backend.app.container import assemble
    from backend.app.settings import Settings
    from backend.models.lint import LintFinding
    from backend.schemas.lint import LintRunCreateRequest

    service = make_service()
    self_origin = "https://pin-origin-fixture.invalid:54443"
    configured = assemble(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="test-session-secret",
            public_base_url=self_origin,
        ),
        session_factory=service.session_factory,
    )
    service = configured.get_page_service()
    reader = configured.get_services().domain_read
    with service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        # This fixture exercises indexing of legacy bytes, not the save fixer.
        workspace.lint_policy = {"link_fix_mode": "suggest"}
    target = service.write_page(
        workspace_slug="default",
        slug="한글-문서",
        title="한글 문서",
        body_md="# 한글 문서",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    # Distinct failure mechanisms in the same existing lifecycle: normalized
    # page-id keys must coalesce URL forms; slug reuse must respect prior pins;
    # hidden pins must fail closed rather than fall back to the reused slug.
    linked_body = (
        "# Link Source\n\nSee [[한글 문서]]. "
        "[Legacy](/context/한글-문서.md) "
        f"[Configured]({self_origin}/context/한글-문서.md) "
        f"[Short](/p/{target.id}) [Full](/p/{target.workspace_id}/{target.id}) "
        f"[Navigation](/context?target={target.id})"
    )
    linked = service.write_page(
        workspace_slug="default",
        slug="link-source",
        title="Link Source",
        body_md=linked_body,
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    with service.session_factory.begin() as session:
        link = session.scalar(
            select(PageLink).where(
                PageLink.source_version_id == linked.version_id,
                PageLink.locator_key == "wikilink:한글-문서",
            )
        )
        assert link.target_slug == "한글-문서"
        assert link.target_page_id == target.id
        keyed = {
            row.locator_key: row
            for row in session.scalars(
                select(PageLink).where(PageLink.source_version_id == linked.version_id)
            )
        }
        assert set(keyed) == {
            "wikilink:한글-문서",
            "concept_path:한글-문서",
            f"page_id:{target.id}",
        }
        assert keyed[f"page_id:{target.id}"].target_slug is None
        assert keyed[f"page_id:{target.id}"].occurrence_count == 3
        assert keyed["concept_path:한글-문서"].occurrence_count == 2
        session.get(ContextPage, target.id).slug = "renamed-korean-page"
    replacement = service.write_page(
        workspace_slug="default",
        slug="한글-문서",
        title="Replacement",
        body_md="# Replacement",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    assert replacement.id != target.id

    duplicate = service.write_page(
        workspace_slug="default",
        slug="link-source",
        title="Link Source",
        body_md=linked_body,
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=linked.version_id,
        base_content_hash=linked.content_hash,
        actor_provenance="browser_session",
    )
    service.write_page(
        workspace_slug="default",
        slug="link-source",
        title="Link Source",
        body_md=linked_body + "\n\nChanged.",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=duplicate.version_id,
        base_content_hash=duplicate.content_hash,
        actor_provenance="browser_session",
    )
    restored = service.restore_version(
        page_id=linked.id,
        version_id=linked.version_id,
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    with service.session_factory.begin() as session:
        pins = session.scalars(
            select(PageLink).where(PageLink.source_page_id == linked.id)
        ).all()
        source_page = session.get(ContextPage, linked.id)
        live_version_ids = {
            source_page.current_published_version_id,
            source_page.latest_draft_version_id,
            source_page.current_version_id,
        } - {None}
        assert {pin.source_version_id for pin in pins} <= live_version_ids
        assert restored.version_id in {pin.source_version_id for pin in pins}
        assert all(pin.target_page_id == target.id for pin in pins)
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        detail = reader.read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=linked.id,
            version_id=restored.version_id,
            actor=ActorContext(
                actor_kind="human",
                actor_id="editor",
                workspace_role="editor",
                role="editor",
                workspace_id=workspace.id,
            ),
            include_warnings=False,
        )
    assert detail.wikilink_targets[0].target_slug == "한글-문서"
    assert detail.wikilink_targets[0].target_page_id == target.id
    assert detail.wikilink_targets[0].current_slug == "renamed-korean-page"

    assert len(detail.link_targets) == 6
    assert all(entry.state == "ok" for entry in detail.link_targets)
    assert all(entry.target_page_id == target.id for entry in detail.link_targets)
    # Budget exception: the six mixed locators above do not distinguish one
    # query per target from a request-scoped batch. Fifty distinct published
    # page IDs rule out per-target memoization, while a real SQLite session and
    # persisted PageLink rows are needed to count SELECTs rather than only
    # asserting a mocked helper call count.
    from backend.services.context_link_targets import context_link_targets
    from backend.services.context_links import (
        LinkContext,
        LinkResolutionBatch,
        classify,
        resolve,
    )

    bulk_target_pages = [target]
    for index in range(49):
        bulk_target_pages.append(
            service.write_page(
                workspace_slug="default",
                slug=f"link-query-target-{index}",
                title=f"Link Query Target {index}",
                body_md=f"# Link Query Target {index}",
                actor_kind="human",
                actor_id="dev-local-user",
                actor_provenance="browser_session",
            )
        )
    bulk_body = "\n".join(
        f"[Section {index}](/p/{page.workspace_id}/{page.id}#section-{index})"
        for index, page in enumerate(bulk_target_pages)
    )
    bulk_source = service.write_page(
        workspace_slug="default",
        slug="link-query-batch",
        title="Link Query Batch",
        body_md=bulk_body,
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    engine = service.session_factory.kw["bind"]
    query_count = 0
    measuring = False

    def count_projection_selects(_conn, _cursor, statement, _parameters, _ctx, _many):
        nonlocal query_count
        if measuring and statement.lstrip().upper().startswith(("SELECT", "WITH")):
            query_count += 1

    event.listen(engine, "before_cursor_execute", count_projection_selects)
    try:
        with service.session_factory.begin() as session:
            bulk_page = session.get(ContextPage, bulk_source.id)
            bulk_version = session.get(ContextPageVersion, bulk_source.version_id)
            workspace = session.get(Workspace, target.workspace_id)
            measuring = True
            bulk_targets = context_link_targets(
                session,
                page=bulk_page,
                version=bulk_version,
                actor=ActorContext(
                    actor_kind="human",
                    actor_id="viewer",
                    workspace_role="viewer",
                    role="viewer",
                    workspace_id=workspace.id,
                ),
                self_origins=frozenset(),
            )
            measuring = False
    finally:
        measuring = False
        event.remove(engine, "before_cursor_execute", count_projection_selects)
    assert len(bulk_targets) == 50
    assert all(entry.state == "ok" for entry in bulk_targets)
    assert query_count <= 8

    # Budget exception: this legacy dual-key fallback is a separate failure
    # mode from per-link SQL growth. A copied page version has no exact pins;
    # its old nav_target and page_id aliases must choose the newest historical
    # pin, while a normal wikilink must still inherit its prior source pin.
    fallback_source_v1 = service.write_page(
        workspace_slug="default",
        slug="legacy-pin-source",
        title="Legacy Pin Source",
        body_md="# Legacy Pin Source",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    fallback_source_v2 = service.write_page(
        workspace_slug="default",
        slug="legacy-pin-source",
        title="Legacy Pin Source",
        body_md="# Legacy Pin Source Updated",
        base_version_id=fallback_source_v1.version_id,
        base_content_hash=fallback_source_v1.content_hash,
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    nav_target_id = uuid4()
    nav_locator = classify(f"/context?target={nav_target_id}")
    wiki_locator = classify("[[한글 문서]]")
    with service.session_factory.begin() as session:
        now = datetime.now(UTC)
        session.add_all(
            [
                PageLink(
                    workspace_id=target.workspace_id,
                    source_page_id=fallback_source_v1.id,
                    source_version_id=fallback_source_v1.version_id,
                    locator_key=nav_locator.key,
                    target_slug="한글-문서",
                    target_page_id=target.id,
                    occurrence_count=1,
                    created_at=now - timedelta(days=1),
                ),
                PageLink(
                    workspace_id=target.workspace_id,
                    source_page_id=fallback_source_v1.id,
                    source_version_id=fallback_source_v1.version_id,
                    locator_key=f"page_id:{nav_target_id}",
                    target_slug="link-query-target-48",
                    target_page_id=bulk_target_pages[-1].id,
                    occurrence_count=1,
                    created_at=now,
                ),
                PageLink(
                    workspace_id=target.workspace_id,
                    source_page_id=fallback_source_v1.id,
                    source_version_id=fallback_source_v1.version_id,
                    locator_key=wiki_locator.key,
                    target_slug="한글-문서",
                    target_page_id=target.id,
                    occurrence_count=1,
                    created_at=now,
                ),
            ]
        )
        session.flush()
        source_page = session.get(ContextPage, fallback_source_v2.id)
        source_version = session.get(ContextPageVersion, fallback_source_v2.version_id)
        workspace = session.get(Workspace, target.workspace_id)
        actor = ActorContext(
            actor_kind="human",
            actor_id="editor",
            workspace_role="editor",
            role="editor",
            workspace_id=workspace.id,
        )
        context = LinkContext(
            session=session,
            workspace_id=workspace.id,
            actor=actor,
            source_page_id=source_page.id,
            source_version_id=source_version.id,
        )
        locators = [nav_locator, wiki_locator]
        batch = LinkResolutionBatch(locators, context)
        scalar_results = [resolve(locator, context) for locator in locators]
        batch_results = [resolve(locator, context, batch=batch) for locator in locators]
    assert [result.page_id for result in scalar_results] == [
        bulk_target_pages[-1].id,
        target.id,
    ]
    assert [result.page_id for result in batch_results] == [
        result.page_id for result in scalar_results
    ]
    # Injected Settings must govern SAVE, projection, READ and RUN alike;
    # ambient get_settings() does not contain this unusual trusted origin.
    # This distinct DI failure path reuses the same target and registry fixture.
    absolute_body = (
        f"# Configured Origin\n\n[Target]({self_origin}/context/renamed-korean-page.md)"
    )
    absolute_source = service.write_page(
        workspace_slug="default",
        slug="configured-origin",
        title="Configured Origin",
        body_md=absolute_body,
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    with service.session_factory.begin() as session:
        view = reader.read_context_page(
            session=session,
            workspace_id=target.workspace_id,
            page_id=absolute_source.id,
            include_warnings=False,
        )
        assert view.link_targets[0].state == "ok"
        assert view.link_targets[0].target_page_id == target.id
        configured.get_services().lint.run_lint(
            session,
            target.workspace_id,
            LintRunCreateRequest(trigger_type="manual"),
            "system",
            "lint-fixture",
        )
        assert (
            session.scalar(
                select(LintFinding.id).where(
                    LintFinding.target_id == absolute_source.id,
                    LintFinding.rule_id == "link.internal-noncanonical",
                )
            )
            is not None
        )
        session.get(Workspace, target.workspace_id).lint_policy = {
            "link_fix_mode": "auto"
        }
    fixed_origin = service.write_page(
        workspace_slug="default",
        slug="configured-origin",
        title="Configured Origin",
        body_md=absolute_body,
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
        base_version_id=absolute_source.version_id,
        base_content_hash=absolute_source.content_hash,
    )
    assert self_origin not in fixed_origin.body_md
    assert f"](/p/{target.workspace_id}/{target.id})" in fixed_origin.body_md
    # An independent pin lifetime failure needs two private editors: another
    # author's autosave moves the global pointer without ending the first
    # author's live edit. Its pin must still precede a subsequently reused slug.
    with service.session_factory.begin() as session:
        session.get(Workspace, target.workspace_id).lint_policy = {
            "link_fix_mode": "suggest"
        }
    private_source = service.write_page(
        workspace_slug="default",
        slug="private-pin-source",
        title="Private Pin Source",
        body_md="# Private Pin Source",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    edits = [
        service.begin_context_edit(
            page_id=private_source.id,
            workspace_id=target.workspace_id,
            actor_kind="human",
            actor_id=author,
            actor_provenance="browser_session",
            idempotency_key="private-pin-begin-" + author,
        )
        for author in ("first-editor", "second-editor")
    ]
    first_edit = service.autosave_page(
        page_id=private_source.id,
        workspace_id=target.workspace_id,
        title="Private Pin Source",
        body_md="# Private Pin Source\n\n[Target](/context/renamed-korean-page.md)",
        actor_kind="human",
        actor_id="first-editor",
        actor_provenance="browser_session",
        base_version_id=edits[0].version_id,
        base_content_hash=edits[0].content_hash,
        expected_editing_revision=edits[0].editing_revision,
        editing_version_id=edits[0].version_id,
        idempotency_key="private-pin-save-first-editor",
    )
    service.autosave_page(
        page_id=private_source.id,
        workspace_id=target.workspace_id,
        title="Private Pin Source",
        body_md="# Private Pin Source\n\nAnother editor's private text.",
        actor_kind="human",
        actor_id="second-editor",
        actor_provenance="browser_session",
        base_version_id=edits[1].version_id,
        base_content_hash=edits[1].content_hash,
        expected_editing_revision=edits[1].editing_revision,
        editing_version_id=edits[1].version_id,
        idempotency_key="private-pin-save-second-editor",
    )
    with service.session_factory.begin() as session:
        assert session.scalar(
            select(PageLink.target_page_id).where(
                PageLink.source_version_id == first_edit.version_id
            )
        ) == target.id
        session.get(ContextPage, target.id).slug = "private-pin-renamed"
    service.write_page(
        workspace_slug="default",
        slug="renamed-korean-page",
        title="Second Replacement",
        body_md="# Second Replacement",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    with service.session_factory.begin() as session:
        private_view = reader.read_context_page(
            session=session,
            workspace_id=target.workspace_id,
            page_id=private_source.id,
            version_id=first_edit.version_id,
            include_warnings=False,
            actor=ActorContext(
                actor_kind="human",
                actor_id="first-editor",
                workspace_role="editor",
                role="editor",
                workspace_id=target.workspace_id,
            ),
        )
        assert private_view.link_targets[0].target_page_id == target.id
    with service.session_factory.begin() as session:
        from backend.services.page_service import refresh_page_links

        target_page = session.get(ContextPage, target.id)
        target_page.lifecycle_status = "archived"
        source_page = session.get(ContextPage, linked.id)
        source_version = session.get(ContextPageVersion, restored.version_id)
        refresh_page_links(session, page=source_page, version=source_version)
        assert all(
            row.target_page_id == target.id
            for row in session.scalars(
                select(PageLink).where(PageLink.source_page_id == linked.id)
            )
        )
        hidden = reader.read_context_page(
            session=session,
            workspace_id=target.workspace_id,
            page_id=linked.id,
            version_id=restored.version_id,
            include_warnings=False,
            actor=ActorContext(
                actor_kind="human",
                actor_id="editor",
                workspace_role="editor",
                role="editor",
                workspace_id=target.workspace_id,
            ),
        )
    assert all(entry.state == "unavailable" for entry in hidden.link_targets)
    assert all(entry.target_page_id is None for entry in hidden.link_targets)
    assert all(entry.href is None for entry in hidden.link_targets)
    assert hidden.wikilink_targets[0].target_page_id is None


def test_context_read_includes_unresolved_wikilink_target():
    service = make_service()
    linked = service.write_page(
        workspace_slug="default",
        slug="unresolved-source",
        title="Unresolved Source",
        body_md="# Unresolved Source\n\nSee [[link to nothing]].",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    with service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        detail = DomainReadService().read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=linked.id,
            include_warnings=False,
        )
    assert detail.wikilink_targets[0].target_slug == "link-to-nothing"
    assert detail.wikilink_targets[0].target_page_id is None
    assert detail.wikilink_targets[0].current_slug is None


def test_context_read_resolves_wikilink_target_created_after_source():
    # H-1 (260908 UI audit): a link pinned at write time to a page that did
    # not exist yet left target_page_id NULL forever, so the link rendered
    # broken even after the target page was created -- the most common
    # authoring order in a wiki. Reads must fall back to a live slug lookup
    # for exactly the unresolved rows.
    service = make_service()
    linked = service.write_page(
        workspace_slug="default",
        slug="written-first",
        title="Written First",
        body_md="# Written First\n\nSee [[written-second]].",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    with service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        detail = DomainReadService().read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=linked.id,
            include_warnings=False,
        )
    assert detail.wikilink_targets[0].target_page_id is None

    target = service.write_page(
        workspace_slug="default",
        slug="written-second",
        title="Written Second",
        body_md="# Written Second",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )

    with service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        detail = DomainReadService().read_context_page(
            session=session,
            workspace_id=workspace.id,
            page_id=linked.id,
            include_warnings=False,
        )
    assert detail.wikilink_targets[0].target_slug == "written-second"
    assert detail.wikilink_targets[0].target_page_id == target.id
    assert detail.wikilink_targets[0].current_slug == "written-second"


def test_page_service_noop_duplicate_content_returns_current_version():
    service = make_service()
    created = service.write_page(
        workspace_slug="default",
        slug="hello",
        title="Hello",
        body_md="# Hello",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )

    duplicate = service.write_page(
        workspace_slug="default",
        slug="hello",
        title="Hello",
        body_md="# Hello",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )

    assert duplicate.version_id == created.version_id


def _seed_restored_draft(service):
    created = service.write_page(
        workspace_slug="default",
        slug="restore-meta",
        title="Restore Meta",
        body_md="# Restore Meta\n\nv1",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    updated = service.write_page(
        workspace_slug="default",
        slug="restore-meta",
        title="Restore Meta",
        body_md="# Restore Meta\n\nv2",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )
    restored = service.restore_version(
        page_id=created.id,
        version_id=created.version_id,
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    return created, updated, restored


def test_page_and_context_publish_surfaces_dedup_same_content_embed_job():
    service = make_service()
    created, _updated, restored = _seed_restored_draft(service)
    assert restored.content_hash == created.content_hash

    with service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        assert page is not None
        ContextWriteService().publish_draft(
            session=session,
            workspace_id=page.workspace_id,
            page_id=page.id,
            request=PublishContextPageRequest(
                version_id=restored.version_id,
                reason="cross-surface embed dedup",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
        )

    expected_key = (
        f"context_page:{created.id}:published:"
        f"{created.version_id}:{created.content_hash}"
    )
    with service.session_factory() as session:
        jobs = session.scalars(
            select(Job).where(Job.idempotency_key == expected_key)
        ).all()

    assert len(jobs) == 1
    assert jobs[0].trigger_type == "context_published"


def test_autosave_from_reviewed_restore_forks_without_embed_job():
    service = make_service()
    created, _updated, restored = _seed_restored_draft(service)
    with service.session_factory.begin() as session:
        session.execute(delete(Job))

    autosaved = service.autosave_page(
        page_id=created.id,
        workspace_id=created.workspace_id,
        title="Restore Meta",
        body_md="# Restore Meta\n\nautosaved draft only",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=restored.version_id,
        base_content_hash=restored.content_hash,
        session_version_id=restored.version_id,
        actor_provenance="browser_session",
        idempotency_key="restore-meta-autosave",
    )

    with service.session_factory() as session:
        jobs = session.scalars(select(Job)).all()

    # The restore action already has an approved ReviewItem bound to its exact
    # bytes.  Starting a browser autosave session must therefore fork once;
    # subsequent keystrokes may mutate that unreviewed successor in place.
    assert autosaved.version_id != restored.version_id
    assert jobs == []


def test_metadata_only_update_keeps_restore_draft_unpublished():
    # Draft metadata is version-scoped: the canonical page fields and published
    # name projection remain unchanged while the editor projection gains the
    # pending title.
    service = make_service()
    created, updated, restored = _seed_restored_draft(service)

    patched = service.update_page(
        page_id=created.id,
        title="Restore Meta Renamed",
        body_md=restored.body_md,
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=restored.version_id,
        base_content_hash=restored.content_hash,
        actor_provenance="browser_session",
    )

    with service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        draft = session.get(ContextPageVersion, restored.version_id)
        current_version_id = page.current_version_id
        published_version_id = page.current_published_version_id
        draft_status = draft.version_status
        canonical_title = page.title
        index_rows = session.scalars(
            select(PageNameIndex)
            .where(PageNameIndex.page_id == page.id)
            .order_by(PageNameIndex.visibility_scope, PageNameIndex.name)
        ).all()
        metadata_operations = session.scalars(
            select(ChangeOperation)
            .where(ChangeOperation.target_id == page.id)
            .where(ChangeOperation.operation == "metadata_patch")
        ).all()

    assert patched.title == "Restore Meta Renamed"
    assert patched.version_id == restored.version_id
    assert current_version_id == restored.version_id
    assert published_version_id == updated.version_id
    assert draft_status == "draft"
    assert canonical_title == "Restore Meta"
    assert {
        (row.visibility_scope, row.name, row.source_version_id) for row in index_rows
    } == {
        ("published", "restore-meta", updated.version_id),
        ("published", "Restore Meta", updated.version_id),
        ("draft", "restore-meta", restored.version_id),
        ("draft", "Restore Meta", restored.version_id),
        ("draft", "Restore Meta Renamed", restored.version_id),
    }
    parsed_metadata_operations = [
        json.loads(operation.diff_summary) for operation in metadata_operations
    ]
    assert parsed_metadata_operations == [{"title": "Restore Meta Renamed"}]

    repeated = service.update_page(
        page_id=created.id,
        title="Restore Meta Renamed",
        body_md=restored.body_md,
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=restored.version_id,
        base_content_hash=restored.content_hash,
        actor_provenance="browser_session",
    )
    with service.session_factory() as session:
        operation_count = len(
            session.scalars(
                select(ChangeOperation)
                .where(ChangeOperation.target_id == created.id)
                .where(ChangeOperation.operation == "metadata_patch")
            ).all()
        )

    assert repeated.version_id == restored.version_id
    assert operation_count == 1


def test_pending_title_is_editor_only_until_restore_draft_is_published():
    service = make_service()
    created, updated, restored = _seed_restored_draft(service)
    service.update_page(
        page_id=created.id,
        title="Pending Restore Name",
        body_md=restored.body_md,
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=restored.version_id,
        base_content_hash=restored.content_hash,
        actor_provenance="browser_session",
    )

    with service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        workspace_id = page.workspace_id
        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer",
            workspace_role="viewer",
            role="viewer",
            workspace_id=workspace_id,
        )
        editor = ActorContext(
            actor_kind="human",
            actor_id="editor",
            workspace_role="editor",
            role="editor",
            workspace_id=workspace_id,
        )
        viewer_results = SearchService().search(
            session=session,
            workspace_id=workspace_id,
            query="Pending Restore Name",
            scope="context_page",
            actor=viewer,
        )
        editor_results = SearchService().search(
            session=session,
            workspace_id=workspace_id,
            query="Pending Restore Name",
            scope="context_page",
            actor=editor,
        )

    assert viewer_results.ranked_results == []
    [editor_hit] = editor_results.ranked_results
    assert editor_hit.id == created.id
    assert editor_hit.version_id == restored.version_id
    assert editor_hit.matched_name == "Pending Restore Name"
    assert editor_hit.title == "Restore Meta"

    with service.session_factory.begin() as session:
        node_id = session.scalar(
            select(WorkspaceTreeNode.id).where(
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == created.id,
            )
        )
        TreeService().update_node(
            session,
            workspace_id,
            node_id,
            TreeNodeUpdate(display_name="Immediate Published Rename"),
            actor_kind="human",
            actor_id="editor",
        )
    with service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        renamed_rows = session.scalars(
            select(PageNameIndex).where(PageNameIndex.page_id == created.id)
        ).all()

    assert page.title == "Immediate Published Rename"
    assert {
        row.visibility_scope
        for row in renamed_rows
        if row.name == "Immediate Published Rename"
    } == {"published", "draft"}
    assert any(
        row.visibility_scope == "draft" and row.name == "Pending Restore Name"
        for row in renamed_rows
    )
    assert not any(
        row.visibility_scope == "published" and row.name == "Pending Restore Name"
        for row in renamed_rows
    )

    with service.session_factory.begin() as session:
        ContextWriteService().publish_draft(
            session=session,
            workspace_id=workspace_id,
            page_id=created.id,
            request=PublishContextPageRequest(
                version_id=restored.version_id,
                reason="publish restored draft",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
        )

    with service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        published_rows = session.scalars(
            select(PageNameIndex).where(PageNameIndex.page_id == created.id)
        ).all()
        viewer_results = SearchService().search(
            session=session,
            workspace_id=workspace_id,
            query="Pending Restore Name",
            scope="context_page",
            actor=viewer,
        )

    assert page.title == "Pending Restore Name"
    assert page.current_published_version_id == restored.version_id
    assert {row.visibility_scope for row in published_rows} == {"published"}
    [viewer_hit] = viewer_results.ranked_results
    assert viewer_hit.title == "Pending Restore Name"
    assert viewer_hit.version_id == restored.version_id


def test_autosave_from_reviewed_restore_keeps_both_drafts_unpublished():
    # A restore action is an immutable reviewed snapshot.  The browser forks a
    # mutable successor without advancing the published head.
    service = make_service()
    created, updated, restored = _seed_restored_draft(service)

    saved = service.autosave_page(
        page_id=created.id,
        workspace_id=created.workspace_id,
        title=None,
        body_md="# Restore Meta\n\nv1 autosaved",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=restored.version_id,
        base_content_hash=restored.content_hash,
        session_version_id=restored.version_id,
        actor_provenance="browser_session",
        idempotency_key="restore-meta-v1-autosaved",
    )

    with service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        restored_draft = session.get(ContextPageVersion, restored.version_id)
        saved_draft = session.get(ContextPageVersion, saved.version_id)
        published_version_id = page.current_published_version_id
        latest_draft_version_id = page.latest_draft_version_id

    assert saved.version_id != restored.version_id
    assert published_version_id == updated.version_id
    assert latest_draft_version_id == saved.version_id
    assert restored_draft.version_status == "draft"
    assert saved_draft.version_status == "draft"
    assert restored_draft.body_md == restored.body_md
    assert "v1 autosaved" in saved_draft.body_md


def test_autosave_stages_frontmatter_alias_for_draft_scope_only():
    service = make_service()
    created, updated, restored = _seed_restored_draft(service)

    saved = service.autosave_page(
        page_id=created.id,
        workspace_id=created.workspace_id,
        title=None,
        body_md=restored.body_md,
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=restored.version_id,
        base_content_hash=restored.content_hash,
        session_version_id=restored.version_id,
        aliases=["Pending Restore Alias"],
        actor_provenance="browser_session",
        idempotency_key="pending-restore-alias-autosave",
    )

    with service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        rows = session.scalars(
            select(PageNameIndex).where(PageNameIndex.page_id == created.id)
        ).all()
        patch = session.scalar(
            select(ChangeOperation)
            .where(ChangeOperation.target_id == created.id)
            .where(ChangeOperation.operation == "metadata_patch")
            .order_by(ChangeOperation.created_at.desc(), ChangeOperation.id.desc())
        )

    assert saved.version_id != restored.version_id
    assert page.aliases == []
    assert json.loads(patch.diff_summary) == {"aliases": ["Pending Restore Alias"]}
    assert any(
        row.visibility_scope == "draft" and row.name == "Pending Restore Alias"
        for row in rows
    )
    assert not any(
        row.visibility_scope == "published" and row.name == "Pending Restore Alias"
        for row in rows
    )
    assert any(
        row.visibility_scope == "published"
        and row.source_version_id == updated.version_id
        for row in rows
    )


def test_agent_authenticated_legacy_write_is_draft_pending_review():
    service = make_service()
    created = service.write_page(
        workspace_slug="default",
        slug="agent-draft",
        title="Agent draft",
        body_md="# Agent draft",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="personal_access_token",
    )
    with service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        version = session.get(ContextPageVersion, created.version_id)
        review = session.scalar(
            select(ReviewItem)
            .join(ChangeSet)
            .where(ChangeSet.id == version.change_set_id)
        )
        if review is None:
            review = session.scalar(select(ReviewItem))
        assert version.version_status == "draft"
        assert page.current_published_version_id is None
        assert page.latest_draft_version_id == version.id
        assert review.status == "pending"


def test_browser_legacy_write_still_publishes_directly():
    service = make_service()
    created = service.write_page(
        workspace_slug="default",
        slug="human-published",
        title="Human published",
        body_md="# Human published",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    with service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        version = session.get(ContextPageVersion, created.version_id)
        review = session.scalar(select(ReviewItem))
        assert version.version_status == "published_snapshot"
        assert page.current_published_version_id == version.id
        assert review.status == "approved"
