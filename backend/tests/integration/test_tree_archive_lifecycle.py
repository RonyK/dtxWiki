"""B-8: revive-upsert + archive lifecycle cascade (Sprint-3 C1-T4, fixes A-3)."""

import pytest
from sqlalchemy import select

from backend.app.db import make_engine, make_session_factory
from backend.llm.embedding import DeterministicEmbedding
from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.chunk import ContentChunk
from backend.models.context import ContextPage, ContextPageVersion, PageNameIndex
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.context_write import (
    ReviewDecisionRequest,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.schemas.tree import TreeFolderCreateRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.concept_resolution import resolve_context_concept
from backend.services.context_write_service import ContextWriteService
from backend.services.indexing import DbNativeIndexer
from backend.services.okf import build_workspace_index
from backend.services.page_service import PageService
from backend.services.read_errors import ReadValidationError
from backend.services.review_service import ReviewService
from backend.services.tree_service import TreeService
from backend.services.vector_store import BruteForceVectorStore

pytestmark = pytest.mark.integration

REVIEWER_ID = "00000000-0000-0000-0000-000000000010"


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _owner_actor(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id=REVIEWER_ID,
        scopes=(),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
        token_kind="session",
    )


def _primary_node(session, page_id):
    return session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.target_kind == "context_page",
            WorkspaceTreeNode.target_id == page_id,
            WorkspaceTreeNode.primary_placement.is_(True),
        )
    )


def _write(page_service: PageService, slug: str, title: str, base=None):
    kwargs = {}
    if base is not None:
        kwargs = {
            "base_version_id": base.version_id,
            "base_content_hash": base.content_hash,
        }
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=title,
        body_md=f"# {title}\n\nBody paragraph.",
        actor_kind="human",
        actor_id="tester",
        **kwargs,
        actor_provenance="browser_session",
    )


def test_archive_then_rewrite_revives_instead_of_unique_violation(
    session_factory, page_service: PageService
):
    """A-3 repro: archive the page's node, then write the same slug again.

    Before the fix the second write created a second primary_placement=true
    node for the same target -> UNIQUE violation -> 500."""

    payload = _write(page_service, "a3-page", "A3 Page")
    tree = TreeService()
    with session_factory.begin() as session:
        node = _primary_node(session, payload.id)
        tree.archive_node(
            session=session,
            workspace_id=_workspace_id(session),
            node_id=node.id,
            actor_kind="human",
            actor_id="tester",
        )

    revived = _write(page_service, "a3-page", "A3 Page Reborn", base=payload)

    with session_factory() as session:
        node = _primary_node(session, revived.id)
        assert node is not None
        assert node.visibility_status == "visible"
        nodes = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == revived.id,
            )
        ).all()
        assert len(nodes) == 1  # revived, not duplicated


def test_folder_archive_cascades_to_descendant_pages(
    session_factory, page_service: PageService
):
    payload = _write(page_service, "cascade-page", "Cascade Page")
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "context",
            )
        )
        folder = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Doomed"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        node = _primary_node(session, payload.id)
        node.parent_id = folder.id
        session.flush()
        DbNativeIndexer(
            embedder=DeterministicEmbedding(dim=128),
            vector_store=BruteForceVectorStore(),
        ).index(session=session, source_kind="context_page", source_id=payload.id)
        assert session.scalar(
            select(ContentChunk.id)
            .where(ContentChunk.document_id == payload.id)
            .limit(1)
        ) is not None
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=folder.id,
            actor_kind="human",
            actor_id="tester",
        )

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        page = session.get(ContextPage, payload.id)
        assert page.lifecycle_status == "archived"
        assert session.scalar(
            select(ContentChunk.id)
            .where(ContentChunk.document_id == page.id)
            .limit(1)
        ) is None
        # gone from the name index -> entity lane and read_concept stop finding it
        rows = session.scalars(
            select(PageNameIndex).where(PageNameIndex.page_id == page.id)
        ).all()
        assert rows == []
        result = resolve_context_concept(
            session, workspace_id=workspace_id, identifier="cascade-page", actor=None
        )
        assert result.status == "not_found"
        index = build_workspace_index(
            session, workspace_id, actor=None, workspace_slug="default"
        )
        assert "Cascade Page" not in index
        # archive delta is recorded for what_changed (C2)
        ops = session.scalars(
            select(ChangeOperation).where(
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page.id,
                ChangeOperation.operation == "archive",
            )
        ).all()
        assert len(ops) == 1
        assert ops[0].diff_ref.startswith("context_page_version:")


