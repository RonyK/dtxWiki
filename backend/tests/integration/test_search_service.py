import logging
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.base import Base, utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
    PageNameIndex,
)
from backend.models.feedback import FeedbackItem
from backend.models.review import ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.policy.auth import ActorContext
from backend.schemas.read import RankedResult
from backend.schemas.tree import TreeNodeUpdate
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.extraction_service import ExtractionService
from backend.services.indexing.fts5 import sync_agent_page_doc_sources
from backend.services.library_service import LibraryService
from backend.services.page_service import PageService, sync_page_projections
from backend.services.read_errors import UnsupportedRetrievalModeError
from backend.services.search_result_metadata import enrich_search_result_metadata
from backend.services.search_service import SearchService, ranked_result_types
from backend.services.tree_service import TreeService
from backend.utils.content_hash import content_hash as calculate_content_hash


def make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def audit_actor(workspace_id, *, scopes=("audit-read",)) -> ActorContext:
    return ActorContext(
        actor_kind="mcp_client",
        actor_id="search-audit-test",
        scopes=scopes,
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
        token_kind="mcp_client",
    )


def add_agent_page(
    session,
    *,
    workspace_id,
    title: str,
    slug: str,
    body: str,
    category: str = "agent_guide",
    active: bool = False,
) -> AgentPage:
    page = AgentPage(
        workspace_id=workspace_id,
        category=category,
        title=title,
        slug=slug,
        lifecycle_status="active" if active else "draft",
    )
    session.add(page)
    session.flush()
    version = AgentPageVersion(
        workspace_id=page.workspace_id,
        agent_page_id=page.id,
        version_no=1,
        body_markdown=body,
        actor_type="human",
        version_status="active_snapshot" if active else "draft",
    )
    session.add(version)
    session.flush()
    if active:
        page.current_active_version_id = version.id
    else:
        page.latest_draft_version_id = version.id
    sync_agent_page_doc_sources(session, page)
    return page


def test_search_service_returns_keyword_results_across_read_domains(
    media_installer_factory,
):
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
    PageService(session_factory).write_page(
        workspace_slug="default",
        slug="search-context",
        title="Search Context",
        body_md="# Search Context\n\nNeedle context body",
        aliases=["Needle Shortcut"],
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/search-source",
            title="Search Source",
            raw_text="# Search Source\n\nNeedle artifact body",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        session.add(
            FeedbackItem(
                workspace_id=workspace.id,
                target_type="library_item",
                target_id=registration.library_item.id,
                body="Needle feedback body",
            )
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle",
            scope="all",
            actor=audit_actor(workspace.id),
        )
        feedback_results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle",
            scope="feedback",
            actor=audit_actor(workspace.id),
        )

    assert ranked_result_types(results.ranked_results) >= {
        "context_page",
        "library_artifact",
    }
    assert ranked_result_types(results.ranked_results).isdisjoint(
        {"feedback", "change_set"}
    )
    assert results.supported_retrieval_modes == ["keyword"]
    assert results.ranked_results[0].score >= results.ranked_results[-1].score
    feedback_hit = next(
        result
        for result in feedback_results.ranked_results
        if result.type == "feedback"
    )
    assert feedback_hit.source_ref == {
        "kind": "library_item",
        "id": str(registration.library_item.id),
    }

    with session_factory.begin() as session:
        alias_results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="shortcut",
            scope="context_page",
        )

    assert [result.title for result in alias_results.ranked_results] == [
        "Search Context"
    ]


def test_search_service_change_set_links_review_item_when_available():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        # LEAK-02: change_set_visible now checks the operation's target, so the
        # target must be a real, actor-visible page (not a dangling uuid4()) or
        # the change set is correctly filtered out before this test's assertion.
        page = ContextPage(
            workspace_id=workspace.id,
            slug="needle-review-target",
            title="Needle Review Target",
            page_type="guide",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="Needle review target body.",
            content_hash="needle-review-target-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="user-1",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_version_id = version.id
        page.current_published_version_id = version.id
        sync_page_projections(session, page=page, version=version)

        change_set = ChangeSet(
            workspace_id=workspace.id,
            reason="Needle review workflow",
            trigger_type="context_page_publish",
            actor_type="human",
            status="review_required",
            risk_level="medium",
        )
        session.add(change_set)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="context_page",
                target_id=page.id,
                operation="publish",
                diff_summary="Needle diff",
            )
        )
        review = ReviewItem(
            workspace_id=workspace.id,
            change_set_id=change_set.id,
            review_type="context_page_publish",
            status="pending",
            risk_level="medium",
        )
        session.add(review)
        session.flush()
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle",
            scope="change",
            actor=ActorContext(
                actor_kind="system",
                actor_id="search-test",
                scopes=("*",),
                workspace_id=workspace.id,
                workspace_role="owner",
                role="owner",
            ),
        )
        all_results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle",
            scope="all",
            actor=audit_actor(workspace.id),
        )

    [hit] = results.ranked_results
    assert hit.type == "change_set"
    assert hit.source_ref == {"kind": "review_item", "id": str(review.id)}
    assert "change_set" not in ranked_result_types(all_results.ranked_results)


