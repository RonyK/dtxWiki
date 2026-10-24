from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend.context_media.adapters.sqlalchemy import (
    SqlAlchemyContextMediaRepository,
)
from backend.context_media.application import RegisterMediaAsset
from backend.context_media.domain import ActorIdentity
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
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.repositories import context_page_repo
from backend.schemas.context_edit import PatchDraftRequest
from backend.schemas.context_write import (
    CitationInput,
    PublishContextPageRequest,
    ReviewDecisionRequest,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.schemas.page import BODY_MD_MAX_LENGTH
from backend.services.context_editing import ResolvedEdit, section_content_hash
from backend.services.context_write_service import (
    ContextWriteService,
    _body_changed_outside_edit_ranges,
    _measure_update_event,
)
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.lint.autofix import safe_normalize_markdown
from backend.services.read_errors import (
    ReadValidationError,
    RejectedByPolicyError,
    StaleBaseVersionError,
)
from backend.services.review_risk_policy import _preset_policy
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
        version_status="editing",
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


@pytest.mark.parametrize("unsafe_body", UNSAFE_MEDIA_MARKDOWN)
def test_update_draft_rejects_unsafe_media_without_new_version_or_body_mutation(
    session_factory,
    unsafe_body,
):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Safe Context media base",
                page_type="overview",
                body_markdown="# Safe Context media base\n\nPersisted body.",
                reason="seed safe MCP-equivalent draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(ContextPage, created.page_id)
        base_version = session.get(ContextPageVersion, created.draft_version_id)
        assert page is not None
        assert base_version is not None
        version_count_before = session.scalar(
            select(func.count())
            .select_from(ContextPageVersion)
            .where(ContextPageVersion.page_id == page.id)
        )
        pointer_snapshot = (
            page.current_version_id,
            page.current_published_version_id,
            page.latest_draft_version_id,
        )
        body_snapshot = (base_version.body_md, base_version.content_hash)

        with pytest.raises(RejectedByPolicyError) as captured:
            service.update_draft(
                session=session,
                workspace_id=workspace.id,
                request=UpdateDraftRequest(
                    page_id=page.id,
                    base_version_id=base_version.id,
                    base_content_hash=base_version.content_hash,
                    update_mode="full_rewrite",
                    body_markdown=unsafe_body,
                    reason="reject unsafe MCP-equivalent update",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )

        assert captured.value.error_code == "media_markdown_policy_invalid"
        session.refresh(page)
        session.refresh(base_version)
        assert (
            session.scalar(
                select(func.count())
                .select_from(ContextPageVersion)
                .where(ContextPageVersion.page_id == page.id)
            )
            == version_count_before
        )
        assert (
            page.current_version_id,
            page.current_published_version_id,
            page.latest_draft_version_id,
        ) == pointer_snapshot
        assert (base_version.body_md, base_version.content_hash) == body_snapshot


def test_update_draft_allows_duplicate_logical_media_refs_and_counts_occurrences(
    session_factory,
):
    service = ContextWriteService(
        context_media_repository_factory=SqlAlchemyContextMediaRepository
    )

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Duplicate logical media",
                page_type="overview",
                body_markdown="# Duplicate logical media\n\nSafe base.",
                reason="seed page-owned media fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        base_version = session.get(ContextPageVersion, created.draft_version_id)
        assert base_version is not None
        page = session.get(ContextPage, created.page_id)
        editing_version = _open_editing_version(session, page)
        blob, inspection_id = media_fixtures._seed_shared_image(session, workspace.id)
        actor = ActorIdentity("human", ACTOR_ID)
        asset = RegisterMediaAsset().execute(
            SqlAlchemyContextMediaRepository(session),
            media_fixtures._candidate(
                workspace.id,
                created.page_id,
                editing_version.id,
                blob,
                inspection_id,
                actor,
            ),
            now=media_fixtures.NOW,
        )
        logical_ref = asset.logical_reference
        duplicate_body = (
            "# Duplicate logical media\n\n"
            f"![first]({logical_ref})\n\n![second]({logical_ref})"
        )

        updated = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=created.page_id,
                base_version_id=base_version.id,
                base_content_hash=base_version.content_hash,
                update_mode="full_rewrite",
                body_markdown=duplicate_body,
                reason="retain duplicate logical occurrences",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        usage = session.scalar(
            select(ContextMediaUsage).where(
                ContextMediaUsage.workspace_id == workspace.id,
                ContextMediaUsage.page_id == created.page_id,
                ContextMediaUsage.page_version_id == updated.draft_version_id,
                ContextMediaUsage.asset_id == asset.asset_id,
            )
        )
        stored = session.get(ContextPageVersion, updated.draft_version_id)

    assert stored is not None
    assert stored.body_md == duplicate_body
    assert usage is not None
    assert usage.reference_count == 2


def test_human_review_approval_publishes_logical_media_with_exact_usage(
    session_factory,
):
    service = ContextWriteService(
        context_media_repository_factory=SqlAlchemyContextMediaRepository
    )

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Review media publish",
                page_type="overview",
                body_markdown="# Review media publish\n\nSafe base.",
                reason="seed review media fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        base_version = session.get(ContextPageVersion, created.draft_version_id)
        assert base_version is not None
        page = session.get(ContextPage, created.page_id)
        editing_version = _open_editing_version(session, page)
        blob, inspection_id = media_fixtures._seed_shared_image(session, workspace.id)
        asset = RegisterMediaAsset().execute(
            SqlAlchemyContextMediaRepository(session),
            media_fixtures._candidate(
                workspace.id,
                created.page_id,
                editing_version.id,
                blob,
                inspection_id,
                ActorIdentity("human", ACTOR_ID),
            ),
            now=media_fixtures.NOW,
        )
        body = f"# Review media publish\n\n![reviewed]({asset.logical_reference})"
        updated = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=created.page_id,
                base_version_id=base_version.id,
                base_content_hash=base_version.content_hash,
                update_mode="full_rewrite",
                body_markdown=body,
                reason="publish reviewed logical media",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

        decision = ReviewService(
            context_media_repository_factory=SqlAlchemyContextMediaRepository
        ).apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=updated.review_item_id,
            request=ReviewDecisionRequest(decision_type="approve"),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        usage = session.scalar(
            select(ContextMediaUsage).where(
                ContextMediaUsage.workspace_id == workspace.id,
                ContextMediaUsage.page_id == created.page_id,
                ContextMediaUsage.page_version_id == decision.published_version_id,
                ContextMediaUsage.asset_id == asset.asset_id,
            )
        )

    assert decision.published_version_id == updated.draft_version_id
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
    assert result.latest_draft_version_id == result.draft_version_id
    assert result.current_published_version_id is None
    assert result.review_status == "pending"
    assert page.latest_draft_version_id == result.draft_version_id
    assert page.current_version_id == result.draft_version_id
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
    assert edge.target_kind == "library_item_version"
    assert edge.target_id == library_version.id


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


def test_update_draft_requires_latest_base_hash(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        target_page = ContextPage(
            workspace_id=workspace.id,
            slug="stale-target",
            title="Stale Target",
            page_type="overview",
        )
        session.add(target_page)
        session.flush()
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Stale Base Draft",
                page_type="overview",
                body_markdown="# Stale Base Draft\n\nFirst body.",
                reason="create initial draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        base = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=created.page_id,
                base_version_id=created.draft_version_id,
                base_content_hash=session.get(
                    ContextPage,
                    created.page_id,
                ).latest_draft_version.content_hash,
                update_mode="full_rewrite",
                body_markdown=(
                    "# Stale Base Draft\n\nSecond body links [[stale-target]]."
                ),
                reason="advance draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        with pytest.raises(StaleBaseVersionError):
            service.update_draft(
                session=session,
                workspace_id=workspace.id,
                request=UpdateDraftRequest(
                    page_id=created.page_id,
                    base_version_id=created.draft_version_id,
                    base_content_hash="0" * 64,
                    update_mode="full_rewrite",
                    body_markdown="# Stale Base Draft\n\nStale body.",
                    reason="stale update",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        page = session.get(ContextPage, created.page_id)
        version_count = len(page.versions)
        link = session.scalar(
            select(PageLink).where(PageLink.source_page_id == created.page_id)
        )

    assert base.draft_version_id == page.latest_draft_version_id
    assert version_count == 2
    assert link.source_version_id == base.draft_version_id
    assert link.target_slug == "stale-target"
    assert link.target_page_id == target_page.id


def test_update_draft_metadata_only_stale_write_conflicts_on_editing_revision(
    session_factory,
):
    """P2.4: base_content_hash alone cannot catch a metadata-only change.

    Two callers read the same version. One (say, browser) patches only the
    title -- body_md and content_hash never move, so it reuses the same
    version row (test_update_draft_metadata_patch_applies_on_publish already
    pins body_changed is False for this shape). A second caller (say, MCP)
    that read the version BEFORE that patch and still cites the same
    base_version_id/base_content_hash would pass the existing check and
    silently clobber the title. expected_editing_revision closes that gap.
    """

    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Original Title",
                page_type="overview",
                body_markdown="# Original Title\n\nBody.",
                reason="create draft for editing_revision conflict test",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        assert created.editing_revision == 1
        stale_revision = created.editing_revision

        first_write = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=created.page_id,
                base_version_id=created.draft_version_id,
                base_content_hash=created.persisted_content_hash,
                expected_editing_revision=stale_revision,
                update_mode="metadata_update",
                metadata_patch={"title": "Patched By Browser"},
                reason="browser patches title only",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        assert first_write.body_changed is False
        assert first_write.draft_version_id == created.draft_version_id
        assert first_write.editing_revision == stale_revision + 1

        # A second caller that read the version before the title patch still
        # has a valid base_version_id/base_content_hash (body never moved)
        # but a now-stale expected_editing_revision.
        with pytest.raises(StaleBaseVersionError):
            service.update_draft(
                session=session,
                workspace_id=workspace.id,
                request=UpdateDraftRequest(
                    page_id=created.page_id,
                    base_version_id=created.draft_version_id,
                    base_content_hash=created.persisted_content_hash,
                    expected_editing_revision=stale_revision,
                    update_mode="metadata_update",
                    metadata_patch={"title": "Clobbered By Stale MCP Write"},
                    reason="stale mcp write",
                ),
                actor_kind="mcp_client",
                actor_id=ACTOR_ID,
            )

        page = session.get(ContextPage, created.page_id)
        assert page.title == "Patched By Browser"

        # The same caller, having re-read the current revision, succeeds.
        second_write = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=created.page_id,
                base_version_id=created.draft_version_id,
                base_content_hash=created.persisted_content_hash,
                expected_editing_revision=first_write.editing_revision,
                update_mode="metadata_update",
                metadata_patch={"title": "Patched After Re-Read"},
                reason="caller re-reads then writes",
            ),
            actor_kind="mcp_client",
            actor_id=ACTOR_ID,
        )
        assert second_write.editing_revision == stale_revision + 2

    assert page.title == "Patched After Re-Read"


def test_patch_draft_resolves_fresh_head_for_sequential_non_overlapping_edits(
    session_factory,
):
    service = ContextWriteService()
    original_body = "# One\n\nfirst\n\n# Two\n\nsecond\n"

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = False
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Concurrent Section Edits",
                page_type="overview",
                body_markdown=original_body,
                reason="create concurrent edit fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        original_outline = parse_outline(original_body)
        one = next(
            section for section in original_outline.sections if section.title == "One"
        )
        two = next(
            section for section in original_outline.sections if section.title == "Two"
        )
        one_hash = section_content_hash(
            original_body[one.char_start : one.subtree_char_end]
        )
        two_hash = section_content_hash(
            original_body[two.char_start : two.subtree_char_end]
        )

        first = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_section",
                        "at": {"by": "heading_path", "heading_path": ["One"]},
                        "content": "# One\n\nfirst revised\n\n",
                        "expected_section_hash": one_hash,
                    }
                ],
                reason="first caller edits section one",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        second = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(first.draft_version_id),
                operations=[
                    {
                        "op": "replace_section",
                        "at": {"by": "heading_path", "heading_path": ["Two"]},
                        "content": "# Two\n\nsecond revised\n",
                        "expected_section_hash": two_hash,
                    }
                ],
                reason="second caller edits section two from the latest pointer",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        final_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert first.outcome == "updated_draft"
    assert second.outcome == "updated_draft"
    assert first.draft_version_id != second.draft_version_id
    assert "first revised" in final_version.body_md
    assert "second revised" in final_version.body_md


def test_patch_draft_replace_document_rejects_a_stale_base_instead_of_clobbering(
    session_factory,
):
    # A-3: replace_document had no content precondition at all -- unlike
    # every other operation -- so a second caller working from a stale read
    # could silently discard the first caller's concurrent edit. The second
    # patch here is computed from the pre-first-edit body, so it must be
    # refused rather than overwrite what the first patch just added.
    service = ContextWriteService()
    original_body = "# One\n\nfirst\n"
    original_hash = section_content_hash(original_body)

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = False
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Stale Replace Document",
                page_type="overview",
                body_markdown=original_body,
                reason="create stale-base fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

        first = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_document",
                        "content": "# One\n\nfirst caller's edit\n",
                        "expected_content_hash": original_hash,
                    }
                ],
                reason="first caller replaces the document",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        assert first.outcome == "updated_draft"

        with pytest.raises(StaleBaseVersionError) as exc_info:
            service.patch_draft(
                session=session,
                workspace_id=workspace.id,
                request=PatchDraftRequest(
                    page_id=created.page_id,
                    base=str(created.draft_version_id),
                    operations=[
                        {
                            "op": "replace_document",
                            "content": "# One\n\nsecond caller clobbers\n",
                            "expected_content_hash": original_hash,
                        }
                    ],
                    reason="second caller replaces from the same stale base",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )

        page = session.get(ContextPage, created.page_id)
        final_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert exc_info.value.error_code == "stale_base_version"
    assert "first caller's edit" in final_version.body_md
    assert "clobbers" not in final_version.body_md


def test_update_draft_stale_base_error_never_discloses_a_foreign_private_editing_row(
    session_factory,
):
    """P2.2/P2.6, MCP analog of the REST-side `_validate_base` masking:
    `update_draft`'s stale-base check resolves the current head via
    `_latest_edit_base` (`editable_context_version_id`, a page-global
    pointer that can durably name a DIFFERENT author's private "editing"
    row created by a wholly separate write path, e.g. REST's
    `begin_context_edit`/`autosave_page`). Before this fix, a
    `StaleBaseVersionError` raised here disclosed that row's exact
    `latest_version_id`/`latest_content_hash` to the calling MCP actor
    regardless of who owns it."""

    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Foreign Editing Row",
                page_type="overview",
                body_markdown="# Foreign Editing Row\n\nShared body.",
                reason="create draft for stale-base disclosure test",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(ContextPage, created.page_id)
        private_marker = "SECRET IN-PROGRESS EDIT NOBODY ELSE SHOULD SEE"
        private_body = f"# Foreign Editing Row\n\n{private_marker}\n"
        private_editing = context_page_repo.insert_version(
            session,
            page_id=page.id,
            parent_version_id=created.draft_version_id,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=private_body,
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-0000000000ee",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-0000000000ee",
            version_status="editing",
        )
        # Exactly what a real REST autosave by that other author would leave
        # behind (`page_service._set_current_version`) -- a wholly separate
        # write path can advance the same page-global pointers this legacy
        # MCP path also reads. `editable_context_version_id` prefers
        # `latest_draft_version_id`, so both must move for `_latest_edit_base`
        # to resolve to the private row.
        page.current_version_id = private_editing.id
        page.latest_draft_version_id = private_editing.id

        with pytest.raises(StaleBaseVersionError) as exc_info:
            service.update_draft(
                session=session,
                workspace_id=workspace.id,
                request=UpdateDraftRequest(
                    page_id=created.page_id,
                    base_version_id=created.draft_version_id,
                    base_content_hash=created.persisted_content_hash,
                    update_mode="metadata_update",
                    metadata_patch={"title": "Guess"},
                    reason="a different MCP actor's stale write",
                ),
                actor_kind="mcp_client",
                actor_id="some-other-caller",
            )

    assert private_marker not in str(exc_info.value)
    assert exc_info.value.latest_version_id is None
    assert exc_info.value.latest_content_hash is None
    assert exc_info.value.latest_version_id != private_editing.id
    assert exc_info.value.latest_content_hash != private_editing.content_hash


def test_patch_draft_risk_reflects_autofix_reformatting_an_untouched_section(
    session_factory,
):
    """B-1/D regression: risk used to be classified from the pre-auto-fix
    merge result, so a patch that only touched section B could get scored
    against a delta that ignored auto-fix reformatting section A's table --
    understating risk for a change that, once persisted, touched more than
    the caller asked for. `outside_target_changed`/`normalized` are the
    receipt signals that should have caught this and did not (D).

    Padding keeps the lone "original." -> "revised." edit itself small
    (context_partial_update on its own); only counting the table reformat
    auto-fix also applies pushes the measured delta over the small-edit
    threshold into context_full_rewrite -- proving the classifier used the
    POST-normalization body, not the pre-normalization one.
    """
    service = ContextWriteService()
    padding = ("x" * 60 + "\n") * 20
    original_body = (
        "# Doc\n\n## A\n\n| key | value |\n|-----|-------|\n| aaa | 111 |\n"
        "| bbb | 222 |\n\n" + padding + "## B\n\noriginal.\n"
    )

    with session_factory.begin() as session:
        workspace = _workspace(session)
        # Create with auto-fix OFF so the stored base keeps its unformatted
        # table; turn it on only for the patch, so normalization is
        # attributable to the patch under test, not to write_draft's own
        # auto-fix pass on creation.
        workspace.auto_fix_enabled = False
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Autofix Outside Target",
                page_type="overview",
                body_markdown=original_body,
                reason="create fixture with an unformatted table",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        workspace.auto_fix_enabled = True
        b_hash = section_content_hash(original_body[original_body.index("## B") :])

        result = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_section",
                        "at": {"by": "heading_path", "heading_path": ["Doc", "B"]},
                        "content": "## B\n\nrevised.\n",
                        "expected_section_hash": b_hash,
                    }
                ],
                reason="touch only section B",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        final_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    # Auto-fix realigned section A's table even though the operation only
    # targeted B.
    assert "| --- | ----- |" in final_version.body_md
    assert result.normalized is True
    assert result.outside_target_changed is True
    assert result.risk == "medium"