def test_archive_keeps_pending_context_review_readable_and_actionable(
    session_factory, page_service: PageService
):
    write = ContextWriteService()
    reviews = ReviewService()
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        actor = _owner_actor(workspace_id)
        created = write.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Archived Review",
                page_type="overview",
                body_markdown="# Archived Review\n\nReadable while archived.",
                citations=[],
                reason="pending draft before archive",
            ),
            actor_kind="human",
            actor_id=REVIEWER_ID,
            actor_role="owner",
        )
        review = session.get(ReviewItem, created.review_item_id)
        change_set = session.get(ChangeSet, review.change_set_id)
        page = session.get(ContextPage, created.page_id)
        page_slug = page.slug

        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=_primary_node(session, page.id).id,
            actor_kind="human",
            actor_id=REVIEWER_ID,
        )
        session.refresh(review)
        session.refresh(change_set)
        session.refresh(page)

        assert page.lifecycle_status == "archived"
        assert _primary_node(session, page.id).visibility_status == "archived"
        assert review.status == "pending"
        assert change_set.status == "draft_created"
        assert session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.action == "review.superseded_by_page_archive",
                AuditEvent.target_id == review.id,
            )
        ) is None

        detail = reviews.get_review(
            session=session,
            workspace_id=workspace_id,
            review_item_id=review.id,
            actor_role="owner",
            actor=actor,
        )
        target = next(
            operation
            for operation in detail.evidence.operations
            if operation.target_type == "context_page"
        )
        assert detail.can_decide is True
        assert target.target_title == "Archived Review"
        assert target.target_slug == page_slug

        review_id = review.id
        page_id = page.id
        draft_version_id = created.draft_version_id
        original_change_set_id = change_set.id

    readable = page_service.read_page(
        workspace_slug="default",
        slug=page_slug,
        actor=actor,
    )
    assert "Readable while archived." in readable.body_md

    with session_factory.begin() as session:
        approved = reviews.apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=review_id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id=REVIEWER_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        page = session.get(ContextPage, page_id)
        node = _primary_node(session, page_id)

        assert approved.status == "approved"
        assert approved.published_version_id == draft_version_id
        assert session.get(ChangeSet, original_change_set_id).status == "applied"
        assert page.lifecycle_status == "published"
        assert page.current_published_version_id == draft_version_id
        assert page.latest_draft_version_id is None
        assert node.visibility_status == "visible"


def test_rejecting_archived_page_draft_keeps_page_archived_and_unindexed(
    session_factory,
):
    write = ContextWriteService()
    reviews = ReviewService()
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Archived Rejection",
                page_type="overview",
                body_markdown="# Published\n\nStable body.",
                citations=[],
                reason="published base",
            ),
            actor_kind="human",
            actor_id=REVIEWER_ID,
            actor_role="owner",
        )
        reviews.apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=created.review_item_id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id=REVIEWER_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        page = session.get(ContextPage, created.page_id)
        published_version = session.get(ContextPageVersion, page.current_version_id)
        draft = write.update_draft(
            session=session,
            workspace_id=workspace_id,
            request=UpdateDraftRequest(
                page_id=created.page_id,
                base_version_id=published_version.id,
                base_content_hash=published_version.content_hash,
                update_mode="full_rewrite",
                body_markdown="# Candidate\n\nDraft body.",
                reason="candidate before archive",
            ),
            actor_kind="human",
            actor_id=REVIEWER_ID,
        )
        DbNativeIndexer(
            embedder=DeterministicEmbedding(dim=128),
            vector_store=BruteForceVectorStore(),
        ).index(
            session=session,
            source_kind="context_page",
            source_id=created.page_id,
        )
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=_primary_node(session, created.page_id).id,
            actor_kind="human",
            actor_id=REVIEWER_ID,
        )
        page_id = created.page_id
        published_version_id = published_version.id
        review_id = draft.review_item_id

    with session_factory.begin() as session:
        reviews.apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=review_id,
            request=ReviewDecisionRequest(decision_type="reject"),
            actor_kind="human",
            actor_id=REVIEWER_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        page = session.get(ContextPage, page_id)
        node = _primary_node(session, page_id)

        assert page.lifecycle_status == "archived"
        assert page.current_version_id == published_version_id
        assert page.current_published_version_id == published_version_id
        assert page.latest_draft_version_id is None
        assert node.visibility_status == "archived"
        assert session.scalars(
            select(PageNameIndex).where(PageNameIndex.page_id == page_id)
        ).all() == []
        assert session.scalar(
            select(ContentChunk.id)
            .where(ContentChunk.document_id == page_id)
            .limit(1)
        ) is None