def test_search_service_change_lane_hides_draft_version_autosave_from_viewer():
    # LEAK-02: actor_can_read_audit_records is a capability gate ONLY -- a
    # browser viewer SESSION passes it (token_kind == "session" short-circuits
    # the scope check) and used to see every ChangeSet's reason/diff_summary
    # in the workspace with no target filter at all. The representative
    # payload is an autosave metadata_patch on a page that already has a
    # visible PUBLISHED head (page-visibility alone is not enough), whose
    # diff_ref names the still-unpublished DRAFT version.
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = ContextPage(
            workspace_id=workspace.id,
            slug="leak02-search-target",
            title="Leak02 Search Target",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        published_version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="# Published\n\nVisible body.",
            content_hash="leak02-search-pub",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editor-user",
            version_status="published_snapshot",
        )
        session.add(published_version)
        session.flush()
        page.current_published_version_id = published_version.id
        page.current_version_id = published_version.id

        draft_version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=2,
            body_md="# Draft\n\nSecret upcoming body.",
            content_hash="leak02-search-draft",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editor-user",
            version_status="draft",
            parent_version_id=published_version.id,
        )
        session.add(draft_version)
        session.flush()
        page.latest_draft_version_id = draft_version.id
        page.current_version_id = draft_version.id

        change_set = ChangeSet(
            workspace_id=workspace.id,
            reason="page.autosave.draft",
            trigger_type="page_autosave",
            actor_type="human",
            status="applied",
            risk_level="low",
        )
        session.add(change_set)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="context_page",
                target_id=page.id,
                operation="metadata_patch",
                diff_summary="title -> Zephyr Acquisition Draft Title",
                diff_ref=f"context_page_version:{draft_version.id}",
            )
        )
        workspace_id = workspace.id

    viewer_session = ActorContext(
        actor_kind="human",
        actor_id="viewer-user",
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
    )
    owner = ActorContext(
        actor_kind="system",
        actor_id="owner-test",
        scopes=("*",),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
    )

    with session_factory.begin() as session:
        viewer_results = search_service.search(
            session=session,
            workspace_id=workspace_id,
            query="Zephyr",
            scope="change",
            actor=viewer_session,
        )
        owner_results = search_service.search(
            session=session,
            workspace_id=workspace_id,
            query="Zephyr",
            scope="change",
            actor=owner,
        )

    assert viewer_results.ranked_results == []
    assert len(owner_results.ranked_results) == 1
    # The confidential text must not appear ANYWHERE in the viewer's payload,
    # not just be absent as a top-level hit.
    for hit in viewer_results.ranked_results:
        assert "Zephyr" not in (hit.title or "")
        assert "Zephyr" not in (hit.snippet or "")


def test_search_service_change_lane_scan_does_not_starve_visible_hit():
    # LEAK-02 batch-scan regression: change_set_visible must run per-row, so a
    # naive "SELECT ... LIMIT n, then filter" would let a run of hidden rows
    # (here: sensitive AgentPage operations) starve out a visible hit that
    # sorts after them. _search_changes scans in bounded batches instead
    # (mirrors _search_fts's guard against the same failure mode).
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        sensitive_agent = AgentPage(
            workspace_id=workspace.id,
            category="memory",
            title="Sensitive Memory",
            slug="leak02-sensitive-memory",
            sensitivity="private",
            lifecycle_status="active",
        )
        session.add(sensitive_agent)
        session.flush()
        sensitive_version = AgentPageVersion(
            workspace_id=sensitive_agent.workspace_id,
            agent_page_id=sensitive_agent.id,
            version_no=1,
            body_markdown="# Sensitive",
            frontmatter={},
            actor_type="human",
            version_status="active_snapshot",
        )
        session.add(sensitive_version)
        session.flush()
        sensitive_agent.current_active_version_id = sensitive_version.id

        # 60 newest change sets are all hidden from the viewer (operation
        # targets the sensitive AgentPage). created_at ties break on id in
        # _search_changes's ORDER BY, so an explicit descending timestamp
        # keeps these unambiguously "newest".
        for index in range(60):
            hidden = ChangeSet(
                workspace_id=workspace.id,
                reason="starve needle hidden",
                trigger_type="agent_page_update",
                actor_type="human",
                status="applied",
                risk_level="low",
                created_at=utc_now() - timedelta(seconds=index),
            )
            session.add(hidden)
            session.flush()
            session.add(
                ChangeOperation(
                    workspace_id=hidden.workspace_id,
                    change_set_id=hidden.id,
                    target_type="agent_page",
                    target_id=sensitive_agent.id,
                    operation="update",
                    diff_summary="starve needle hidden",
                )
            )

        # One visible change set, older than all the hidden ones, on an
        # ordinary published page.
        page = ContextPage(
            workspace_id=workspace.id,
            slug="leak02-starve-visible",
            title="Leak02 Starve Visible",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="# Visible\n\nBody.",
            content_hash="leak02-starve-visible",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="editor-user",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_published_version_id = version.id
        page.current_version_id = version.id
        visible_change_set = ChangeSet(
            workspace_id=workspace.id,
            reason="starve needle visible",
            trigger_type="context_page_publish",
            actor_type="human",
            status="applied",
            risk_level="low",
            created_at=utc_now() - timedelta(seconds=120),
        )
        session.add(visible_change_set)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=visible_change_set.workspace_id,
                change_set_id=visible_change_set.id,
                target_type="context_page",
                target_id=page.id,
                operation="publish",
                diff_summary="starve needle visible",
            )
        )
        workspace_id = workspace.id
        visible_change_set_id = visible_change_set.id

    viewer_session = ActorContext(
        actor_kind="human",
        actor_id="viewer-user",
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
    )

    with session_factory.begin() as session:
        results = search_service.search(
            session=session,
            workspace_id=workspace_id,
            query="starve needle",
            scope="change",
            limit=10,
            actor=viewer_session,
        )

    assert visible_change_set_id in {hit.id for hit in results.ranked_results}


def test_search_service_change_lane_requires_audit_visibility():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        session.add(
            ChangeSet(
                workspace_id=workspace.id,
                reason="private governance needle",
                trigger_type="context_page_publish",
                actor_type="human",
                status="review_required",
                risk_level="medium",
            )
        )
        viewer = ActorContext(
            actor_kind="human",
            actor_id=str(uuid4()),
            scopes=("mcp:read",),
            workspace_id=workspace.id,
            workspace_role="viewer",
            role="viewer",
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="governance needle",
            scope="change",
            actor=viewer,
        )

    assert results.ranked_results == []


def test_feedback_search_filters_before_limit():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        old_match = FeedbackItem(
            workspace_id=workspace.id,
            target_type="context_page",
            target_id=uuid4(),
            body="ancient matching needle",
            created_at=utc_now() - timedelta(days=2),
        )
        session.add(old_match)
        session.add_all(
            [
                FeedbackItem(
                    workspace_id=workspace.id,
                    target_type="context_page",
                    target_id=uuid4(),
                    body=f"recent unrelated feedback {index}",
                    created_at=utc_now() - timedelta(minutes=index),
                )
                for index in range(12)
            ]
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="matching needle",
            scope="feedback",
            limit=1,
            actor=audit_actor(workspace.id),
        )

    assert [result.id for result in results.ranked_results] == [old_match.id]