def test_patch_draft_dry_run_reports_the_same_classification_a_real_write_would(
    session_factory,
):
    """B-1: the dry_run branch normalizes separately from
    _persist_context_update, so it needs its own post-normalization
    reclassification -- otherwise a preview's risk disagrees with what
    persisting the identical request produces (see the non-dry-run sibling
    of this test, same fixture).
    """
    service = ContextWriteService()
    padding = ("x" * 60 + "\n") * 20
    original_body = (
        "# Doc\n\n## A\n\n| key | value |\n|-----|-------|\n| aaa | 111 |\n"
        "| bbb | 222 |\n\n" + padding + "## B\n\noriginal.\n"
    )

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = False
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Autofix Outside Target Dry Run",
                page_type="overview",
                body_markdown=original_body,
                reason="create fixture with an unformatted table",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        workspace.auto_fix_enabled = True
        b_hash = section_content_hash(original_body[original_body.index("## B") :])
        version_count_before = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )

        preview = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_section",
                        "at": {"by": "heading_path", "heading_path": ["Doc", "B"]},
                        "content": "## B\n\nrevised.\n",
                        "expected_section_hash": b_hash,
                    }
                ],
                reason="preview touching only section B",
                dry_run=True,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        version_count_after = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )

    assert version_count_after == version_count_before
    assert preview.normalized is True
    assert preview.outside_target_changed is True
    assert preview.risk == "medium"