def test_review_approval_refreshes_page_archived_after_stale_identity_load(tmp_path):
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'review-race.sqlite').as_posix()}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    write = ContextWriteService()
    reviews = ReviewService()
    tree = TreeService()
    try:
        with session_factory.begin() as session:
            seed_default_workspace(session)
            workspace_id = _workspace_id(session)
            created = write.write_draft(
                session=session,
                workspace_id=workspace_id,
                request=WriteDraftRequest(
                    title="Archive Approval Race",
                    page_type="overview",
                    body_markdown="# Published\n\nStable body.",
                    citations=[],
                    reason="published base",
                ),
                actor_kind="human",
                actor_id=REVIEWER_ID,
                actor_role="owner",
            )
            reviews.apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=created.review_item_id,
                request=ReviewDecisionRequest(decision_type="approve"),
                actor_kind="human",
                actor_id=REVIEWER_ID,
                actor_role="owner",
                actor_provenance="browser_session",
            )
            page = session.get(ContextPage, created.page_id)
            published_version = session.get(
                ContextPageVersion,
                page.current_version_id,
            )
            draft = write.update_draft(
                session=session,
                workspace_id=workspace_id,
                request=UpdateDraftRequest(
                    page_id=page.id,
                    base_version_id=published_version.id,
                    base_content_hash=published_version.content_hash,
                    update_mode="full_rewrite",
                    body_markdown="# Candidate\n\nPublish after archive.",
                    reason="candidate",
                ),
                actor_kind="human",
                actor_id=REVIEWER_ID,
            )
            page_id = page.id
            review_id = draft.review_item_id

        with session_factory() as stale_session:
            stale_page = stale_session.get(ContextPage, page_id)
            assert stale_page.lifecycle_status == "published"

            with session_factory.begin() as archive_session:
                tree.archive_node(
                    session=archive_session,
                    workspace_id=workspace_id,
                    node_id=_primary_node(archive_session, page_id).id,
                    actor_kind="human",
                    actor_id=REVIEWER_ID,
                )

            # The approving session still carries its pre-archive identity.
            assert stale_page.lifecycle_status == "published"
            reviews.apply_decision(
                session=stale_session,
                workspace_id=workspace_id,
                review_item_id=review_id,
                request=ReviewDecisionRequest(decision_type="approve"),
                actor_kind="human",
                actor_id=REVIEWER_ID,
                actor_role="owner",
                actor_provenance="browser_session",
            )
            stale_session.commit()

        with session_factory() as session:
            page = session.get(ContextPage, page_id)
            node = _primary_node(session, page_id)
            assert page.lifecycle_status == "published"
            assert page.current_published_version_id == draft.draft_version_id
            assert node.visibility_status == "visible"
    finally:
        engine.dispose()


def test_review_decision_revalidates_stale_review_before_second_terminal_write(
    tmp_path,
):
    database_path = (tmp_path / "decision-race.sqlite").as_posix()
    database_url = f"sqlite+pysqlite:///{database_path}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    write = ContextWriteService()
    reviews = ReviewService()
    try:
        with session_factory.begin() as session:
            seed_default_workspace(session)
            workspace_id = _workspace_id(session)
            created = write.write_draft(
                session=session,
                workspace_id=workspace_id,
                request=WriteDraftRequest(
                    title="Decision Race",
                    page_type="overview",
                    body_markdown="# Candidate\n\nOne decision only.",
                    citations=[],
                    reason="candidate",
                ),
                actor_kind="human",
                actor_id=REVIEWER_ID,
                actor_role="owner",
            )
            review_id = created.review_item_id

        with session_factory() as stale_session:
            stale_review = stale_session.get(ReviewItem, review_id)
            assert stale_review.status == "pending"

            with session_factory.begin() as concurrent_session:
                reviews.apply_decision(
                    session=concurrent_session,
                    workspace_id=workspace_id,
                    review_item_id=review_id,
                    request=ReviewDecisionRequest(decision_type="reject"),
                    actor_kind="human",
                    actor_id=REVIEWER_ID,
                    actor_role="owner",
                    actor_provenance="browser_session",
                )

            # The second decision must re-read the row after write intent/lock;
            # its identity map still says pending from before the rejection.
            assert stale_review.status == "pending"
            with pytest.raises(ReadValidationError, match="cannot decide"):
                reviews.apply_decision(
                    session=stale_session,
                    workspace_id=workspace_id,
                    review_item_id=review_id,
                    request=ReviewDecisionRequest(decision_type="approve"),
                    actor_kind="human",
                    actor_id=REVIEWER_ID,
                    actor_role="owner",
                    actor_provenance="browser_session",
                )
            stale_session.rollback()

        with session_factory() as session:
            assert session.get(ReviewItem, review_id).status == "rejected"
            assert len(
                session.scalars(
                    select(ReviewDecision).where(
                        ReviewDecision.review_item_id == review_id
                    )
                ).all()
            ) == 1
    finally:
        engine.dispose()

def test_rewrite_after_folder_archive_lands_under_default_parent(
    session_factory, page_service: PageService
):
    """Reviving under an archived ancestor chain must reparent to the section
    default parent, not mass-revive the deliberately archived folder."""

    payload = _write(page_service, "orphan-revive", "Orphan Revive")
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.slug == "context",
            )
        )
        folder = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Archived Home"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        node = _primary_node(session, payload.id)
        node.parent_id = folder.id
        session.flush()
        folder_id = folder.id
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=folder_id,
            actor_kind="human",
            actor_id="tester",
        )

    revived = _write(
        page_service, "orphan-revive", "Orphan Revive Again", base=payload
    )

    with session_factory() as session:
        page = session.get(ContextPage, revived.id)
        assert page.lifecycle_status == "published"
        node = _primary_node(session, revived.id)
        assert node.visibility_status == "visible"
        # reparented away from the archived folder
        assert node.parent_id != folder_id
        parent = session.get(WorkspaceTreeNode, node.parent_id)
        assert parent.visibility_status == "visible"
        # the archived folder itself stays archived
        folder = session.get(WorkspaceTreeNode, folder_id)
        assert folder.visibility_status == "archived"