def test_feedback_search_requires_audit_visibility():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        feedback = FeedbackItem(
            workspace_id=workspace.id,
            target_type="context_page",
            target_id=uuid4(),
            body="private feedback visibility needle",
        )
        session.add(feedback)
        session.flush()

        anonymous = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="visibility needle",
            scope="feedback",
        )
        read_only = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="visibility needle",
            scope="feedback",
            actor=audit_actor(workspace.id, scopes=("mcp:read",)),
        )
        auditor = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="visibility needle",
            scope="feedback",
            actor=audit_actor(workspace.id),
        )

    assert anonymous.ranked_results == []
    assert read_only.ranked_results == []
    assert [result.id for result in auditor.ranked_results] == [feedback.id]


def test_change_search_filters_before_limit():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        # LEAK-02: change_set_visible fails CLOSED for a change set with no
        # operations at all (its own "readable only if EVERY operation is
        # readable" contract must not default to True for an empty set), so
        # every change set under test here needs a real, actor-visible
        # target -- not a dangling uuid4(). One shared visible page is
        # enough; this test is about keyword-match/limit ordering, not
        # per-target uniqueness.
        page = ContextPage(
            workspace_id=workspace.id,
            slug="change-search-limit-target",
            title="Change Search Limit Target",
            page_type="guide",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="Change search limit target body.",
            content_hash="change-search-limit-target-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="user-1",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_version_id = version.id
        page.current_published_version_id = version.id

        def _operation(change_set_id):
            return ChangeOperation(
                workspace_id=workspace.id,
                change_set_id=change_set_id,
                target_type="context_page",
                target_id=page.id,
                operation="update",
                diff_summary="",
            )

        old_match = ChangeSet(
            workspace_id=workspace.id,
            reason="ancient matching governance",
            trigger_type="context_page_publish",
            actor_type="human",
            status="review_required",
            risk_level="medium",
            created_at=utc_now() - timedelta(days=2),
        )
        session.add(old_match)
        session.flush()
        session.add(_operation(old_match.id))
        unrelated = [
            ChangeSet(
                workspace_id=workspace.id,
                reason=f"recent unrelated change {index}",
                trigger_type="context_page_publish",
                actor_type="human",
                status="review_required",
                risk_level="medium",
                created_at=utc_now() - timedelta(minutes=index),
            )
            for index in range(12)
        ]
        session.add_all(unrelated)
        session.flush()
        session.add_all(_operation(change_set.id) for change_set in unrelated)
        actor = ActorContext(
            actor_kind="system",
            actor_id="search-test",
            scopes=("*",),
            workspace_id=workspace.id,
            workspace_role="owner",
            role="owner",
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="matching governance",
            scope="change",
            actor=actor,
            limit=1,
        )

    assert [result.id for result in results.ranked_results] == [old_match.id]


def test_search_service_feedback_links_context_citation_parent_page():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
    created = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="citation-parent",
        title="Citation Parent",
        body_md="# Citation Parent\n\nNeedle source.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = session.get(ContextPage, created.id)
        citation = ContextCitation(
            workspace_id=workspace.id,
            target_kind="context_page_whole",
            target_id=created.version_id,
            target_location={},
            cited_context_page_version_id=created.version_id,
            location={},
            citation_type="context_page",
            verification_status="strong",
        )
        session.add(citation)
        session.flush()
        session.add(
            FeedbackItem(
                workspace_id=workspace.id,
                target_type="context_citation",
                target_id=citation.id,
                body="Needle citation feedback",
            )
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle citation",
            scope="feedback",
            actor=audit_actor(workspace.id),
        )

    [hit] = results.ranked_results
    assert hit.type == "feedback"
    assert hit.source_ref == {"kind": "context_page", "id": str(page.id)}


def test_search_service_feedback_links_context_claim_citation_parent_page():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/citation-source",
            title="Citation Source",
            raw_text="# Citation Source\n\nLibrary evidence.",
        )
    created = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="claim-citation-parent",
        title="Claim Citation Parent",
        body_md="# Claim Citation Parent\n\nClaim target.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = session.get(ContextPage, created.id)
        claim = ContextClaim(
            workspace_id=workspace.id,
            page_version_id=created.version_id,
            claim_text="Claim citation target",
            location_in_page={"kind": "whole_page"},
            claim_type="summary",
            confidence="unknown",
            status="active",
            created_by_actor_type="human",
        )
        session.add(claim)
        session.flush()
        citation = ContextCitation(
            workspace_id=workspace.id,
            target_kind="context_claim",
            target_id=claim.id,
            target_location={},
            library_item_version_id=registration.version.id,
            location={},
            citation_type="raw_source",
            verification_status="strong",
        )
        session.add(citation)
        session.flush()
        session.add(
            FeedbackItem(
                workspace_id=workspace.id,
                target_type="context_citation",
                target_id=citation.id,
                body="Needle claim citation feedback",
            )
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle claim citation",
            scope="feedback",
            actor=audit_actor(workspace.id),
        )

    [hit] = results.ranked_results
    assert hit.type == "feedback"
    assert hit.source_ref == {"kind": "context_page", "id": str(page.id)}


def test_search_service_claim_search_keeps_published_claim_with_pending_draft():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        seed_default_workspace(session)
    created = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="claim-version-parent",
        title="Claim Version Parent",
        body_md="# Claim Version Parent\n\nPublished claim body.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = session.get(ContextPage, created.id)
        published_claim = ContextClaim(
            workspace_id=workspace.id,
            page_version_id=created.version_id,
            claim_text="PublishedOnlyNeedle claim",
            location_in_page={"kind": "whole_page"},
            claim_type="summary",
            confidence="unknown",
            status="active",
            created_by_actor_type="human",
        )
        draft_body = "# Claim Version Parent\n\nDraft body without the needle."
        draft_version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            parent_version_id=created.version_id,
            version_no=2,
            body_md=draft_body,
            content_hash=calculate_content_hash(draft_body),
            graph_status="linear",
            actor_type="human",
            actor_id=uuid4(),
            created_by_actor_kind="human",
            created_by_actor_id="00000000-0000-0000-0000-000000000010",
            version_status="draft",
        )
        session.add_all([published_claim, draft_version])
        session.flush()
        page.latest_draft_version_id = draft_version.id
        sync_page_projections(session, page=page, version=draft_version)
        editor = ActorContext(
            actor_kind="human",
            actor_id="editor",
            workspace_role="editor",
            role="editor",
            workspace_id=workspace.id,
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="publishedonlyneedle",
            scope="context_claim",
            actor=editor,
        )

    [hit] = results.ranked_results
    assert hit.type == "context_claim"
    assert hit.id == published_claim.id
    assert hit.version_id == created.version_id
    assert hit.source_ref == {"kind": "context_page", "id": str(created.id)}