@pytest.mark.parametrize("dry_run", [False, True])
def test_patch_draft_small_same_section_autofix_outside_target_raises_risk(
    session_factory,
    dry_run,
):
    """An outside-target reformat is a full-rewrite event even when it stays
    in one section and is too small to trip the character/ratio thresholds.

    The older regression above happened to become medium from its measured
    size and two touched sections, so it stayed green while the classifier's
    outside_target_changed input was hardcoded False.
    """

    service = ContextWriteService()
    padding = ("x" * 80 + "\n") * 30
    original_body = (
        "# Doc\n\n" + padding + "| key |value|\n|---|---|\n| a |1|\n\ntarget old\n"
    )

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = False
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title=f"Small Outside Target {dry_run}",
                page_type="overview",
                body_markdown=original_body,
                reason="create small-risk fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        workspace.auto_fix_enabled = True

        result = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_text",
                        "old_text": "target old",
                        "new_text": "target new",
                    }
                ],
                reason="change one target while Auto-fix formats its table",
                dry_run=dry_run,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

    assert result.normalized is True
    assert result.outside_target_changed is True
    assert result.risk == "medium"


def test_patch_draft_detects_autofix_outside_target_on_the_same_line(
    session_factory,
):
    """Target coordinates are characters, not whole lines. Replacing one
    phrase must not authorize Auto-fix to rewrite a list marker and trailing
    spaces elsewhere on that line without reporting the locality break.
    """

    service = ContextWriteService()
    padding = ("x" * 80 + "\n") * 20
    original_body = "# Doc\n\n" + padding + "* prefix target old suffix   \n"

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = False
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Same-line Outside Target",
                page_type="overview",
                body_markdown=original_body,
                reason="create exact-range fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        workspace.auto_fix_enabled = True

        result = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_text",
                        "old_text": "target old",
                        "new_text": "target new",
                    }
                ],
                reason="change only the target phrase",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

    assert result.normalized is True
    assert result.outside_target_changed is True
    assert result.risk == "medium"


@pytest.mark.parametrize("position", ["before", "after", "append_child"])
def test_patch_draft_insert_section_applies_across_every_position_under_autofix(
    session_factory,
    position,
):
    """insert_section always applies now -- a retry is replayed from the
    idempotency ledger one layer up (see backend/mcp/context_tools.py), so
    this engine never needs to recognize its own past output. Cover that the
    insert itself still lands correctly at every position under Auto-fix.
    """

    service = ContextWriteService()
    original_body = (
        "# Zero\n\nzero text\n\n# Parent\n\nparent text\n\n# Sibling\n\nsibling text\n"
    )

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = True
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title=f"Canonical Insert {position}",
                page_type="overview",
                body_markdown=original_body,
                reason="create insert fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        request = PatchDraftRequest(
            page_id=created.page_id,
            base=str(created.draft_version_id),
            operations=[
                {
                    "op": "insert_section",
                    "at": {"by": "heading_path", "heading_path": ["Parent"]},
                    "position": position,
                    "title": "Inserted",
                    # Auto-fix persists this as "- item".
                    "content": "* item",
                }
            ],
            reason="insert one section",
        )

        first = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=request,
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        final_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert first.outcome == "updated_draft"
    assert first.normalized is True
    assert first.outside_target_changed is False
    assert final_version.body_md.count("# Inserted") == 1
    assert "- item" in final_version.body_md


@pytest.mark.parametrize("auto_fix_enabled", [False, True], ids=["raw", "auto-fix"])
def test_patch_draft_section_receipt_hashes_match_the_landed_outline_and_round_trip(
    session_factory,
    auto_fix_enabled,
):
    """A section receipt is a usable CAS token for the body it describes.

    Cover preview + persistence, insert's natural retry, and replace's
    normalized no-op retry.  The authored list marker makes the raw and
    Auto-fix landing forms hash differently, so either branch selecting the
    wrong candidate fails before the follow-up replace/delete can run.
    """

    service = ContextWriteService()
    original_body = "# Parent\n\nparent text\n\n# Sibling\n\nsibling text\n"

    def receipt_hash(result):
        assert result.operation_receipts is not None
        (receipt,) = result.operation_receipts
        assert receipt.new_section_hash is not None
        return receipt.new_section_hash

    def landed_hash(body):
        inserted = next(
            section
            for section in parse_outline(body).sections
            if section.title == "Inserted"
        )
        return section_content_hash(
            body[inserted.char_start : inserted.subtree_char_end]
        )

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = auto_fix_enabled
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title=f"Section receipt round trip {auto_fix_enabled}",
                page_type="overview",
                body_markdown=original_body,
                reason="create section receipt fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        insert_operation = {
            "op": "insert_section",
            "at": {"by": "heading_path", "heading_path": ["Parent"]},
            "position": "append_child",
            "title": "Inserted",
            "content": "* item",
        }
        insert_preview = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[insert_operation],
                reason="preview inserted section",
                dry_run=True,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        inserted = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[insert_operation],
                reason="insert section",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        inserted_version = session.get(ContextPageVersion, page.latest_draft_version_id)
        inserted_landed_hash = landed_hash(inserted_version.body_md)

        assert insert_preview.draft_version_id is None
        assert receipt_hash(insert_preview) == inserted_landed_hash
        assert receipt_hash(inserted) == inserted_landed_hash

        replace_operation = {
            "op": "replace_section",
            "at": {
                "by": "heading_path",
                "heading_path": ["Parent", "Inserted"],
            },
            "content": "## Inserted\n\n* replaced\n\n### Nested\n\n+ nested",
            "expected_section_hash": receipt_hash(inserted),
        }
        replace_preview = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(inserted.draft_version_id),
                operations=[replace_operation],
                reason="preview replaced section",
                dry_run=True,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        replaced = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(inserted.draft_version_id),
                operations=[replace_operation],
                reason="replace inserted section",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        replaced_version = session.get(ContextPageVersion, page.latest_draft_version_id)
        replaced_landed_hash = landed_hash(replaced_version.body_md)

        assert receipt_hash(replace_preview) == replaced_landed_hash
        assert receipt_hash(replaced) == replaced_landed_hash

        replace_retry = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(replaced.draft_version_id),
                operations=[
                    {
                        **replace_operation,
                        "expected_section_hash": receipt_hash(replaced),
                    }
                ],
                reason="retry replaced section",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        assert replace_retry.outcome == "no_change"
        assert receipt_hash(replace_retry) == replaced_landed_hash

        deleted = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(replace_retry.draft_version_id),
                operations=[
                    {
                        "op": "delete_section",
                        "at": {
                            "by": "heading_path",
                            "heading_path": ["Parent", "Inserted"],
                        },
                        "expected_section_hash": receipt_hash(replace_retry),
                    }
                ],
                reason="delete replaced section using its receipt hash",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        deleted_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert deleted.outcome == "updated_draft"
    assert all(
        section.title != "Inserted"
        for section in parse_outline(deleted_version.body_md).sections
    )
    assert all(
        section.title != "Nested"
        for section in parse_outline(deleted_version.body_md).sections
    )


