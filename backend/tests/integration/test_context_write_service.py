from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from sqlalchemy import func, select

from backend.context_media.adapters.sqlalchemy import (
    SqlAlchemyContextMediaRepository,
)
from backend.context_media.application import RegisterMediaAsset
from backend.context_media.domain import ActorIdentity
from backend.domain.input_policy import InputPolicyError
from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
)
from backend.models.context_media import (
    ContextMediaUsage,
)
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.repositories import context_page_repo
from backend.schemas.context_edit import EditOperation
from backend.schemas.context_write import (
    CitationInput,
    PublishContextPageRequest,
    ReviewDecisionRequest,
    WriteDraftRequest,
)
from backend.schemas.page import BODY_MD_MAX_LENGTH
from backend.services.context_write_service import ContextWriteService
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.page_service import PageConflictError, PageService
from backend.services.read_errors import (
    RejectedByPolicyError,
)
from backend.services.review_service import ReviewService
from backend.tests.integration import test_context_media_repository as media_fixtures
from backend.tests.utils.prepared_body import prepare_context_body
from backend.utils.content_hash import content_hash as calculate_content_hash
from backend.utils.markdown_outline import parse_outline

ACTOR_ID = "00000000-0000-0000-0000-000000000010"

UNSAFE_MEDIA_MARKDOWN = (
    pytest.param(
        "![inline](data:image/png;base64,aGVsbG8=)",
        id="data-uri",
    ),
    pytest.param(
        '<video src="blob:https://example.test/temporary-video"></video>',
        id="blob-uri",
    ),
    pytest.param(
        "![temporary](/api/v1/media-sessions/"
        "00000000-0000-4000-8000-000000000001/assets/"
        "00000000-0000-4000-8000-000000000002/content)",
        id="temporary-media-url",
    ),
    pytest.param(
        "![Uploading pixel.png...](\u200b00000000-0000-4000-8000-000000000003)",
        id="zero-width-upload-marker",
    ),
    pytest.param(
        "<!-- upload cancelled 00000000-0000-4000-8000-000000000004: pixel.png -->",
        id="operational-cancel-comment",
    ),
)