def test_keyword_search_uses_shared_visibility_for_pending_drafts():
    """KW-1: the keyword lane must apply the same visibility as the entity
    lane, grep, and claim search — a draft-capable actor matches the pending
    draft head AND the published head; a viewer matches published only. A
    published decoy hit must not mask the draft-head match."""

    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        seed_default_workspace(session)
    page_service = PageService(session_factory)
    drafted = page_service.write_page(
        workspace_slug="default",
        slug="draft-needle-page",
        title="Draft Needle Page",
        body_md="# Draft Needle Page\n\nLegacyNeedle published body.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    published = page_service.write_page(
        workspace_slug="default",
        slug="published-needle-page",
        title="Published Needle Page",
        body_md="# Published Needle Page\n\nSharedNeedle published body.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = session.get(ContextPage, drafted.id)
        draft_body = "# Draft Needle Page\n\nSharedNeedle pending draft body."
        draft_version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            parent_version_id=drafted.version_id,
            version_no=2,
            body_md=draft_body,
            content_hash=calculate_content_hash(draft_body),
            graph_status="linear",
            actor_type="human",
            actor_id=uuid4(),
            created_by_actor_kind="human",
            created_by_actor_id="00000000-0000-0000-0000-000000000010",
            version_status="draft",
        )
        session.add(draft_version)
        session.flush()
        page.latest_draft_version_id = draft_version.id
        sync_page_projections(session, page=page, version=draft_version)
        draft_version_id = draft_version.id
        editor = ActorContext(
            actor_kind="human",
            actor_id="editor",
            workspace_role="editor",
            role="editor",
            workspace_id=workspace.id,
        )
        viewer = ActorContext(
            actor_kind="human",
            actor_id="viewer",
            workspace_role="viewer",
            role="viewer",
            workspace_id=workspace.id,
        )

        editor_results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="sharedneedle",
            scope="context_page",
            actor=editor,
        )
        viewer_results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="legacyneedle",
            scope="context_page",
            actor=viewer,
        )
        editor_union = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="legacyneedle",
            scope="context_page",
            actor=editor,
        )

    editor_hits = {hit.id: hit for hit in editor_results.ranked_results}
    assert set(editor_hits) == {drafted.id, published.id}
    assert editor_hits[drafted.id].version_id == draft_version_id
    assert editor_hits[published.id].version_id == published.version_id

    # The published body of the drafted page stays searchable for both roles
    # while the draft is pending (union semantics; viewer never saw the draft).
    [viewer_hit] = viewer_results.ranked_results
    assert viewer_hit.id == drafted.id
    assert viewer_hit.version_id == drafted.version_id
    [union_hit] = editor_union.ranked_results
    assert union_hit.id == drafted.id
    assert union_hit.version_id == drafted.version_id


def test_name_index_resyncs_on_rename_and_title_only_update():
    """BS-1/BS-2: a tree rename and a body-unchanged title-only edit both
    mutate the canonical page title without writing a new version; the name
    projection must resync so the entity lane resolves the new title.

    P2.3: a retired title is no longer auto-preserved as an alias -- it is
    reclaimed immediately, so each old name norm must stop resolving."""

    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    page_service = PageService(session_factory)
    created = page_service.write_page(
        workspace_slug="default",
        slug="rename-target",
        title="Rename Target",
        body_md="# Stable Heading\n\nStable body.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    def name_norms() -> set[str]:
        with session_factory() as session:
            return set(
                session.scalars(
                    select(PageNameIndex.name_norm).where(
                        PageNameIndex.page_id == created.id
                    )
                )
            )

    def exact_title_hit(query: str) -> bool:
        with session_factory() as session:
            results = search_service.search(
                session=session,
                workspace_id=workspace_id,
                query=query,
                scope="context_page",
            )
        return any(
            hit.match_reason == "exact_title" and hit.id == created.id
            for hit in results.ranked_results
        )

    assert "rename target" in name_norms()

    # BS-1: tree rename mutates page.title without a new version.
    with session_factory.begin() as session:
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
            TreeNodeUpdate(display_name="Fresh Tree Title"),
            actor_kind="human",
            actor_id="tester",
        )
    norms = name_norms()
    assert "fresh tree title" in norms
    assert "rename target" not in norms
    assert exact_title_hit("Fresh Tree Title")
    assert not exact_title_hit("Rename Target")

    # BS-2: title-only PATCH with an unchanged body (same-hash branch).
    updated = page_service.update_page(
        page_id=created.id,
        title="Patched Title",
        body_md=created.body_md,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )
    norms = name_norms()
    assert "patched title" in norms
    assert "fresh tree title" not in norms
    assert exact_title_hit("Patched Title")

    # BS-2: a title-only autosave stages the rename instead of publishing it.
    # Autosave no longer moves the published pointer, so the staged title is
    # indexed for actors who can see drafts but must not resolve the page for a
    # reader until the author publishes.
    autosaved = page_service.autosave_page(
        page_id=created.id,
        workspace_id=created.workspace_id,
        title="Autosaved Title",
        body_md=updated.body_md,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        base_version_id=updated.version_id,
        base_content_hash=updated.content_hash,
        actor_provenance="browser_session",
        idempotency_key="autosaved-title",
    )
    norms = name_norms()
    assert "autosaved title" in norms
    assert "patched title" in norms
    assert not exact_title_hit("Autosaved Title")
    assert exact_title_hit("Patched Title")

    page_service.publish_draft(
        page_id=created.id,
        version_id=autosaved.version_id,
        reason="editor Save",
        publish_despite_findings=False,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    norms = name_norms()
    assert "autosaved title" in norms
    assert "patched title" not in norms
    assert exact_title_hit("Autosaved Title")


def test_search_service_artifact_title_uses_parent_item_title(media_installer_factory):
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/titled-source",
            title="Quarterly Report",
            raw_text="# Quarterly Report\n\nNeedle artifact body",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle",
            scope="library_artifact",
        )

    titles = [result.title for result in results.ranked_results]
    assert titles == ["Quarterly Report"]
    # The raw artifact_type must never surface as the result title.
    assert "extracted_text" not in titles