def test_replace_section_receipt_hashes_the_first_final_subtree_in_its_landed_span(
    session_factory,
):
    """A same-level heading ends the receipt's actual parser subtree."""

    service = ContextWriteService()
    original_body = "# Before\n\nbefore\n\n# Target\n\ntarget\n\n# After\n\nafter\n"

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = True
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Final outline receipt locator",
                page_type="overview",
                body_markdown=original_body,
                reason="create final-outline receipt fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        target = next(
            section
            for section in parse_outline(original_body).sections
            if section.title == "Target"
        )
        target_hash = section_content_hash(
            original_body[target.char_start : target.subtree_char_end]
        )
        replacement = (
            "# Reborn\n\n* target\n\n## Child\n\n+ nested\n\n# Peer\n\n* peer\n"
        )
        replaced = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_section",
                        "at": {"by": "heading_path", "heading_path": ["Target"]},
                        "content": replacement,
                        "expected_section_hash": target_hash,
                    }
                ],
                reason="replace with a nested section and a same-level peer",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        replaced_version = session.get(ContextPageVersion, page.latest_draft_version_id)
        replaced_outline = parse_outline(replaced_version.body_md)
        reborn = next(
            section
            for section in replaced_outline.sections
            if section.title == "Reborn"
        )
        reborn_hash = section_content_hash(
            replaced_version.body_md[reborn.char_start : reborn.subtree_char_end]
        )

        assert replaced.operation_receipts is not None
        (receipt,) = replaced.operation_receipts
        assert receipt.new_section_hash == reborn_hash
        assert receipt.new_section_hash != section_content_hash(replacement)
        assert "# Peer" in replaced_version.body_md

        deleted = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(replaced.draft_version_id),
                operations=[
                    {
                        "op": "delete_section",
                        "at": {"by": "heading_path", "heading_path": ["Reborn"]},
                        "expected_section_hash": receipt.new_section_hash,
                    }
                ],
                reason="delete only the first landed replacement section",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        deleted_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert deleted.outcome == "updated_draft"
    final_titles = {
        section.title for section in parse_outline(deleted_version.body_md).sections
    }
    assert "Reborn" not in final_titles
    assert "Child" not in final_titles
    assert "Peer" in final_titles


def test_replace_section_receipt_fails_closed_without_a_root_heading(
    session_factory,
):
    """Do not mislabel a later heading as the replaced section's root.

    In the middle of a document the leading delimiters are body syntax, not
    standalone frontmatter. The full-body normalizer still persists the edit,
    but there is no heading at the replacement root and therefore no honest
    new-section CAS token to return.
    """

    service = ContextWriteService()
    original_body = "# Before\n\nbefore\n\n# Target\n\ntarget\n\n# After\n\nafter\n"

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = True
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Fail-closed section receipt locator",
                page_type="overview",
                body_markdown=original_body,
                reason="create fail-closed receipt fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        target = next(
            section
            for section in parse_outline(original_body).sections
            if section.title == "Target"
        )
        target_hash = section_content_hash(
            original_body[target.char_start : target.subtree_char_end]
        )
        replaced = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_section",
                        "at": {"by": "heading_path", "heading_path": ["Target"]},
                        "content": (
                            "---\n* context-sensitive line\n---\n\n"
                            "# Later heading\n\ncontent\n"
                        ),
                        "expected_section_hash": target_hash,
                    }
                ],
                reason="replace without a root heading",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        replaced_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert replaced.outcome == "updated_draft"
    assert replaced.operation_receipts is not None
    (receipt,) = replaced.operation_receipts
    assert receipt.new_section_hash is None
    assert "- context-sensitive line" in replaced_version.body_md


def test_replace_section_receipt_survives_canonical_version_reuse(
    session_factory,
):
    """Dry-run and persist agree when NFC-equivalent content reuses a version."""

    service = ContextWriteService()
    original_body = "# Caf\u00e9\n\nbody\n"
    replacement = "# Cafe\u0301\n\nbody\n"

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = False
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Canonical section receipt",
                page_type="overview",
                body_markdown=original_body,
                reason="create canonical receipt fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        original_section = next(
            section
            for section in parse_outline(original_body).sections
            if section.kind == "heading"
        )
        original_hash = section_content_hash(
            original_body[
                original_section.char_start : original_section.subtree_char_end
            ]
        )
        operation = {
            "op": "replace_section",
            "at": {"by": "heading_path", "heading_path": ["Caf\u00e9"]},
            "content": replacement,
            "expected_section_hash": original_hash,
        }
        preview = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[operation],
                reason="preview canonically equivalent replacement",
                dry_run=True,
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        persisted = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[operation],
                reason="persist canonically equivalent replacement",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        final_version = session.get(ContextPageVersion, page.latest_draft_version_id)
        final_section = next(
            section
            for section in parse_outline(final_version.body_md).sections
            if section.kind == "heading"
        )
        final_hash = section_content_hash(
            final_version.body_md[
                final_section.char_start : final_section.subtree_char_end
            ]
        )

    assert preview.operation_receipts is not None
    assert persisted.operation_receipts is not None
    assert preview.operation_receipts[0].new_section_hash == final_hash
    assert persisted.operation_receipts[0].new_section_hash == final_hash
    assert persisted.draft_version_id == created.draft_version_id
    assert final_version.body_md == original_body


@pytest.mark.parametrize(
    "operation_kind",
    ["insert_text", "append_document", "insert_section"],
)
def test_insert_operations_always_apply_and_never_report_a_no_op(
    session_factory,
    operation_kind,
):
    """No insertion carries retry memory in the edit engine -- a same-key
    retry is replayed from the idempotency ledger one layer up (see
    backend/mcp/context_tools.py) before it ever reaches ContextWriteService.
    Calling the service directly with distinct requests, as this test does,
    must therefore insert every time, never recognize a prior insert."""

    service = ContextWriteService()
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
        workspace = _workspace(session)
        workspace.auto_fix_enabled = False
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title=f"Always-applies insert {operation_kind}",
                page_type="overview",
                body_markdown=original_body,
                reason="create always-applies insert fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

        def apply_once():
            page = session.get(ContextPage, created.page_id)
            return service.patch_draft(
                session=session,
                workspace_id=workspace.id,
                request=PatchDraftRequest(
                    page_id=created.page_id,
                    base=str(page.latest_draft_version_id),
                    operations=[operation],
                    reason="insert without a key",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )

        first = apply_once()
        second = apply_once()
        page = session.get(ContextPage, created.page_id)
        final_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert first.outcome == "updated_draft"
    assert second.outcome == "updated_draft"
    assert first.operation_receipts[0].outcome == "applied"
    assert second.operation_receipts[0].outcome == "applied"
    assert final_version.body_md.count("* item") == 2
    if operation_kind == "insert_section":
        assert (
            sum(
                section.title == "Inserted"
                for section in parse_outline(final_version.body_md).sections
            )
            == 2
        )


def test_delete_text_blank_cleanup_stays_targeted_under_fast_moving_policy(
    session_factory,
):
    service = ContextWriteService()
    stable = "Stable context remains unchanged. " * 20
    original_body = f"# Doc\n\n{stable}\n\nremove-me\n\nTail remains.\n"

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = True
        workspace.review_policy = _preset_policy("fast_moving")
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Targeted deletion cleanup",
                page_type="overview",
                body_markdown=original_body,
                reason="create targeted deletion fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

        result = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_text",
                        "old_text": "remove-me",
                        "new_text": "",
                    }
                ],
                reason="delete one targeted line",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        review = session.get(ReviewItem, result.review_item_id)

    assert result.normalized is True
    assert result.outside_target_changed is False
    assert result.risk == "low"
    assert review.risk_signals["reason_codes"] == ["default:context_partial_update:low"]