def _workspace(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default"))


def _publish_link_target(session, page):
    """A resolvable link target needs a real visible head, not just a slug."""
    body = f"# {page.title}\n"
    version = ContextPageVersion(
        workspace_id=page.workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=body,
        content_hash=calculate_content_hash(body),
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id=ACTOR_ID,
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    page.current_published_version_id = version.id
    page.lifecycle_status = "published"
    session.flush()


def _open_editing_version(session, page, *, actor_id=ACTOR_ID):
    """Open a second, page-first-style "editing" version for ``page``.

    RegisterMediaAsset's repository (SqlAlchemyContextMediaRepository.add_asset)
    always requires the asset's created_for_version_id to name a currently
    "editing" version owned by the same actor (P3.2) -- write_draft's own
    returned version is already "draft" by the time these tests reach it, so
    fixtures that register media directly need their own open editing row,
    mirroring PageService.begin_editing's insert.
    """
    version_no = (
        session.scalar(
            select(func.max(ContextPageVersion.version_no)).where(
                ContextPageVersion.page_id == page.id
            )
        )
        or 0
    ) + 1
    version = ContextPageVersion(
        id=uuid4(),
        workspace_id=page.workspace_id,
        page_id=page.id,
        version_no=version_no,
        body_md="",
        content_hash=calculate_content_hash(""),
        actor_type="human",
        actor_id=None,
        version_status="draft",
        created_by_actor_kind="human",
        created_by_actor_id=actor_id,
    )
    session.add(version)
    session.flush()
    return version


def _approve(session, workspace_id, review_item_id):
    ReviewService().apply_decision(
        session=session,
        workspace_id=workspace_id,
        review_item_id=review_item_id,
        request=ReviewDecisionRequest(decision_type="approve"),
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_role="owner",
        actor_provenance="browser_session",
    )


def _library_item_ref(session, workspace_id, media_installer_factory):
    registration = LibraryService().register_library_item(
        session=session,
        workspace_id=workspace_id,
        canonical_uri="https://example.com/context-write-source",
        title="Context Write Source",
        format_hint="markdown",
        raw_text="# Evidence\n\nVerified source text.",
    )
    artifact = ExtractionService(
        media_installer_factory=media_installer_factory
    ).extract_library_item_version(
        session=session,
        library_item_version_id=registration.version.id,
    )
    return registration.version, artifact


def _current_draft(session, page):
    return session.scalar(
        select(ContextPageVersion)
        .where(
            ContextPageVersion.page_id == page.id,
            ContextPageVersion.version_status == "draft",
        )
        .order_by(
            ContextPageVersion.last_saved_at.desc(),
            ContextPageVersion.version_no.desc(),
        )
        .limit(1)
    )


def _current_draft_id(session, page):
    draft = _current_draft(session, page)
    return draft.id if draft is not None else None


def _media_page_service(session_factory):
    return PageService(
        session_factory,
        context_media_repository_factory=SqlAlchemyContextMediaRepository,
    )


def _new_draft_page(page_service, title, body_md):
    return page_service.create_page_first(
        workspace_slug="default",
        title=title,
        body_md=body_md,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )


def _published_page(page_service, slug, title, body_md):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=title,
        body_md=body_md,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        page_type="overview",
    )


def _begin(page_service, page):
    """Start a shared draft from the page's published version."""
    return page_service.begin_context_edit(
        page_id=page.id,
        workspace_id=page.workspace_id,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        idempotency_key=str(uuid4()),
    )


def _save(page_service, draft, body_md, *, title=None, **metadata):
    """Browser save of the named shared draft at its current revision."""
    return page_service.autosave_page(
        page_id=draft.id,
        workspace_id=draft.workspace_id,
        title=title,
        body_md=body_md,
        actor_kind="human",
        actor_id=ACTOR_ID,
        base_version_id=draft.version_id,
        base_content_hash=draft.content_hash,
        actor_provenance="browser_session",
        idempotency_key=str(uuid4()),
        expected_editing_revision=draft.editing_revision,
        version_id=draft.version_id,
        **metadata,
    )


def _submit(page_service, draft):
    return page_service.submit_context_edit_for_review(
        page_id=draft.id,
        version_id=draft.version_id,
        workspace_id=draft.workspace_id,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        expected_editing_revision=draft.editing_revision,
        idempotency_key=str(uuid4()),
    )


def _patch(page_service, draft, operations):
    return page_service.patch_context_edit(
        page_id=draft.id,
        version_id=draft.version_id,
        workspace_id=draft.workspace_id,
        operations=TypeAdapter(list[EditOperation]).validate_python(operations),
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        expected_editing_revision=draft.editing_revision,
        idempotency_key=str(uuid4()),
    )


def _register_image(session_factory, draft):
    with session_factory.begin() as session:
        blob, inspection_id = media_fixtures._seed_shared_image(
            session, draft.workspace_id
        )
        return RegisterMediaAsset().execute(
            SqlAlchemyContextMediaRepository(session),
            media_fixtures._candidate(
                draft.workspace_id,
                draft.id,
                draft.version_id,
                blob,
                inspection_id,
                ActorIdentity("human", ACTOR_ID),
            ),
            now=media_fixtures.NOW,
        )


def _usage(session_factory, draft, version_id, asset):
    with session_factory() as session:
        return session.scalar(
            select(ContextMediaUsage).where(
                ContextMediaUsage.workspace_id == draft.workspace_id,
                ContextMediaUsage.page_id == draft.id,
                ContextMediaUsage.page_version_id == version_id,
                ContextMediaUsage.asset_id == asset.asset_id,
            )
        )


@pytest.mark.parametrize("unsafe_body", UNSAFE_MEDIA_MARKDOWN)
def test_write_draft_rejects_unsafe_media_before_page_or_version_rows(
    session_factory,
    unsafe_body,
):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        page_count_before = session.scalar(
            select(func.count()).select_from(ContextPage)
        )
        version_count_before = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )

        with pytest.raises(RejectedByPolicyError) as captured:
            service.write_draft(
                session=session,
                workspace_id=workspace.id,
                request=WriteDraftRequest(
                    title="Unsafe Context media",
                    page_type="overview",
                    body_markdown=unsafe_body,
                    reason="prove MCP-equivalent media policy refusal",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
            )

        assert captured.value.error_code == "media_markdown_policy_invalid"
        assert (
            session.scalar(select(func.count()).select_from(ContextPage))
            == page_count_before
        )
        assert (
            session.scalar(select(func.count()).select_from(ContextPageVersion))
            == version_count_before
        )


# The per-kind matrix runs once, on write_draft above: both writers classify
# through the same body preparer, so the update path needs one representative.
def test_draft_save_allows_duplicate_logical_media_refs_and_counts_occurrences(
    session_factory,
):
    page_service = _media_page_service(session_factory)
    draft = _new_draft_page(
        page_service,
        "Duplicate logical media",
        "# Duplicate logical media\n\nSafe base.",
    )
    asset = _register_image(session_factory, draft)
    logical_ref = asset.logical_reference
    duplicate_body = (
        "# Duplicate logical media\n\n"
        f"![first]({logical_ref})\n\n![second]({logical_ref})"
    )

    saved = _save(page_service, draft, duplicate_body)

    usage = _usage(session_factory, draft, saved.version_id, asset)
    with session_factory() as session:
        stored = session.get(ContextPageVersion, saved.version_id)
    assert stored is not None
    assert stored.body_md == duplicate_body
    assert usage is not None
    assert usage.reference_count == 2


def test_human_review_approval_publishes_logical_media_with_exact_usage(
    session_factory,
):
    page_service = _media_page_service(session_factory)
    draft = _new_draft_page(
        page_service, "Review media publish", "# Review media publish\n\nSafe base."
    )
    asset = _register_image(session_factory, draft)
    body = f"# Review media publish\n\n![reviewed]({asset.logical_reference})"
    saved = _save(page_service, draft, body)
    submitted = _submit(page_service, saved)

    with session_factory.begin() as session:
        decision = ReviewService(
            context_media_repository_factory=SqlAlchemyContextMediaRepository
        ).apply_decision(
            session=session,
            workspace_id=draft.workspace_id,
            review_item_id=submitted.review_item_id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
    usage = _usage(session_factory, draft, decision.published_version_id, asset)

    assert decision.published_version_id == draft.version_id
    assert usage is not None
    assert usage.reference_count == 1


def test_write_draft_creates_reviewable_context_rows(
    session_factory, media_installer_factory
):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        library_item_version, _artifact = _library_item_ref(
            session, workspace.id, media_installer_factory
        )
        target_page = ContextPage(
            workspace_id=workspace.id,
            slug="linked-target",
            title="Linked Target",
            page_type="overview",
        )
        session.add(target_page)
        session.flush()
        _publish_link_target(session, target_page)
        result = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Context Draft",
                page_type="overview",
                body_markdown="# Context Draft\n\nVerified claim to [[linked-target]].",
                citations=[
                    CitationInput(
                        citation_type="raw_source",
                        library_item_version_id=library_item_version.id,
                        snippet="Verified source text.",
                    )
                ],
                reason="create cited context draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

        page = session.get(ContextPage, result.page_id)
        version = session.get(ContextPageVersion, result.draft_version_id)
        claim = session.scalar(select(ContextClaim))
        citation = session.scalar(select(ContextCitation))
        change_set = session.get(ChangeSet, result.change_set_id)
        operation = session.scalar(select(ChangeOperation))
        review = session.get(ReviewItem, result.review_item_id)
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "context_page.draft.create")
        )
        link = session.scalar(
            select(PageLink).where(PageLink.source_page_id == result.page_id)
        )

    assert result.next_action == "review_required"
    assert result.citation_warnings == []
    assert result.persisted_content_hash == version.content_hash
    assert result.persisted_character_count == len(version.body_md)
    assert result.outcome == "created_draft"
    assert result.body_changed is True
    assert result.metadata_changed is True
    assert result.version_status == "draft"
    assert result.draft_version_id == result.draft_version_id
    assert result.current_published_version_id is None
    assert result.review_status == "pending"
    assert _current_draft_id(session, page) == result.draft_version_id
    assert page.current_version_id is None
    assert page.current_published_version_id is None
    assert page.lifecycle_status == "draft"
    assert claim.page_version_id == result.draft_version_id
    assert claim.is_core is False
    assert citation.target_id == claim.id
    assert citation.library_item_version_id == library_item_version.id
    assert citation.library_artifact_id is None
    assert change_set.status == "draft_created"
    assert change_set.trigger_type == "mcp_write_draft"
    assert operation.diff_ref == f"context_page_version:{result.draft_version_id}"
    assert review.status == "pending"
    assert review.risk_level == "low"
    assert review.risk_policy_version
    assert review.risk_resolution_summary.startswith("context draft create")
    assert review.risk_policy_snapshot["preset"] == "balanced"
    assert audit.target_id == page.id
    assert link.source_version_id == result.draft_version_id
    assert link.target_slug == "linked-target"
    assert link.target_page_id == target_page.id