def test_search_service_artifact_title_falls_back_to_humanized_type(
    media_installer_factory,
):
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/untitled-source",
            title=None,
            raw_text="# Untitled\n\nNeedle artifact body",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle",
            scope="library_artifact",
        )

    titles = [result.title for result in results.ranked_results]
    # A-1: artifact_type is retired; the fallback title is now the humanized
    # `kind` axis instead (domain_read.py / retrieval/keyword.py). This
    # extraction-service artifact's media_type is text/plain, which
    # _artifact_kind_for_media_type maps to kind="text" -> "Text".
    assert titles == ["Text"]
    assert "extracted_text" not in titles


def test_search_service_filters_sources_by_connector():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/web-source",
            title="Filtered Source",
            connector_type="web",
            raw_text="filtered body",
        )
        LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="file:///manual-source.md",
            title="Filtered Source",
            connector_type="manual_upload",
            raw_text="filtered body",
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="filtered",
            scope="library_item",
            filters={"connector_type": ["manual_upload"]},
        )

    assert [result.title for result in results.ranked_results] == ["Filtered Source"]
    assert len(results.ranked_results) == 1


def test_search_service_excludes_restricted_library_items_and_artifacts(
    media_installer_factory,
):
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/restricted-library",
            title="Restricted Source",
            trust_level="restricted",
            raw_text="# Restricted Source\n\nNeedle restricted artifact body",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle restricted",
            scope="all",
        )

    assert ranked_result_types(results.ranked_results).isdisjoint(
        {"library_item", "library_artifact"}
    )
    assert (
        results.permission_note
        == "Restricted Library items and artifacts were excluded."
    )
    assert "permission_limited" in results.warnings


def test_search_service_can_include_restricted_library_items_for_authorized_actor(
    media_installer_factory,
):
    session_factory = make_session_factory()
    search_service = SearchService()
    owner = ActorContext(
        actor_kind="human",
        actor_id="owner",
        workspace_role="owner",
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/restricted-library",
            title="Restricted Source",
            trust_level="restricted",
            raw_text="# Restricted Source\n\nNeedle restricted artifact body",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="restricted",
            scope="all",
            actor=owner,
        )

    assert ranked_result_types(results.ranked_results) >= {
        "library_item",
        "library_artifact",
    }
    assert results.permission_note is None
    assert "permission_limited" not in results.warnings


def test_search_service_excludes_archived_sources_and_artifacts(
    media_installer_factory,
):
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/archived-source",
            title="Archived Source",
            trust_level="trusted",
            raw_text="# Archived Source\n\nNeedle archived artifact body",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        registration.library_item.lifecycle_status = "archived"
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle archived",
            scope="all",
        )

    assert ranked_result_types(results.ranked_results).isdisjoint(
        {"library_item", "library_artifact"}
    )


def test_search_service_agent_page_snippet_omits_title_and_slug():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        # A-5 defensive scope: an anonymous/viewer-grade search (actor=None)
        # matches ACTIVE snapshots only — seed an activated non-sensitive
        # page so the snippet-shape pins keep exercising the visible path.
        add_agent_page(
            session,
            workspace_id=workspace.id,
            title="Release Checklist Prompt",
            slug="release-checklist-prompt",
            body="Needle agent body guidance",
            category="skill",
            active=True,
        )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle",
            scope="agent_page",
        )

    assert [result.title for result in results.ranked_results] == [
        "Release Checklist Prompt"
    ]
    snippet = results.ranked_results[0].snippet
    assert "Needle agent body guidance" in snippet
    assert "release-checklist-prompt" not in snippet
    assert "Release Checklist Prompt" not in snippet
    assert results.ranked_results[0].summary == "Needle agent body guidance"

    with session_factory.begin() as session:
        slug_results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="release-checklist-prompt",
            scope="agent_page",
        )

    assert [result.title for result in slug_results.ranked_results] == [
        "Release Checklist Prompt"
    ]
    assert slug_results.ranked_results[0].snippet == "Needle agent body guidance"

    with session_factory.begin() as session:
        seeded_results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="default system",
            scope="agent_page",
        )

    # the seeded Default System Prompt is agent_guide (sensitive) — like the
    # resources surface, only owners may find it (A-5 alignment)
    assert seeded_results.ranked_results == []


def test_search_service_context_page_snippet_omits_title_slug_and_frontmatter():
    # M-A: the context path matches on the full title/slug/aliases haystack, but
    # the snippet is sourced from the body window so it no longer echoes the
    # title/slug nor leaks leading YAML frontmatter (mirror of the agent-page
    # fix). body_md is stored raw with managed frontmatter injected on write.
    session_factory = make_session_factory()
    search_service = SearchService()

    body_md = (
        "---\n"
        "title: Onboarding Runbook\n"
        "slug: onboarding-runbook\n"
        "type: guide\n"
        "tags: []\n"
        "aliases: [Starter Shortcut]\n"
        "---\n"
        "\n"
        "Needle context body guidance"
    )
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = ContextPage(
            workspace_id=workspace.id,
            slug="onboarding-runbook",
            title="Onboarding Runbook",
            page_type="guide",
            aliases=["Starter Shortcut"],
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md=body_md,
            content_hash="onboarding-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="user-1",
        )
        session.add(version)
        session.flush()
        page.current_version_id = version.id
        # Publish the version so an anonymous (actor=None) search can see it;
        # the keyword draft predicate hides draft-only pages from non-draft
        # actors, and this test exercises snippet rendering, not draft gating.
        page.current_published_version_id = version.id
        sync_page_projections(session, page=page, version=version)

        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle",
            scope="context_page",
        )

    assert [result.title for result in results.ranked_results] == ["Onboarding Runbook"]
    snippet = results.ranked_results[0].snippet
    assert snippet == "Needle context body guidance"
    # The snippet opens on the body, not the title/slug or the YAML frontmatter.
    assert "Onboarding Runbook" not in snippet
    assert "onboarding-runbook" not in snippet
    assert "---" not in snippet
    assert "aliases" not in snippet

    # The match haystack still carries title/slug/aliases, so a title/alias-only
    # query still finds the page (matching is unchanged; only the snippet narrows).
    with session_factory.begin() as session:
        alias_results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="starter shortcut",
            scope="context_page",
        )

    assert [result.title for result in alias_results.ranked_results] == [
        "Onboarding Runbook"
    ]