def test_delete_text_still_reports_unrelated_table_normalization(
    session_factory,
):
    service = ContextWriteService()
    original_body = (
        "# Doc\n\n"
        "| k | v |\n|---|---|\n| long value | 1 |\n\n"
        + ("Stable context remains unchanged. " * 20)
        + "\n\nremove-me\n\nTail remains.\n"
    )

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = False
        workspace.review_policy = _preset_policy("fast_moving")
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Deletion with outside normalization",
                page_type="overview",
                body_markdown=original_body,
                reason="create noncanonical table fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        workspace.auto_fix_enabled = True

        result = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_text",
                        "old_text": "remove-me",
                        "new_text": "",
                    }
                ],
                reason="delete while preserving outside-change detection",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        review = session.get(ReviewItem, result.review_item_id)

    assert result.normalized is True
    assert result.outside_target_changed is True
    assert result.risk == "medium"
    assert review.risk_signals["reason_codes"] == [
        "default:context_full_rewrite:medium"
    ]


def test_delete_influence_closure_never_absorbs_non_whitespace_insertions():
    before = "left DELETE right"
    provisional = "left  right"
    edit = ResolvedEdit(
        0,
        len(before),
        provisional,
        "replace_text",
        changed_ranges=((5, 11),),
    )

    assert (
        _body_changed_outside_edit_ranges(
            before,
            provisional,
            "left X right",
            (edit,),
        )
        is True
    )


def test_delete_influence_closure_excludes_previous_line_trailing_space_cleanup():
    before = "# H\noutside   \nremove\n\nTail\n"
    target_start = before.index("remove")
    provisional = before[:target_start] + before[target_start + len("remove") :]
    after = safe_normalize_markdown(provisional)
    edit = ResolvedEdit(
        0,
        len(before),
        provisional,
        "replace_text",
        changed_ranges=((target_start, target_start + len("remove")),),
    )

    outside = _body_changed_outside_edit_ranges(
        before,
        provisional,
        after,
        (edit,),
    )

    assert outside is True
    assert (
        _measure_update_event(before, after, outside_target_changed=outside)
        == "context_full_rewrite"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_delete_influence_closure_is_local_for_every_supported_newline(newline):
    before = newline.join(("# H", "remove", "", "Tail", ""))
    target_start = before.index("remove")
    provisional = before[:target_start] + before[target_start + len("remove") :]
    # Isolate deletion-seam cleanup from whole-document newline
    # canonicalization: only the newly adjacent blank line is removed.
    after = newline.join(("# H", "", "Tail", ""))
    edit = ResolvedEdit(
        0,
        len(before),
        provisional,
        "replace_text",
        changed_ranges=((target_start, target_start + len("remove")),),
    )

    assert (
        _body_changed_outside_edit_ranges(before, provisional, after, (edit,)) is False
    )


def test_repeated_large_body_delete_cleanup_stays_local_and_low_risk():
    repeated = "x\n" * 5_000
    before = repeated + "\ndelete\n\n" + repeated
    target_start = len(repeated) + 1
    provisional = before[:target_start] + before[target_start + len("delete") :]
    after = safe_normalize_markdown(provisional)
    edit = ResolvedEdit(
        0,
        len(before),
        provisional,
        "replace_text",
        changed_ranges=((target_start, target_start + len("delete")),),
    )

    outside = _body_changed_outside_edit_ranges(
        before,
        provisional,
        after,
        (edit,),
    )

    assert outside is False
    assert (
        _measure_update_event(before, after, outside_target_changed=outside)
        == "context_partial_update"
    )


def test_large_repeated_prefix_delete_cleanup_keeps_adjacent_blank_seam_local():
    before = "# H\n" + ("x\n" * 20_000) + "remove-me\n\nTail\n"
    target_start = before.index("remove-me")
    provisional = before[:target_start] + before[target_start + len("remove-me") :]
    after = safe_normalize_markdown(provisional)
    edit = ResolvedEdit(
        0,
        len(before),
        provisional,
        "replace_text",
        changed_ranges=((target_start, target_start + len("remove-me")),),
    )

    outside = _body_changed_outside_edit_ranges(
        before,
        provisional,
        after,
        (edit,),
    )

    assert outside is False
    assert (
        _measure_update_event(before, after, outside_target_changed=outside)
        == "context_partial_update"
    )


def test_patch_draft_unscoped_replace_text_reports_change_outside_the_match(
    session_factory,
):
    """outside_target_changed regression: _replace_text's ResolvedEdit range
    is the SEARCH SCOPE (the whole document when `at` is omitted), not the
    matched text -- treating that scope as "the target" pinned this signal to
    False for any unscoped replace_text, the most common operation. The real
    target is where old_text was actually found (ResolvedEdit.changed_ranges).
    """
    service = ContextWriteService()
    original_body = (
        "# Doc\n\n## A\n\n| k | v |\n|---|---|\n| a | 1 |\n\n## B\n\noriginal.\n"
    )

    with session_factory.begin() as session:
        workspace = _workspace(session)
        # Create with auto-fix OFF so the stored base keeps its unformatted
        # table; turn it on only for the patch (same setup as the
        # replace_section sibling test above).
        workspace.auto_fix_enabled = False
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Unscoped Replace Text Outside Target",
                page_type="overview",
                body_markdown=original_body,
                reason="create fixture with an unformatted table",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        workspace.auto_fix_enabled = True

        result = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_text",
                        "old_text": "original.",
                        "new_text": "revised.",
                    }
                ],
                reason="touch only the sentence in section B",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        final_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert "| --- | --- |" in final_version.body_md
    assert result.normalized is True
    assert result.outside_target_changed is True


def test_patch_draft_replace_text_reports_no_outside_change_when_autofix_is_a_no_op(
    session_factory,
):
    """Negative case for the fix above: an unscoped replace_text on an
    already-canonical body must still report outside_target_changed=False --
    the fix must narrow the blind spot, not flip it to permanently True.
    """
    service = ContextWriteService()
    original_body = "# Doc\n\n## A\n\nalready clean\n\n## B\n\noriginal.\n"

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = True
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Replace Text No Outside Change",
                page_type="overview",
                body_markdown=original_body,
                reason="create clean fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

        result = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                operations=[
                    {
                        "op": "replace_text",
                        "old_text": "original.",
                        "new_text": "revised.",
                    }
                ],
                reason="touch only the sentence in section B",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

    assert result.normalized is False
    assert result.outside_target_changed is False


def test_patch_draft_honors_needs_review_even_when_every_operation_is_a_no_op(
    session_factory,
):
    """B-2: `_patch_draft_once` used to short-circuit to `_unchanged_update_
    result` (no change_set, no review) whenever every operation resolved to
    a no-op, unconditionally -- even when the caller explicitly set
    needs_review=True. That silently dropped a review request update_draft's
    own identical-body path already knows how to honor (the `not
    needs_review` guard in `_persist_context_update`'s Auto-fix no-op
    shortcut). patch_draft must agree with it, not invent a second rule.
    """
    service = ContextWriteService()
    body = "# One\n\ntext\n"

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = False
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="No-op Needs Review",
                page_type="overview",
                body_markdown=body,
                reason="create fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        version_count_before = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )

        result = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=PatchDraftRequest(
                page_id=created.page_id,
                base=str(created.draft_version_id),
                # old_text == new_text: every operation resolves to a genuine
                # no-op (ResolvedEdit.no_op is True), the same as it would for
                # an idempotent retry.
                operations=[
                    {"op": "replace_text", "old_text": "text", "new_text": "text"}
                ],
                reason="no-op edit, but flag it for review",
                needs_review=True,
                idempotency_key="b2-noop-needs-review",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        version_count_after = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )

    # A pending review must exist -- the whole point of needs_review=True --
    # regardless of exactly how the no-op is represented as a version.
    assert result.review_item_id is not None
    assert result.change_set_id is not None
    assert version_count_after >= version_count_before