@pytest.mark.parametrize(
    "target_kind",
    ("context_claim", "context_page_section", "context_page_whole"),
)
def test_published_citation_projection_uses_writer_target_shapes(
    session_factory, target_kind, media_installer_factory
):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        library_version, _artifact = _library_item_ref(
            session, workspace.id, media_installer_factory
        )
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title=f"Citation owner {target_kind}",
                page_type="overview",
                body_markdown="# Citation owner\n\nPublished evidence.",
                citations=[
                    CitationInput(
                        target_kind=target_kind,
                        citation_type="raw_source",
                        library_item_version_id=library_version.id,
                        snippet="Verified source text.",
                    )
                ],
                reason="exercise citation owner identity",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        citation = session.scalar(
            select(ContextCitation).where(
                ContextCitation.library_item_version_id == library_version.id
            )
        )
        claim = session.scalar(
            select(ContextClaim).where(
                ContextClaim.page_version_id == created.draft_version_id
            )
        )
        assert citation.target_id == (
            claim.id if target_kind == "context_claim" else created.draft_version_id
        )
        assert (
            session.scalar(
                select(EdgeProjection).where(
                    EdgeProjection.edge_kind == "citation_source",
                    EdgeProjection.source_id == created.page_id,
                )
            )
            is None
        )

        _approve(session, workspace.id, created.review_item_id)
        service.publish_draft(
            session=session,
            workspace_id=workspace.id,
            page_id=created.page_id,
            request=PublishContextPageRequest(
                version_id=created.draft_version_id,
                reason="publish citation identity fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        edge = session.scalar(
            select(EdgeProjection).where(
                EdgeProjection.edge_kind == "citation_source",
                EdgeProjection.source_id == created.page_id,
            )
        )

    assert edge is not None
    assert edge.target_kind == "library_item"
    assert edge.target_id == library_version.library_item_id


def test_write_draft_standard_policy_warns_without_rollback(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        result = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Missing Citation Draft",
                page_type="guide",
                body_markdown="# Missing Citation Draft\n\nNeeds evidence.",
                reason="create draft before citations are ready",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        claim = session.scalar(
            select(ContextClaim).where(
                ContextClaim.page_version_id == result.draft_version_id
            )
        )

    assert result.next_action == "more_citation_needed"
    assert result.citation_warnings == ["citation_missing"]
    assert claim.is_core is True


def test_write_draft_strict_policy_rejects_without_rows(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.citation_policy = {"strictness": "strict"}
        with pytest.raises(RejectedByPolicyError):
            service.write_draft(
                session=session,
                workspace_id=workspace.id,
                request=WriteDraftRequest(
                    title="Strict Missing Citation",
                    page_type="overview",
                    body_markdown="# Strict Missing Citation\n\nNeeds evidence.",
                    reason="strict policy blocks uncited draft",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
            )
        pages = session.scalars(select(ContextPage)).all()
        changes = session.scalars(select(ChangeSet)).all()

    assert pages == []
    assert changes == []


def test_write_draft_strict_policy_allows_manual_citation_with_warning(
    session_factory, media_installer_factory
):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.citation_policy = {"strictness": "strict"}
        library_item_version, _artifact = _library_item_ref(
            session, workspace.id, media_installer_factory
        )
        result = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Strict Manual Citation",
                page_type="overview",
                body_markdown="# Strict Manual Citation\n\nCited body.",
                citations=[
                    CitationInput(
                        citation_type="raw_source",
                        library_item_version_id=library_item_version.id,
                        snippet="Verified source text.",
                        created_via="manual_input",
                    )
                ],
                reason="strict policy allows cited draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        change_set = session.get(ChangeSet, result.change_set_id)

    assert result.citation_warnings == ["workflow_sequence_incomplete"]
    assert result.next_action == "review_required"
    assert change_set.cite_call_skipped is True


def test_metadata_only_save_conflicts_on_stale_editing_revision(page_service):
    """P2.4: content_hash alone cannot catch a metadata-only change.

    Two callers read the same draft. One patches only the title -- body_md
    and content_hash never move, so the draft row is reused. A second caller
    that read the draft BEFORE that patch and still cites the same version
    must be refused on its stale editing_revision instead of silently
    clobbering the title.
    """

    draft = _new_draft_page(page_service, "Original Title", "# Original Title\n\nBody.")
    stale_revision = draft.editing_revision

    first_write = _save(page_service, draft, draft.body_md, title="Patched By Browser")
    assert first_write.version_id == draft.version_id
    assert first_write.content_hash == draft.content_hash
    assert first_write.editing_revision == stale_revision + 1

    with pytest.raises(PageConflictError):
        _save(page_service, draft, draft.body_md, title="Clobbered By Stale Write")
    with page_service.session_factory() as session:
        stored = session.get(ContextPageVersion, draft.version_id)
        assert stored.page_metadata["title"] == "Patched By Browser"

    # The same caller, having re-read the current revision, succeeds.
    second_write = _save(
        page_service, first_write, first_write.body_md, title="Patched After Re-Read"
    )
    assert second_write.editing_revision == stale_revision + 2
    with page_service.session_factory() as session:
        stored = session.get(ContextPageVersion, draft.version_id)
        assert stored.page_metadata["title"] == "Patched After Re-Read"


@pytest.mark.parametrize("position", ["before", "after", "append_child"])
def test_patch_insert_section_applies_across_every_position_under_autofix(
    session_factory,
    page_service,
    position,
):
    """insert_section always applies -- a retry is replayed from the
    idempotency ledger, so the edit engine never needs to recognize its own
    past output. Cover that the insert itself lands correctly at every
    position under Auto-fix.
    """

    original_body = (
        "# Zero\n\nzero text\n\n# Parent\n\nparent text\n\n# Sibling\n\nsibling text\n"
    )
    with session_factory.begin() as session:
        _workspace(session).auto_fix_enabled = True
    draft = _new_draft_page(page_service, f"Canonical Insert {position}", original_body)

    patched = _patch(
        page_service,
        draft,
        [
            {
                "op": "insert_section",
                "at": {"by": "heading_path", "heading_path": ["Parent"]},
                "position": position,
                "title": "Inserted",
                # Auto-fix persists this as "- item".
                "content": "* item",
            }
        ],
    )

    assert patched.version_id == draft.version_id
    assert patched.body_md.count("# Inserted") == 1
    assert "- item" in patched.body_md


@pytest.mark.parametrize(
    "operation_kind",
    ["insert_text", "append_document", "insert_section"],
)
def test_insert_operations_always_apply_and_never_report_a_no_op(
    session_factory,
    page_service,
    operation_kind,
):
    """No insertion carries retry memory in the edit engine -- a same-key
    retry is replayed from the idempotency ledger before it ever reaches the
    engine. Two distinct requests must therefore insert every time, never
    recognize a prior insert."""

    if operation_kind == "insert_text":
        original_body = "# Doc\n\nanchor\n"
        operation = {
            "op": "insert_text",
            "at": {
                "by": "anchor",
                "anchor_text": "anchor",
                "char_offset_hint": 7,
            },
            "new_text": "\n* item",
            "side": "after",
        }
    elif operation_kind == "append_document":
        original_body = "# Doc\n"
        operation = {"op": "append_document", "content": "* item"}
    else:
        original_body = "# Parent\n\nparent\n"
        operation = {
            "op": "insert_section",
            "at": {"by": "heading_path", "heading_path": ["Parent"]},
            "position": "append_child",
            "title": "Inserted",
            "content": "* item",
        }
    with session_factory.begin() as session:
        _workspace(session).auto_fix_enabled = False
    draft = _new_draft_page(
        page_service, f"Always-applies insert {operation_kind}", original_body
    )

    first = _patch(page_service, draft, [operation])
    second = _patch(page_service, first, [operation])

    assert second.version_id == first.version_id == draft.version_id
    assert second.editing_revision == first.editing_revision + 1
    assert second.body_md.count("* item") == 2
    if operation_kind == "insert_section":
        assert (
            sum(
                section.title == "Inserted"
                for section in parse_outline(second.body_md).sections
            )
            == 2
        )


def test_draft_update_preserves_published_pagelinks_until_publish(
    session_factory, page_service
):
    with session_factory.begin() as session:
        workspace = _workspace(session)
        old_target = ContextPage(
            workspace_id=workspace.id,
            slug="published-target",
            title="Published Target",
            page_type="overview",
        )
        new_target = ContextPage(
            workspace_id=workspace.id,
            slug="draft-target",
            title="Draft Target",
            page_type="overview",
        )
        session.add_all([old_target, new_target])
        session.flush()
        _publish_link_target(session, old_target)
        _publish_link_target(session, new_target)
        new_target_id = new_target.id
    published = _published_page(
        page_service,
        "published-link-source",
        "Published Link Source",
        "# Published Link Source\n\nLive [[published-target]].",
    )

    def published_link(version_id):
        with session_factory() as session:
            return session.scalar(
                select(PageLink).where(
                    PageLink.source_page_id == published.id,
                    PageLink.source_version_id == version_id,
                )
            )

    before_draft = published_link(published.version_id)
    draft = _begin(page_service, published)
    saved = _save(
        page_service, draft, "# Published Link Source\n\nDraft [[draft-target]]."
    )
    during_draft = published_link(published.version_id)
    republished = page_service.publish_draft(
        page_id=published.id,
        version_id=saved.version_id,
        reason="publish changed links",
        publish_despite_findings=False,
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
        workspace_id=published.workspace_id,
    )
    after_publish = published_link(republished.page.version_id)

    assert before_draft.target_slug == "published-target"
    assert during_draft.target_slug == "published-target"
    assert republished.page.version_id == saved.version_id
    assert after_publish.target_slug == "draft-target"
    assert after_publish.target_page_id == new_target_id


# D-3: the risk event `update_draft` measures must reflect the ACTUAL change,
# not the caller-declared `update_mode` string. `_body_for_update` treats
# every non-append mode (including `replace_section`) as a whole-body
# assignment, so the mode string alone can't distinguish a one-line tweak
# from a whole-document replace.


def test_draft_review_measures_whole_document_replace_as_full_rewrite_risk(
    session_factory, page_service
):
    """The measured classifier reaches `context_full_rewrite` through its
    NON-append branch on a real draft review, not just in unit tests."""

    published = _published_page(
        page_service,
        "replace-whole-doc",
        "Replace Section Whole Doc",
        "# Replace Section Whole Doc\n\nOriginal body.",
    )
    draft = _begin(page_service, published)
    replacement_body = "# Replace Section Whole Doc\n\n" + (
        "Completely different content. " * 100
    )
    # The measured classifier re-prices an open request on every save.
    submitted = _submit(page_service, draft)
    _save(page_service, submitted, replacement_body)

    with session_factory() as session:
        review = session.get(ReviewItem, submitted.review_item_id)
    assert review.risk_level == "medium"
    assert (
        review.risk_signals["reason_codes"][0] == "default:context_full_rewrite:medium"
    )


def test_draft_review_small_append_stays_low_risk(session_factory, page_service):
    published = _published_page(
        page_service, "small-append", "Small Append", "# Small Append\n\nOriginal body."
    )
    draft = _begin(page_service, published)
    submitted = _submit(page_service, draft)
    _save(page_service, submitted, draft.body_md + "\n\n## Added\n\nA short addition.")

    with session_factory() as session:
        review = session.get(ReviewItem, submitted.review_item_id)
    assert review.risk_level == "low"
    assert review.risk_signals["reason_codes"] == ["default:context_partial_update:low"]


def test_draft_review_large_append_over_strict_threshold_becomes_medium(
    session_factory, page_service
):
    published = _published_page(
        page_service, "large-append", "Large Append", "# Large Append\n\nOriginal body."
    )
    draft = _begin(page_service, published)
    large_addition = "## Added\n\n" + ("x" * 8300)
    submitted = _submit(page_service, draft)
    _save(page_service, submitted, draft.body_md + "\n\n" + large_addition)

    with session_factory() as session:
        review = session.get(ReviewItem, submitted.review_item_id)
    assert review.risk_level == "medium"
    assert (
        review.risk_signals["reason_codes"][0] == "default:context_full_rewrite:medium"
    )


def test_patch_rejects_append_that_pushes_merged_body_past_max_length(page_service):
    """D-5: `BODY_MD_MAX_LENGTH` is enforced by the schema only on each INPUT
    field, so an append can merge a valid base plus a valid addition into a
    persisted body that exceeds the limit -- the merged result must be
    re-checked after the merge, not just its ingredients beforehand.
    """
    near_limit_body = "# Near Limit\n\n" + ("a" * (BODY_MD_MAX_LENGTH - 100))
    draft = _new_draft_page(page_service, "Near Limit", near_limit_body)

    # The body preparer re-validates the merged body; REST and MCP map this
    # refusal to validation_failed.
    with pytest.raises(InputPolicyError, match="body must contain"):
        _patch(
            page_service,
            draft,
            [{"op": "append_document", "content": "## Added\n\n" + ("b" * 200)}],
        )

    with page_service.session_factory() as session:
        stored = session.get(ContextPageVersion, draft.version_id)
        assert stored.body_md == draft.body_md
        assert stored.editing_revision == draft.editing_revision


def test_write_draft_sets_decision_summary_code(session_factory):
    """I18N-08a: `ReviewItem.decision_summary` is English prose written
    straight into the row, so it can never be localized. The fixed-copy
    cases must also set `decision_summary_code` to a stable machine code;
    caller-supplied free text (`risk_notes`) has no fixed translation and
    must leave the code unset so the frontend falls back to the prose.
    """
    service = ContextWriteService()
    with session_factory.begin() as session:
        workspace = _workspace(session)
        # write_draft, no risk_notes: the fixed fallback copy gets a code.
        no_notes = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Decision Summary Code No Notes",
                page_type="overview",
                body_markdown="# Decision Summary Code No Notes\n\nBody.",
                reason="create draft without risk notes",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        no_notes_review = session.get(ReviewItem, no_notes.review_item_id)
        assert no_notes_review.decision_summary == "context draft requires review"
        assert no_notes_review.decision_summary_code == "context_draft_requires_review"
        # write_draft, with risk_notes: caller-supplied free text, no code.
        with_notes = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Decision Summary Code With Notes",
                page_type="overview",
                body_markdown="# Decision Summary Code With Notes\n\nBody.",
                reason="create draft with risk notes",
                risk_notes="flagging this for a second look",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        with_notes_review = session.get(ReviewItem, with_notes.review_item_id)
        assert with_notes_review.decision_summary == "flagging this for a second look"
        assert with_notes_review.decision_summary_code is None


def test_write_draft_korean_titles_create_distinct_pages(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        first = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="한글 제목 하나",
                page_type="overview",
                body_markdown="# 한글 제목 하나\n\n첫 번째 본문.",
                reason="create first korean draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        second = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="한글 제목 둘",
                page_type="overview",
                body_markdown="# 한글 제목 둘\n\n두 번째 본문.",
                reason="create second korean draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        first_slug = session.get(ContextPage, first.page_id).slug
        second_slug = session.get(ContextPage, second.page_id).slug

    assert first.page_id != second.page_id
    assert first_slug == "한글-제목-하나"
    assert second_slug == "한글-제목-둘"


def test_write_draft_uniquifies_slug_when_titles_collide(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        first = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Hello World",
                page_type="overview",
                body_markdown="# Hello World\n\nFirst body.",
                reason="create first colliding draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        second = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Hello, World!",
                page_type="overview",
                body_markdown="# Hello, World!\n\nSecond body.",
                reason="create second colliding draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        first_page = session.get(ContextPage, first.page_id)
        second_page = session.get(ContextPage, second.page_id)
        first_slug = first_page.slug
        first_title = first_page.title
        second_slug = second_page.slug
        second_title = second_page.title

    assert first.page_id != second.page_id
    assert first_slug == "hello-world"
    assert first_title == "Hello World"
    assert second_slug == "hello-world-2"
    assert second_title == "Hello, World!"


def test_write_draft_existing_page_defers_metadata_until_publish(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        first = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Existing Metadata Page",
                page_type="overview",
                body_markdown="# Existing Metadata Page\n\nFirst body.",
                reason="create initial draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        _approve(session, workspace.id, first.review_item_id)
        service.publish_draft(
            session=session,
            workspace_id=workspace.id,
            page_id=first.page_id,
            request=PublishContextPageRequest(
                version_id=first.draft_version_id,
                reason="publish initial draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        second = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Existing Metadata Page",
                page_type="guide",
                body_markdown="# Existing Metadata Page\n\nSecond body.",
                reason="rewrite as guide",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(ContextPage, second.page_id)
        second_version = session.get(ContextPageVersion, second.draft_version_id)
        assert second.persisted_character_count == len(second_version.body_md)
        page_type_before_publish = page.page_type
        _approve(session, workspace.id, second.review_item_id)
        service.publish_draft(
            session=session,
            workspace_id=workspace.id,
            page_id=second.page_id,
            request=PublishContextPageRequest(
                version_id=second.draft_version_id,
                reason="publish guide rewrite",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page_type_after_publish = page.page_type

    assert second.page_id == first.page_id
    assert second.outcome == "updated_draft"
    assert page_type_before_publish == "overview"
    assert page_type_after_publish == "guide"


def test_never_published_draft_metadata_is_version_scoped(
    session_factory, page_service
):
    draft = _new_draft_page(
        page_service,
        "Unpublished Metadata Page",
        "# Unpublished Metadata Page\n\nBody.",
    )

    _save(
        page_service,
        draft,
        draft.body_md,
        title="Immediate Draft Title",
        aliases=["Immediate Alias"],
    )

    with session_factory() as session:
        version = session.get(ContextPageVersion, draft.version_id)
        metadata_operations = session.scalars(
            select(ChangeOperation)
            .where(ChangeOperation.target_id == draft.id)
            .where(ChangeOperation.operation == "metadata_patch")
        ).all()
    assert version.page_metadata["title"] == "Immediate Draft Title"
    # P2.3: renaming no longer auto-preserves the retired title
    # ("Unpublished Metadata Page") as an alias.
    assert version.page_metadata["aliases"] == ["Immediate Alias"]
    assert metadata_operations == []


def test_draft_metadata_reject_leaves_page_untouched(session_factory, page_service):
    published = _published_page(
        page_service,
        "reject-patch-target",
        "Reject Patch Target",
        "# Reject Patch Target\n\nBody.",
    )
    draft = _begin(page_service, published)
    saved = _save(
        page_service,
        draft,
        draft.body_md,
        title="Rejected Title",
        page_type="concept",
        tags=["rejected"],
    )
    submitted = _submit(page_service, saved)

    with session_factory.begin() as session:
        ReviewService().apply_decision(
            session=session,
            workspace_id=published.workspace_id,
            review_item_id=submitted.review_item_id,
            request=ReviewDecisionRequest(decision_type="reject"),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
    with session_factory() as session:
        page = session.get(ContextPage, published.id)
        draft_version = session.get(ContextPageVersion, draft.version_id)
        title_after_reject = page.title
        page_type_after_reject = page.page_type
        tags_after_reject = list(page.tags or [])
        published_after_reject = page.current_published_version_id

    assert draft.version_id != published.version_id
    assert draft_version.version_status == "closed"
    assert draft_version.page_metadata["title"] == "Rejected Title"
    assert title_after_reject == "Reject Patch Target"
    assert page_type_after_reject == "overview"
    assert tags_after_reject == []
    assert published_after_reject == published.version_id


def test_publish_draft_refuses_pending_review(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Pending Publish Target",
                page_type="overview",
                body_markdown="# Pending Publish Target\n\nBody.",
                reason="create draft awaiting review",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        with pytest.raises(RejectedByPolicyError):
            service.publish_draft(
                session=session,
                workspace_id=workspace.id,
                page_id=created.page_id,
                request=PublishContextPageRequest(
                    version_id=created.draft_version_id,
                    reason="publish before review decision",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        page = session.get(ContextPage, created.page_id)
        version = session.get(ContextPageVersion, created.draft_version_id)
        review = session.get(ReviewItem, created.review_item_id)
        published_id = page.current_published_version_id
        version_status = version.version_status
        review_status = review.status

    assert published_id is None
    assert version_status == "draft"
    assert review_status == "pending"


def test_publish_draft_applies_approved_change_set(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Approved Publish Target",
                page_type="overview",
                body_markdown="# Approved Publish Target\n\nBody.",
                reason="create draft for approval",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        _approve(session, workspace.id, created.review_item_id)
        result = service.publish_draft(
            session=session,
            workspace_id=workspace.id,
            page_id=created.page_id,
            request=PublishContextPageRequest(
                version_id=created.draft_version_id,
                reason="publish approved draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        review = session.get(ReviewItem, created.review_item_id)
        draft_change_set = session.get(ChangeSet, created.change_set_id)
        published_id = page.current_published_version_id
        review_status = review.status
        draft_change_set_status = draft_change_set.status

    assert result.published_version_id == created.draft_version_id
    assert published_id == created.draft_version_id
    assert review_status == "approved"
    assert draft_change_set_status == "applied"


def test_publish_draft_without_bound_review_publishes(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Review Free Publish Target",
                page_type="overview",
                body_markdown="# Review Free Publish Target\n\nBody.",
                reason="seed page for review-free draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        _approve(session, workspace.id, created.review_item_id)
        service.publish_draft(
            session=session,
            workspace_id=workspace.id,
            page_id=created.page_id,
            request=PublishContextPageRequest(
                version_id=created.draft_version_id,
                reason="publish seeded draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        unbound_draft = context_page_repo.insert_version(
            session,
            page_id=created.page_id,
            parent_version_id=created.draft_version_id,
            body=prepare_context_body(
                session,
                page_id=created.page_id,
                body_md="# Review Free Publish Target\n\nUnreviewed body.",
                actor_kind="human",
                actor_id=ACTOR_ID,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            version_status="draft",
        )
        # Publication accepts a review-free draft, but it must still be the
        # page's latest linear draft head.  Merely inserting a historical row
        # is not enough to make it publishable.
        page = session.get(ContextPage, created.page_id)
        assert page is not None
        page.latest_draft_version_id = unbound_draft.id
        result = service.publish_draft(
            session=session,
            workspace_id=workspace.id,
            page_id=created.page_id,
            request=PublishContextPageRequest(
                version_id=unbound_draft.id,
                reason="publish review-free draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        published_id = page.current_published_version_id

    assert result.published_version_id == unbound_draft.id
    assert published_id == unbound_draft.id


def test_publish_revalidates_stored_unsafe_body_without_moving_published_pointer(
    session_factory,
):
    service = ContextWriteService()
    unsafe_body = "![inline](data:image/png;base64,aGVsbG8=)"

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Stored unsafe publish target",
                page_type="overview",
                body_markdown="# Stored unsafe publish target\n\nSafe published body.",
                reason="seed published head before legacy-body revalidation",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        _approve(session, workspace.id, created.review_item_id)
        service.publish_draft(
            session=session,
            workspace_id=workspace.id,
            page_id=created.page_id,
            request=PublishContextPageRequest(
                version_id=created.draft_version_id,
                reason="publish safe seed",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        assert page is not None
        published_id_before = page.current_published_version_id
        current_id_before = page.current_version_id
        published_version = session.get(ContextPageVersion, published_id_before)
        assert published_version is not None

        legacy_draft = context_page_repo.insert_version(
            session,
            page_id=created.page_id,
            parent_version_id=published_id_before,
            body=prepare_context_body(
                session,
                page_id=created.page_id,
                body_md="# Stored unsafe publish target\n\nSafe staged body.",
                actor_kind="human",
                actor_id=ACTOR_ID,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            version_status="draft",
        )
        page.latest_draft_version_id = legacy_draft.id
        session.flush()

        # Simulate a legacy/corrupt row that predates the write-ingress gate.
        # The unsafe bytes are deliberately flushed before publish opens its
        # SAVEPOINT so the test exercises publication's fresh stored-body check.
        legacy_draft.body_md = unsafe_body
        legacy_draft.content_hash = calculate_content_hash(unsafe_body)
        session.flush()

        with pytest.raises(RejectedByPolicyError) as captured:
            service.publish_draft(
                session=session,
                workspace_id=workspace.id,
                page_id=created.page_id,
                request=PublishContextPageRequest(
                    version_id=legacy_draft.id,
                    reason="reject stored unsafe body",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )

        assert captured.value.error_code == "media_markdown_policy_invalid"
        session.refresh(page)
        session.refresh(legacy_draft)
        session.refresh(published_version)
        assert page.current_published_version_id == published_id_before
        assert page.current_version_id == current_id_before
        assert _current_draft_id(session, page) == legacy_draft.id
        assert page.lifecycle_status == "published"
        assert published_version.version_status == "published_snapshot"
        assert legacy_draft.version_status == "draft"
        assert legacy_draft.body_md == unsafe_body


def test_write_draft_converges_when_concurrent_insert_wins_slug(
    session_factory, monkeypatch
):
    import backend.services.context_write_service as context_write_module

    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace_id = workspace.id
        winner = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Race Title",
                page_type="overview",
                body_markdown="# Race Title\n\nWinner body.",
                reason="winner of the slug race",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

    # Force the loser's locking lookup to miss once, so its INSERT collides
    # with the committed winner row on uq_page_ws_slug and must recover.
    real_lookup = context_write_module.get_page_by_slug_for_update
    state = {"missed": False}

    def miss_once(session, workspace_id, slug):
        if not state["missed"]:
            state["missed"] = True
            return None
        return real_lookup(session, workspace_id, slug)

    monkeypatch.setattr(context_write_module, "get_page_by_slug_for_update", miss_once)
    # This test exercises the lower-level concurrent INSERT fallback. The new
    # name-index fast path normally finds the winner before slug allocation.
    monkeypatch.setattr(
        context_write_module,
        "unique_visible_page_name_match",
        lambda *args, **kwargs: None,
    )

    with session_factory.begin() as session:
        loser = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Race Title",
                page_type="overview",
                body_markdown="# Race Title\n\nLoser body.",
                reason="loser of the slug race",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page_count = session.scalar(
            select(func.count())
            .select_from(ContextPage)
            .where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.slug == "race-title",
            )
        )

    assert state["missed"] is True
    assert loser.page_id == winner.page_id
    assert loser.draft_version_id != winner.draft_version_id
    assert page_count == 1


def test_write_draft_uniquifies_after_concurrent_insert_with_other_title(
    session_factory, monkeypatch
):
    import backend.services.context_write_service as context_write_module

    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace_id = workspace.id
        winner = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Race Title",
                page_type="overview",
                body_markdown="# Race Title\n\nWinner body.",
                reason="winner of the slug race",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

    real_lookup = context_write_module.get_page_by_slug_for_update
    state = {"missed": False}

    def miss_once(session, workspace_id, slug):
        if not state["missed"]:
            state["missed"] = True
            return None
        return real_lookup(session, workspace_id, slug)

    monkeypatch.setattr(context_write_module, "get_page_by_slug_for_update", miss_once)
    # This test exercises the lower-level concurrent INSERT fallback. The new
    # name-index fast path normally finds the winner before slug allocation.
    monkeypatch.setattr(
        context_write_module,
        "unique_visible_page_name_match",
        lambda *args, **kwargs: None,
    )

    with session_factory.begin() as session:
        loser = service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Race: Title!",
                page_type="overview",
                body_markdown="# Race: Title!\n\nLoser body.",
                reason="colliding slug with a different title",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        loser_page = session.get(ContextPage, loser.page_id)
        loser_slug = loser_page.slug
        loser_title = loser_page.title

    assert state["missed"] is True
    assert loser.page_id != winner.page_id
    assert loser_slug == "race-title-2"
    assert loser_title == "Race: Title!"