def test_search_service_entity_lane_uses_page_name_index_and_warnings():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        seed_default_workspace(session)
    PageService(session_factory).write_page(
        workspace_slug="default",
        slug="entity-lane-runbook",
        title="Entity Lane Runbook",
        body_md="# Entity Lane Runbook\n\nBody without the alias phrase",
        aliases=["Symbolic Shortcut"],
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="Symbolic Shortcut",
            scope="context_page",
            filters={"tag": ["ignored"], "date_from": "2026-01-01T00:00:00Z"},
        )

    assert [result.title for result in results.ranked_results] == [
        "Entity Lane Runbook"
    ]
    assert results.ranked_results[0].match_reason == "exact_alias"
    assert results.ranked_results[0].matched_name == "Symbolic Shortcut"
    assert results.ranked_results[0].concept_path == "/context/entity-lane-runbook.md"
    # §6.2: a single exact hit short-circuits — no fallback lane runs.
    assert results.tried_lanes == ["entity"]
    assert results.raw_query == "Symbolic Shortcut"
    assert results.normalized_query == "symbolic shortcut"
    assert results.warnings == [
        "unsupported_filter_tag",
        "unsupported_filter_date",
        "permission_limited",
    ]

    # The raw query is echoed back exactly, padding included.
    with session_factory.begin() as session:
        padded = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="  Padded Query  ",
            scope="context_page",
        )
    assert padded.raw_query == "  Padded Query  "
    assert padded.normalized_query == "padded query"