def test_patch_draft_idempotency_key_replays_self_containing_replace_once(
    session_factory,
    monkeypatch,
):
    from backend.services import context_write_service

    real_apply_operations = context_write_service.apply_operations_with_block_map
    engine_calls = 0

    def counted_apply_operations(*args, **kwargs):
        nonlocal engine_calls
        engine_calls += 1
        return real_apply_operations(*args, **kwargs)

    monkeypatch.setattr(
        context_write_service,
        "apply_operations_with_block_map",
        counted_apply_operations,
    )
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Patch Idempotency",
                page_type="overview",
                body_markdown="# Patch Idempotency\n\nold value\n",
                reason="create patch idempotency fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        request = PatchDraftRequest(
            page_id=created.page_id,
            base=str(created.draft_version_id),
            operations=[
                {
                    "op": "replace_text",
                    "old_text": "old",
                    "new_text": "old-new",
                }
            ],
            reason="apply self-containing replacement once",
            idempotency_key="patch-self-containing-replace",
        )
        first = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=request,
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        replay = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=request,
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        final_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert first.push_outcome == "committed"
    assert replay.model_copy(update={"push_outcome": "committed"}) == first
    assert replay.push_outcome == "replayed"
    assert engine_calls == 1
    assert final_version.body_md.count("old-new") == 1
    assert "old-new-new" not in final_version.body_md


def test_patch_draft_distinct_keys_apply_intentional_repeat(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = False
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Patch Intentional Repeat",
                page_type="overview",
                body_markdown="# Keyed Repeat\n\nanchor\n",
                reason="create intentional repeat fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

        def apply_with_key(key: str):
            page = session.get(ContextPage, created.page_id)
            return service.patch_draft(
                session=session,
                workspace_id=workspace.id,
                request=PatchDraftRequest(
                    page_id=created.page_id,
                    base=str(page.latest_draft_version_id),
                    operations=[{"op": "append_document", "content": "repeat-token"}],
                    reason="append one intentional repeat",
                    idempotency_key=key,
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )

        first = apply_with_key("intentional-repeat-1")
        second = apply_with_key("intentional-repeat-2")
        page = session.get(ContextPage, created.page_id)
        final_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert first.outcome == "updated_draft"
    assert second.outcome == "updated_draft"
    assert first.draft_version_id != second.draft_version_id
    assert first.operation_receipts[0].outcome == "applied"
    assert second.operation_receipts[0].outcome == "applied"
    assert first.block_map_delta is not None
    assert second.block_map_delta is not None
    assert any(entry.change == "added" for entry in first.block_map_delta)
    assert any(entry.change == "added" for entry in second.block_map_delta)
    assert final_version.body_md.count("repeat-token") == 2


def test_patch_draft_distinct_keys_repeat_equivalent_table_block(session_factory):
    service = ContextWriteService()
    body = "| A | B |\n| --- | --- |\n| old | anchor |\n"
    anchor_row = "| old | anchor |"
    wide_value = "x" * 500
    operation = {
        "op": "insert_text",
        "at": {
            "by": "anchor",
            "anchor_text": anchor_row,
            "char_offset_hint": body.index(anchor_row),
        },
        "new_text": f"\n| H2 | V2 |\n| --- | --- |\n| {wide_value} | y |",
        "side": "after",
    }

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = True
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Patch Intentional Table Repeat",
                page_type="overview",
                body_markdown=body,
                reason="create intentional table repeat fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

        results = []
        for index in range(2):
            page = session.get(ContextPage, created.page_id)
            results.append(
                service.patch_draft(
                    session=session,
                    workspace_id=workspace.id,
                    request=PatchDraftRequest(
                        page_id=created.page_id,
                        base=str(page.latest_draft_version_id),
                        operations=[operation],
                        reason="repeat equivalent table with a distinct key",
                        idempotency_key=f"intentional-table-repeat-{index}",
                    ),
                    actor_kind="human",
                    actor_id=ACTOR_ID,
                )
            )
        page = session.get(ContextPage, created.page_id)
        final_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert all(result.outcome == "updated_draft" for result in results)
    assert all(result.operation_receipts[0].outcome == "applied" for result in results)
    assert final_version.body_md.count("H2") == 2
    assert final_version.body_md.count(wide_value) == 2


def test_keyed_insert_near_a_table_replays_from_the_ledger_without_a_second_version(
    session_factory,
):
    """A same-key retry is replayed from the idempotency ledger before it
    reaches the edit engine (backend/services/mcp_write_idempotency.py), so
    it never creates a second version or re-derives the insert's outcome."""

    service = ContextWriteService()
    body = "# Ambiguous\n\n| key   | value |\n| ----- | ----- |\n| alpha | beta  |\n"

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = True
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Ambiguous Insert Key",
                page_type="overview",
                body_markdown=body,
                reason="create ambiguous insert fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        operation = {
            "op": "insert_text",
            "at": {
                "by": "anchor",
                "anchor_text": "alpha",
                "char_offset_hint": body.index("alpha"),
            },
            "new_text": " gamma  ",
            "side": "after",
        }
        versions_before = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )

        keyed_request = PatchDraftRequest(
            page_id=created.page_id,
            base=str(created.draft_version_id),
            operations=[operation],
            reason="apply insert near a table with a key",
            idempotency_key="ambiguous-table-insert-1",
        )
        first = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=keyed_request,
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        versions_after_first = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )
        replay = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=keyed_request,
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        versions_after_replay = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )
        page = session.get(ContextPage, created.page_id)
        final_version = session.get(ContextPageVersion, page.latest_draft_version_id)

    assert first.push_outcome == "committed"
    assert replay.model_copy(update={"push_outcome": "committed"}) == first
    assert replay.push_outcome == "replayed"
    assert first.operation_receipts[0].outcome == "applied"
    assert final_version.body_md.count("gamma") == 1
    assert versions_after_first == versions_before + 1
    assert versions_after_replay == versions_after_first