def test_exact_entity_hit_short_circuits_fallback_retrieval():
    """Exact hits share the ranked list without invoking fallback retrieval;
    a miss still runs the requested retriever."""

    class CountingRetriever:
        def __init__(self):
            self.calls = 0

        def supported_modes(self):
            return {"keyword"}

        def retrieve(self, **kwargs):
            self.calls += 1
            return []

    counting = CountingRetriever()
    session_factory = make_session_factory()
    search_service = SearchService(retrievers=[counting])
    page_service = PageService(session_factory)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    page_service.write_page(
        workspace_slug="default",
        slug="short-circuit-target",
        title="Short Circuit Target",
        body_md="# Short Circuit Target\n\nBody.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    shared_title_ids = []
    for slug in ("ambiguous-a", "ambiguous-b"):
        page = page_service.write_page(
            workspace_slug="default",
            slug=slug,
            title="Ambiguous Name",
            body_md=f"# Ambiguous Name\n\n{slug} body.",
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
            actor_provenance="browser_session",
        )
        shared_title_ids.append(page.id)

    with session_factory() as session:
        exact = search_service.search(
            session=session,
            workspace_id=workspace_id,
            query="Short Circuit Target",
            scope="context_page",
        )
        assert counting.calls == 0
        assert exact.tried_lanes == ["entity"]

        ambiguous = search_service.search(
            session=session,
            workspace_id=workspace_id,
            query="Ambiguous Name",
            scope="context_page",
        )
        assert counting.calls == 0
        assert ambiguous.tried_lanes == ["entity"]
        assert [hit.id for hit in ambiguous.ranked_results] == sorted(
            shared_title_ids, key=str
        )
        assert all(
            hit.match_reason == "exact_title" for hit in ambiguous.ranked_results
        )
        assert ambiguous.total_count == 2
        assert ambiguous.warnings == exact.warnings

        miss = search_service.search(
            session=session,
            workspace_id=workspace_id,
            query="no such exact name",
            scope="context_page",
        )
        assert counting.calls == 1
        assert miss.tried_lanes == ["entity", "trigram", "keyword"]


def test_zero_hit_payload_includes_low_confidence_nearest_candidates(monkeypatch):
    class EmptyRetriever:
        def supported_modes(self):
            return {"keyword"}

        def retrieve(self, **_kwargs):
            return []

    session_factory = make_session_factory()
    service = SearchService(retrievers=[EmptyRetriever()])
    candidate_id = uuid4()

    def trigram_lane(**kwargs):
        if kwargs.get("min_similarity") != 0.15:
            return []
        return [
            RankedResult(
                id=candidate_id,
                type="context_page",
                title="Nearby concept",
                snippet="",
                score=0.2,
                citation_available=True,
                concept_path="/context/nearby-concept.md",
                match_reason="trigram",
            )
        ]

    monkeypatch.setattr(service, "_trigram_lane", trigram_lane)
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        result = service.search(
            session=session,
            workspace_id=workspace.id,
            query="nearby typo",
            scope="context_page",
        )

    assert result.ranked_results == []
    assert result.nearest_candidates == [
        {
            "id": str(candidate_id),
            "kind": "context_page",
            "title": "Nearby concept",
            "concept_path": "/context/nearby-concept.md",
            "match_reason": "trigram",
        }
    ]


def test_read_concept_deduplicates_same_page_candidates():
    """A page whose title and alias both normalize to the query must appear
    once in the ambiguous candidates (strongest kind wins)."""

    from backend.services.concept_resolution import resolve_context_concept

    session_factory = make_session_factory()
    page_service = PageService(session_factory)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    page_service.write_page(
        workspace_slug="default",
        slug="dup-kind-page",
        title="Shared Concept",
        body_md="# Shared Concept\n\nA body.",
        aliases=["Shared Concept"],
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    page_service.write_page(
        workspace_slug="default",
        slug="other-shared-page",
        title="Shared Concept",
        body_md="# Shared Concept\n\nB body.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    with session_factory() as session:
        result = resolve_context_concept(
            session,
            workspace_id,
            identifier="Shared Concept",
            actor=None,
        )

    assert result.status == "ambiguous"
    assert len(result.candidates) == 2
    kinds = {c.path: c.match_kind for c in result.candidates}
    # dup-kind-page matched by both title and alias — title (stronger) wins.
    assert kinds["/context/dup-kind-page.md"] == "title"


def test_search_service_entity_lane_refreshes_metadata_only_alias_change():
    session_factory = make_session_factory()
    search_service = SearchService()
    page_service = PageService(session_factory)
    body = "# Metadata Projection\n\nStable body"

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
    created = page_service.write_page(
        workspace_slug="default",
        slug="metadata-projection",
        title="Metadata Projection",
        body_md=body,
        aliases=["Old Shortcut"],
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    page_service.write_page(
        workspace_slug="default",
        slug="metadata-projection",
        title="Metadata Projection",
        body_md=body,
        aliases=["Fresh Shortcut"],
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    with session_factory() as session:
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="Fresh Shortcut",
            scope="context_page",
        )

    assert [result.title for result in results.ranked_results] == [
        "Metadata Projection"
    ]
    assert results.ranked_results[0].match_reason == "exact_alias"


def test_search_service_reports_total_count_and_has_more_beyond_limit():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        for index in range(25):
            add_agent_page(
                session,
                workspace_id=workspace.id,
                title=f"Needle Prompt {index:03d}",
                slug=f"needle-prompt-{index:03d}",
                body="Needle agent body",
            )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle",
            scope="agent_page",
            limit=20,
            # draft-grade sensitive-category seeds: owner-visible (A-5) —
            # this test pins pagination counting, not visibility
            actor=ActorContext(
                actor_kind="human", actor_id="owner", workspace_role="owner"
            ),
        )

    # ranked_results is truncated to limit, but total_count/has_more report the
    # full pre-truncation count so the count chip can read "20+".
    assert len(results.ranked_results) == 20
    assert results.total_count == 25
    assert results.has_more is True


def test_search_service_total_count_matches_when_not_truncated():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        for index in range(3):
            add_agent_page(
                session,
                workspace_id=workspace.id,
                title=f"Single Needle {index}",
                slug=f"single-needle-{index}",
                body="Needle body",
            )
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle",
            scope="agent_page",
            limit=20,
            actor=ActorContext(
                actor_kind="human", actor_id="owner", workspace_role="owner"
            ),
        )

    assert results.total_count == len(results.ranked_results)
    assert results.has_more is False


def test_search_result_carries_okf_concept_pointer():
    session_factory = make_session_factory()
    search_service = SearchService()

    with session_factory.begin() as session:
        seed_default_workspace(session)
    PageService(session_factory).write_page(
        workspace_slug="default",
        slug="pointer-page",
        title="Pointer Page",
        body_md="# Pointer Page\n\nNeedle pointer body",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        results = search_service.search(
            session=session,
            workspace_id=workspace.id,
            query="needle",
            scope="context_page",
        )

    hit = next(r for r in results.ranked_results if r.type == "context_page")
    assert hit.concept_path == "/context/pointer-page.md"
    assert hit.concept_type is not None
    assert hit.version_id is not None
    assert hit.source_ref == {
        "kind": "context_page_version",
        "id": str(hit.version_id),
    }


def test_search_service_rejects_unsupported_retrieval_mode():
    class _NoDatabase:
        def __getattr__(self, name):
            raise AssertionError(f"mode check must precede session use: {name}")

    with pytest.raises(UnsupportedRetrievalModeError):
        SearchService().search(
            session=_NoDatabase(),
            workspace_id=uuid4(),
            query="anything",
            retrieval_mode="semantic",
        )


def _metadata_page(session, workspace_id, *, slug, title, body, published=True):
    page = ContextPage(workspace_id=workspace_id, slug=slug, title=title)
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        body_md=body,
        content_hash=calculate_content_hash(body),
        actor_type="human",
        version_status="published_snapshot" if published else "draft",
        created_by_actor_kind="human",
        created_by_actor_id="author",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    if published:
        page.current_published_version_id = version.id
        page.lifecycle_status = "published"
    else:
        page.latest_draft_version_id = version.id
    session.flush()
    sync_page_projections(session, page=page, version=version)
    # Reproduce a row saved before the prose-only summary rule. Projection
    # sync deliberately recomputes this field for current writes.
    version.nav_description = "- 상태: draft - 작성자: owner"
    session.flush()
    return page, version


def _metadata_node(session, workspace_id, title, *, parent=None, page=None):
    node = WorkspaceTreeNode(
        workspace_id=workspace_id,
        section="context",
        node_kind="page_ref" if page is not None else "folder",
        target_kind="context_page" if page is not None else None,
        target_id=page.id if page is not None else None,
        parent_id=parent.id if parent is not None else None,
        title=title,
        sort_key=title,
    )
    session.add(node)
    session.flush()
    return node


def test_search_metadata_query_count_is_bounded_for_cold_one_and_twenty_results():
    session_factory = make_session_factory()
    results = []
    with session_factory.begin() as session:
        workspace_id = seed_default_workspace(session).id
        folder = _metadata_node(session, workspace_id, "Plans")
        parent, _ = _metadata_page(
            session,
            workspace_id,
            slug="parent",
            title="Parent document",
            body="Parent subject.",
        )
        document = _metadata_node(
            session, workspace_id, parent.title, parent=folder, page=parent
        )
        for index in range(20):
            body = f"# Result {index}\n\n- 상태: draft\n\nSubject {index}."
            page, version = _metadata_page(
                session,
                workspace_id,
                slug=f"result-{index}",
                title=f"Result {index}",
                body=body,
            )
            _metadata_node(
                session, workspace_id, page.title, parent=document, page=page
            )
            result = RankedResult(
                id=page.id,
                type="context_page",
                title=page.title,
                snippet="",
                score=1,
                citation_available=True,
                version_id=version.id,
            )
            result._summary_markdown = body
            results.append(result)
    viewer = ActorContext(
        actor_kind="human", actor_id="reader", workspace_role="viewer"
    )
    counts = []
    engine = session_factory.kw["bind"]
    for size in (1, 20):
        statements = []

        def record(
            _conn,
            _cursor,
            statement,
            _parameters,
            _context,
            _many,
            _statements=statements,
        ):
            _statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            with session_factory() as session:
                assert not session.identity_map
                enrich_search_result_metadata(
                    session, workspace_id, results[:size], viewer
                )
        finally:
            event.remove(engine, "before_cursor_execute", record)
        counts.append(len(statements))
        assert all(
            result.location == ["Plans", "Parent document"] for result in results[:size]
        )
        assert all(
            result.summary == f"Subject {index}."
            for index, result in enumerate(results[:size])
        )
    # One leaf query, two ancestor levels, one batched document visibility query.
    # Counting the whole stage also catches accidental body reads in summaries.
    assert counts == [4, 4]


def test_search_metadata_hides_unreadable_ancestor_and_repairs_legacy_summary():
    session_factory = make_session_factory()
    body = (
        "# Result heading\n\n- 상태: draft\n- 작성자: owner\n\n"
        "**Updated**: today\n\nNeedle subject description.\n\nLater paragraph."
    )
    with session_factory.begin() as session:
        workspace_id = seed_default_workspace(session).id
        root = _metadata_node(session, workspace_id, "Plans")
        leaves = []
        for hidden in (False, True):
            parent, _ = _metadata_page(
                session,
                workspace_id,
                slug=f"parent-{hidden}",
                title="Private parent" if hidden else "Public parent",
                body="Parent subject.",
                published=not hidden,
            )
            parent_node = _metadata_node(
                session, workspace_id, parent.title, parent=root, page=parent
            )
            folder = _metadata_node(
                session, workspace_id, "Nested folder", parent=parent_node
            )
            leaf, version = _metadata_page(
                session,
                workspace_id,
                slug=f"leaf-{hidden}",
                title=f"Result {hidden}",
                body=body,
            )
            _metadata_node(session, workspace_id, leaf.title, parent=folder, page=leaf)
            leaves.append((leaf.id, version.id))
    viewer = ActorContext(
        actor_kind="human", actor_id="reader", workspace_role="viewer"
    )
    with session_factory() as session:
        payload = SearchService().search(
            session=session,
            workspace_id=workspace_id,
            query="needle",
            scope="context_page",
            actor=viewer,
        )
        anonymous_payload = SearchService().search(
            session=session,
            workspace_id=workspace_id,
            query="needle",
            scope="context_page",
        )
    hits = {hit.id: hit for hit in payload.ranked_results}
    assert hits.keys() == {leaf_id for leaf_id, _ in leaves}
    assert hits[leaves[0][0]].location == ["Plans", "Public parent", "Nested folder"]
    assert hits[leaves[1][0]].location is None
    assert "Private parent" not in payload.model_dump_json()
    assert "Private parent" not in anonymous_payload.model_dump_json()
    assert all(hit.summary == "Needle subject description." for hit in hits.values())
    assert all("_summary_markdown" not in hit.model_dump() for hit in hits.values())
    with session_factory() as session:
        # Search cleanup never mutates the stored legacy summaries.
        assert all(
            session.get(ContextPageVersion, version_id).nav_description.startswith(
                "- 상태"
            )
            for _, version_id in leaves
        )
        owner_payload = SearchService().search(
            session=session,
            workspace_id=workspace_id,
            query="needle",
            scope="context_page",
            actor=ActorContext(
                actor_kind="human", actor_id="editor", workspace_role="editor"
            ),
        )
    private_hit = next(
        hit for hit in owner_payload.ranked_results if hit.id == leaves[1][0]
    )
    assert private_hit.location == ["Plans", "Private parent", "Nested folder"]


def test_search_service_paginates_ranked_results_with_cursor():
    # A5: the ranked set is paged by an opaque offset cursor; every match must be
    # reachable exactly once and next_cursor must be null only at the end.
    session_factory = make_session_factory()
    search_service = SearchService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    page_service = PageService(session_factory)
    for index in range(5):
        page_service.write_page(
            workspace_slug="default",
            slug="paginate-" + str(index),
            title="Uniqueneedle Page " + str(index),
            body_md="Uniqueneedle Page " + str(index) + " body",
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
            actor_provenance="browser_session",
        )

    collected: list = []
    cursor: str | None = None
    with session_factory() as session:
        for _ in range(10):  # generous cap; loop ends via has_more
            page = search_service.search(
                session=session,
                workspace_id=workspace_id,
                query="uniqueneedle",
                scope="context_page",
                limit=2,
                cursor=cursor,
            )
            assert page.total_count == 5
            assert len(page.ranked_results) <= 2
            collected.extend(result.id for result in page.ranked_results)
            if not page.has_more:
                assert page.next_cursor is None
                break
            assert page.next_cursor is not None
            cursor = page.next_cursor

    assert len(collected) == 5
    assert len(set(collected)) == 5

    with session_factory() as session:
        restarted = search_service.search(
            session=session,
            workspace_id=workspace_id,
            query="uniqueneedle",
            scope="context_page",
            limit=2,
            cursor="not-a-real-cursor",
        )
    assert restarted.total_count == 5
    assert len(restarted.ranked_results) == 2


def test_search_service_pagination_exact_multiple_has_no_trailing_empty_page():
    # A5 boundary: when total_count is an exact multiple of the limit, the final
    # full page must report has_more=False / next_cursor=None — no empty page.
    session_factory = make_session_factory()
    search_service = SearchService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
    page_service = PageService(session_factory)
    for index in range(4):
        page_service.write_page(
            workspace_slug="default",
            slug="exactmultiple-" + str(index),
            title="Exactmultiple Page " + str(index),
            body_md="Exactmultiple Page " + str(index) + " body",
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
            actor_provenance="browser_session",
        )

    collected: list = []
    cursor: str | None = None
    pages = 0
    with session_factory() as session:
        for _ in range(10):  # generous cap; loop ends via has_more
            page = search_service.search(
                session=session,
                workspace_id=workspace_id,
                query="exactmultiple",
                scope="context_page",
                limit=2,
                cursor=cursor,
            )
            assert page.total_count == 4
            pages += 1
            collected.extend(result.id for result in page.ranked_results)
            if not page.has_more:
                assert page.next_cursor is None
                break
            assert page.next_cursor is not None
            cursor = page.next_cursor

    # 4 results / limit 2 = exactly 2 pages; no empty trailing page is emitted.
    assert pages == 2
    assert len(collected) == 4
    assert len(set(collected)) == 4


def test_reranker_failure_logs_warning_and_falls_back():
    """I8 (Codex F5): a failing reranker must not silently disable reranking —
    it falls back to fusion order AND emits an operator-visible warning."""

    class _FailingReranker:
        def rerank(self, request):
            raise RuntimeError("reranker endpoint unreachable")

    service = SearchService(reranker=_FailingReranker())
    ranked = [
        RankedResult(
            id=uuid4(),
            type="context_page",
            title=f"Doc {i}",
            snippet="body",
            score=float(10 - i),
            citation_available=True,
        )
        for i in range(3)
    ]

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    log = logging.getLogger("dtxwiki.search")
    logging.disable(logging.NOTSET)
    previous_level, previous_disabled = log.level, log.disabled
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    log.disabled = False
    try:
        result = service._maybe_rerank(query="anything", ranked=ranked)
    finally:
        log.removeHandler(handler)
        log.setLevel(previous_level)
        log.disabled = previous_disabled

    # Fallback preserves the original fusion order (all results, unchanged).
    assert [r.id for r in result] == [r.id for r in ranked]
    assert any("reranker" in r.getMessage().lower() for r in records)