def test_patch_draft_dry_run_returns_preview_without_persistence(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        workspace.auto_fix_enabled = True
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Patch Dry Run",
                page_type="overview",
                body_markdown="# Patch Dry Run\n\noriginal\ncontinued\n",
                reason="create dry-run fixture",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        version_count = session.scalar(
            select(func.count()).select_from(ContextPageVersion)
        )
        change_count = session.scalar(select(func.count()).select_from(ChangeSet))
        review_count = session.scalar(select(func.count()).select_from(ReviewItem))
        # Canonical hashes ignore this semantic hard break. Preview and save
        # must still recognize and preserve its exact authored bytes.
        preview_body = "# Patch Dry Run\n\noriginal  \ncontinued\n"
        original_body = "# Patch Dry Run\n\noriginal\ncontinued\n"
        assert calculate_content_hash(preview_body) == calculate_content_hash(
            original_body
        )
        request = PatchDraftRequest(
            page_id=created.page_id,
            base=str(created.draft_version_id),
            operations=[
                {
                    "op": "replace_document",
                    "content": preview_body,
                    "expected_content_hash": section_content_hash(original_body),
                }
            ],
            reason="preview replacement without writing",
            dry_run=True,
        )

        preview = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=request,
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)

        assert (
            session.scalar(select(func.count()).select_from(ContextPageVersion))
            == version_count
        )
        assert (
            session.scalar(select(func.count()).select_from(ChangeSet)) == change_count
        )
        assert (
            session.scalar(select(func.count()).select_from(ReviewItem)) == review_count
        )
        assert page.latest_draft_version_id == created.draft_version_id
        assert preview.outcome == "updated_draft"
        saved = service.patch_draft(
            session=session,
            workspace_id=workspace.id,
            request=request.model_copy(update={"dry_run": False}),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        stored = session.get(ContextPageVersion, saved.draft_version_id)
        assert stored.body_md == preview.preview_body_markdown == preview_body
        assert saved.body_changed is True
        assert saved.draft_version_id != created.draft_version_id

    assert preview.body_changed is True
    assert preview.persisted_content_hash == calculate_content_hash(preview_body)


def test_draft_update_preserves_published_pagelinks_until_publish(session_factory):
    service = ContextWriteService()

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
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Published Link Source",
                page_type="overview",
                body_markdown="# Published Link Source\n\nLive [[published-target]].",
                reason="create publishable source",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        _approve(session, workspace.id, created.review_item_id)
        published = service.publish_draft(
            session=session,
            workspace_id=workspace.id,
            page_id=created.page_id,
            request=PublishContextPageRequest(
                version_id=created.draft_version_id,
                reason="publish initial source",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        published_link = session.scalar(
            select(PageLink).where(PageLink.source_page_id == page.id)
        )

        draft = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=page.id,
                base_version_id=published.published_version_id,
                base_content_hash=session.get(
                    ContextPageVersion, published.published_version_id
                ).content_hash,
                update_mode="full_rewrite",
                body_markdown="# Published Link Source\n\nDraft [[draft-target]].",
                reason="draft new links",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        draft_pending_link = session.scalar(
            select(PageLink).where(PageLink.source_page_id == page.id)
        )
        _approve(session, workspace.id, draft.review_item_id)
        republished = service.publish_draft(
            session=session,
            workspace_id=workspace.id,
            page_id=page.id,
            request=PublishContextPageRequest(
                version_id=draft.draft_version_id,
                reason="publish changed links",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        republished_link = session.scalar(
            select(PageLink).where(PageLink.source_page_id == page.id)
        )

    assert published_link.source_version_id == published.published_version_id
    assert published_link.target_slug == "published-target"
    assert draft_pending_link.source_version_id == published.published_version_id
    assert draft_pending_link.target_slug == "published-target"
    assert republished_link.source_version_id == republished.published_version_id
    assert republished_link.target_slug == "draft-target"
    assert republished_link.target_page_id == new_target.id


def test_update_draft_appends_and_links_feedback(
    session_factory, media_installer_factory
):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        library_item_version, _artifact = _library_item_ref(
            session, workspace.id, media_installer_factory
        )
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Append Draft",
                page_type="overview",
                body_markdown="# Append Draft\n\nOriginal body.",
                reason="create draft for append",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(ContextPage, created.page_id)
        base_version = page.latest_draft_version
        result = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=page.id,
                base_version_id=base_version.id,
                base_content_hash=base_version.content_hash,
                update_mode="append_section",
                body_markdown="## Added\n\nCited addition.",
                citations=[
                    CitationInput(
                        citation_type="raw_source",
                        library_item_version_id=library_item_version.id,
                        snippet="Verified source text.",
                        created_via="manual_input",
                    )
                ],
                reason="append cited section",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        updated_version = session.get(ContextPageVersion, result.draft_version_id)
        change_set = session.get(ChangeSet, result.change_set_id)
        operation = session.scalar(
            select(ChangeOperation).where(
                ChangeOperation.change_set_id == result.change_set_id
            )
        )
        citation = session.scalar(
            select(ContextCitation).where(
                ContextCitation.target_id.in_(
                    select(ContextClaim.id).where(
                        ContextClaim.page_version_id == updated_version.id
                    )
                )
            )
        )

    assert "## Added" in updated_version.body_markdown
    assert updated_version.parent_version_id == base_version.id
    assert operation.operation == "append_section"
    assert result.review_item_id is not None
    assert change_set.cite_call_skipped is True
    assert citation.created_via == "manual_input"


# D-3: the risk event `update_draft` measures must reflect the ACTUAL change,
# not the caller-declared `update_mode` string. `_body_for_update` treats
# every non-append mode (including `replace_section`) as a whole-body
# assignment, so the mode string alone can't distinguish a one-line tweak
# from a whole-document replace.


def test_update_draft_measures_whole_document_replace_as_full_rewrite_risk(
    session_factory,
):
    """A caller that declares `replace_section` but actually replaces the
    entire document (which is what `_body_for_update` does for that mode --
    it has no section-scoping of its own) must be risk-classified the same
    as `full_rewrite`, not as the low-risk `context_partial_update` the mode
    string used to imply.
    """
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Replace Section Whole Doc",
                page_type="overview",
                body_markdown="# Replace Section Whole Doc\n\nOriginal body.",
                reason="create draft for replace_section measurement",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(ContextPage, created.page_id)
        base_version = page.latest_draft_version
        replacement_body = "# Replace Section Whole Doc\n\n" + (
            "Completely different content. " * 100
        )
        result = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=page.id,
                base_version_id=base_version.id,
                base_content_hash=base_version.content_hash,
                update_mode="full_rewrite",
                body_markdown=replacement_body,
                reason="whole-document replace measured end to end",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        review = session.get(ReviewItem, result.review_item_id)

    # This originally declared `replace_section` to prove that a mode claiming to
    # be partial still measured as a full rewrite. D-1 removed that mode -- it was
    # the only one that could lie this way -- so the scenario is now unreachable by
    # construction rather than caught at classification time. The schema-level
    # rejection is covered by
    # test_replace_section_update_mode_is_rejected_with_actionable_message.
    #
    # What remains worth asserting end to end is the other half: the measured
    # classifier reaches `context_full_rewrite` through its NON-append branch on a
    # real service call, not just in unit tests.
    assert review.risk_level == "medium"
    assert (
        review.risk_signals["reason_codes"][0] == "default:context_full_rewrite:medium"
    )


def test_update_draft_small_append_stays_low_risk(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Small Append",
                page_type="overview",
                body_markdown="# Small Append\n\nOriginal body.",
                reason="create draft for small append measurement",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(ContextPage, created.page_id)
        base_version = page.latest_draft_version
        result = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=page.id,
                base_version_id=base_version.id,
                base_content_hash=base_version.content_hash,
                update_mode="append_section",
                body_markdown="## Added\n\nA short addition.",
                reason="small append under the strict-append threshold",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        review = session.get(ReviewItem, result.review_item_id)

    assert review.risk_level == "low"
    assert review.risk_signals["reason_codes"] == ["default:context_partial_update:low"]


def test_update_draft_large_append_over_strict_threshold_becomes_medium(
    session_factory,
):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Large Append",
                page_type="overview",
                body_markdown="# Large Append\n\nOriginal body.",
                reason="create draft for large append measurement",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(ContextPage, created.page_id)
        base_version = page.latest_draft_version
        large_addition = "## Added\n\n" + ("x" * 8300)
        result = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=page.id,
                base_version_id=base_version.id,
                base_content_hash=base_version.content_hash,
                update_mode="append_section",
                body_markdown=large_addition,
                reason="append past the strict-append character threshold",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        review = session.get(ReviewItem, result.review_item_id)

    assert review.risk_level == "medium"
    assert (
        review.risk_signals["reason_codes"][0] == "default:context_full_rewrite:medium"
    )


def test_update_draft_ratchet_keeps_tiny_full_rewrite_at_declared_severity(
    session_factory,
):
    """D-3 Task 2 ratchet: a small in-place edit submitted through
    `full_rewrite` measures, on its own, as a `context_partial_update` (it
    satisfies the small-edit whitelist). But the declared mode's own
    severity is a FLOOR -- resolving it must not drop below what
    `full_rewrite` already cost under this workspace's policy, or a
    `fast_moving` workspace would gain a brand-new auto-approve path in the
    very commit that closes the old one.
    """
    service = ContextWriteService()
    filler = "Stable status line about routine operations that does not change. " * 20

    with session_factory.begin() as session:
        workspace = _workspace(session)
        before_body = f"# Report\n\n{filler}\n\nStatus: initial.\n\n{filler}"
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Tiny Full Rewrite",
                page_type="overview",
                body_markdown=before_body,
                reason="create draft for ratchet measurement",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(ContextPage, created.page_id)
        base_version = page.latest_draft_version
        after_body = f"# Report\n\n{filler}\n\nStatus: revised.\n\n{filler}"
        result = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=page.id,
                base_version_id=base_version.id,
                base_content_hash=base_version.content_hash,
                update_mode="full_rewrite",
                body_markdown=after_body,
                reason="one-line change declared as full_rewrite",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        review = session.get(ReviewItem, result.review_item_id)

    assert review.risk_level == "medium"
    assert review.risk_signals["reason_codes"] == [
        "default:context_partial_update:low",
        "floor:medium",
    ]


def test_update_draft_rejects_append_that_pushes_merged_body_past_max_length(
    session_factory,
):
    """D-5: `BODY_MD_MAX_LENGTH` is enforced by the schema only on the INPUT
    field, so `append_section` can merge a valid base plus a valid addition
    into a persisted body that exceeds the limit -- the merged result must
    be re-checked after the merge, not just its ingredients beforehand.
    """
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        near_limit_body = "# Near Limit\n\n" + ("a" * (BODY_MD_MAX_LENGTH - 100))
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Near Limit",
                page_type="overview",
                body_markdown=near_limit_body,
                reason="create draft near the body length limit",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(ContextPage, created.page_id)
        base_version = page.latest_draft_version

        with pytest.raises(ReadValidationError) as exc_info:
            service.update_draft(
                session=session,
                workspace_id=workspace.id,
                request=UpdateDraftRequest(
                    page_id=page.id,
                    base_version_id=base_version.id,
                    base_content_hash=base_version.content_hash,
                    update_mode="append_section",
                    body_markdown="## Added\n\n" + ("b" * 200),
                    reason="append that pushes the merged body over the limit",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )

    assert exc_info.value.error_code == "body_length_exceeded"


def test_write_draft_and_update_draft_set_decision_summary_code(
    session_factory, media_installer_factory
):
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

        # update_draft, no citation warnings: the fixed fallback copy gets a
        # code.
        no_notes_page = session.get(ContextPage, no_notes.page_id)
        clean_update = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=no_notes_page.id,
                base_version_id=no_notes.draft_version_id,
                base_content_hash=no_notes_page.latest_draft_version.content_hash,
                update_mode="full_rewrite",
                body_markdown="# Decision Summary Code No Notes\n\nRevised body.",
                reason="revise without citation warnings",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        clean_update_review = session.get(ReviewItem, clean_update.review_item_id)

        # update_draft, with a manual_input citation: `_workflow_warnings`
        # returns exactly `["workflow_sequence_incomplete"]` here (its only
        # non-empty return value), so this branch is just as codeable as the
        # empty-warnings one -- never free text.
        library_item_version, _artifact = _library_item_ref(
            session, workspace.id, media_installer_factory
        )
        with_notes_page = session.get(ContextPage, with_notes.page_id)
        warned_update = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=with_notes_page.id,
                base_version_id=with_notes.draft_version_id,
                base_content_hash=with_notes_page.latest_draft_version.content_hash,
                update_mode="append_section",
                body_markdown="## Added\n\nManually cited addition.",
                citations=[
                    CitationInput(
                        citation_type="raw_source",
                        library_item_version_id=library_item_version.id,
                        snippet="Verified source text.",
                        created_via="manual_input",
                    )
                ],
                reason="revise with a manual citation warning",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        warned_update_review = session.get(ReviewItem, warned_update.review_item_id)

    assert no_notes_review.decision_summary == "context draft requires review"
    assert no_notes_review.decision_summary_code == "context_draft_requires_review"

    assert with_notes_review.decision_summary == "flagging this for a second look"
    assert with_notes_review.decision_summary_code is None

    assert (
        clean_update_review.decision_summary == "context update draft requires review"
    )
    assert clean_update_review.decision_summary_code == "context_update_requires_review"

    assert warned_update_review.decision_summary == "workflow_sequence_incomplete"
    assert (
        warned_update_review.decision_summary_code
        == "context_update_workflow_sequence_incomplete"
    )


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
    assert second.persisted_character_count == len(second_version.body_md)
    assert page_type_before_publish == "overview"
    assert page_type_after_publish == "guide"


def test_update_draft_metadata_patch_applies_on_publish(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Patch Target",
                page_type="overview",
                body_markdown="# Patch Target\n\nBody.",
                reason="create draft for metadata patch",
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
                reason="publish metadata patch base",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        base_version = page.current_published_version
        result = service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=page.id,
                base_version_id=base_version.id,
                base_content_hash=base_version.content_hash,
                update_mode="metadata_update",
                metadata_patch={
                    "title": "Patched Title",
                    "page_type": "concept",
                    "tags": ["patched"],
                    "aliases": ["Patched Alias", "patched alias"],
                },
                reason="patch metadata",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        assert result.persisted_content_hash == base_version.content_hash
        assert result.persisted_character_count == len(base_version.body_md)
        assert result.outcome == "updated_draft"
        assert result.body_changed is False
        assert result.metadata_changed is True
        assert result.version_status == "draft"
        assert result.latest_draft_version_id == result.draft_version_id
        assert result.current_published_version_id == base_version.id
        assert result.review_status == "pending"
        title_before_publish = page.title
        page_type_before_publish = page.page_type
        tags_before_publish = list(page.tags or [])
        aliases_before_publish = list(page.aliases or [])
        _approve(session, workspace.id, result.review_item_id)
        service.publish_draft(
            session=session,
            workspace_id=workspace.id,
            page_id=page.id,
            request=PublishContextPageRequest(
                version_id=result.draft_version_id,
                reason="publish patched draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        title_after_publish = page.title
        page_type_after_publish = page.page_type
        tags_after_publish = list(page.tags or [])
        aliases_after_publish = list(page.aliases or [])
        tree_node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == page.id,
                WorkspaceTreeNode.primary_placement.is_(True),
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )

    assert title_before_publish == "Patch Target"
    assert page_type_before_publish == "overview"
    assert tags_before_publish == []
    assert aliases_before_publish == []
    assert title_after_publish == "Patched Title"
    assert page_type_after_publish == "concept"
    assert tags_after_publish == ["patched"]
    # P2.3: renaming no longer auto-preserves the retired title ("Patch
    # Target") as an alias -- only the explicitly requested alias survives.
    assert aliases_after_publish == ["Patched Alias"]
    assert tree_node.title == "Patched Title"


def test_never_published_draft_metadata_applies_immediately(session_factory):
    service = ContextWriteService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Unpublished Metadata Page",
                page_type="overview",
                body_markdown="# Unpublished Metadata Page\n\nBody.",
                reason="create unpublished metadata page",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        page = session.get(ContextPage, created.page_id)
        base_version = page.latest_draft_version
        service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=page.id,
                base_version_id=base_version.id,
                base_content_hash=base_version.content_hash,
                update_mode="metadata_update",
                metadata_patch={
                    "title": "Immediate Draft Title",
                    "aliases": ["Immediate Alias"],
                },
                reason="update unpublished metadata",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        metadata_operations = session.scalars(
            select(ChangeOperation)
            .where(ChangeOperation.target_id == page.id)
            .where(ChangeOperation.operation == "metadata_patch")
        ).all()

    assert page.title == "Immediate Draft Title"
    # P2.3: renaming no longer auto-preserves the retired title
    # ("Unpublished Metadata Page") as an alias.
    assert page.aliases == ["Immediate Alias"]
    assert metadata_operations == []


def test_update_draft_metadata_patch_reject_leaves_page_untouched(session_factory):
    write_service = ContextWriteService()
    review_service = ReviewService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = write_service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Reject Patch Target",
                page_type="overview",
                body_markdown="# Reject Patch Target\n\nBody.",
                reason="create draft for rejection",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        _approve(session, workspace.id, created.review_item_id)
        write_service.publish_draft(
            session=session,
            workspace_id=workspace.id,
            page_id=created.page_id,
            request=PublishContextPageRequest(
                version_id=created.draft_version_id,
                reason="publish initial draft",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        page = session.get(ContextPage, created.page_id)
        published_version = session.get(
            ContextPageVersion, page.current_published_version_id
        )
        result = write_service.update_draft(
            session=session,
            workspace_id=workspace.id,
            request=UpdateDraftRequest(
                page_id=page.id,
                base_version_id=published_version.id,
                base_content_hash=published_version.content_hash,
                update_mode="metadata_update",
                metadata_patch={
                    "title": "Rejected Title",
                    "page_type": "concept",
                    "tags": ["rejected"],
                },
                reason="patch metadata before rejection",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        review_service.apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=result.review_item_id,
            request=ReviewDecisionRequest(decision_type="reject"),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        draft_version = session.get(ContextPageVersion, result.draft_version_id)
        title_after_reject = page.title
        page_type_after_reject = page.page_type
        tags_after_reject = list(page.tags or [])
        published_after_reject = page.current_published_version_id

    assert result.draft_version_id != published_version.id
    assert draft_version.version_status == "draft"
    assert title_after_reject == "Reject Patch Target"
    assert page_type_after_reject == "overview"
    assert tags_after_reject == []
    assert published_after_reject == published_version.id


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


def test_publish_draft_refuses_rejected_review(session_factory):
    write_service = ContextWriteService()
    review_service = ReviewService()

    with session_factory.begin() as session:
        workspace = _workspace(session)
        created = write_service.write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title="Rejected Publish Target",
                page_type="overview",
                body_markdown="# Rejected Publish Target\n\nBody.",
                reason="create draft for rejection",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )
        review_service.apply_decision(
            session=session,
            workspace_id=workspace.id,
            review_item_id=created.review_item_id,
            request=ReviewDecisionRequest(decision_type="reject"),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        with pytest.raises(RejectedByPolicyError):
            write_service.publish_draft(
                session=session,
                workspace_id=workspace.id,
                page_id=created.page_id,
                request=PublishContextPageRequest(
                    version_id=created.draft_version_id,
                    reason="publish rejected draft",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
            )
        page = session.get(ContextPage, created.page_id)
        version = session.get(ContextPageVersion, created.draft_version_id)
        published_id = page.current_published_version_id
        version_status = version.version_status

    assert published_id is None
    assert version_status == "draft"


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
        assert page.latest_draft_version_id == legacy_draft.id
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
