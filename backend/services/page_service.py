from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import json
import re
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.orm import Session, aliased

from backend.context_media.application import SyncVersionUsage
from backend.context_media.domain import (
    ContextMediaError,
    MarkdownMediaPolicyError,
    validate_markdown_media_policy,
)
from backend.context_media.ports import ContextMediaRepository
from backend.domain.input_policy import validate_body, validate_text
from backend.domain.prepared_body import PreparedBody, PreparedMetadata
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import (
    ContextPage,
    ContextPageVersion,
    PageLink,
    PageNameIndex,
)
from backend.models.feedback import FeedbackItem
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.policy.trust_boundaries import actor_can_read_drafts
from backend.repositories import context_page_repo, workspace_repo
from backend.repositories.workspace_scoped import (
    delete_in_workspace,
    get_in_workspace,
    join_in_workspace,
    select_in_workspace,
    select_projection_in_workspace,
)
from backend.schemas.common import DraftReviewReason
from backend.schemas.context_edit import AnchorAddress, EditOperation
from backend.schemas.page import (
    PAGE_LIST_CURSOR_MAX_OFFSET,
    PAGE_LIST_LIMIT_MAX,
    DiscardContextEditResult,
    ListMyContextEditsResult,
    MyContextEditSummary,
    PageBacklink,
    PageInlineComment,
    PageInlineCommentAnchor,
    PagePayload,
    PageSummary,
    PageVersionDiff,
    PageVersionEntry,
    PublicPagePayload,
)
from backend.services.actor_visible_read import (
    PUBLISHED_CONTEXT_VERSION_STATUSES,
    context_parent_projection,
    context_version_is_visible,
)
from backend.services.anchor_resolver import (
    AnchorStatus,
    ResolvedAnchor,
    resolve_anchor,
)
from backend.services.audit_service import AuditService
from backend.services.body_preparer import (
    BodyInput,
    BodyPreparer,
    PageMetadataInput,
    WriteContext,
)
from backend.services.change_review_audit import ChangeReviewAuditRecorder
from backend.services.context_editing import (
    ContextEditError,
    apply_operations_with_block_map,
    block_operation_provenance,
)
from backend.services.context_lifecycle import (
    project_context_version,
    record_context_decision,
    record_metadata_snapshot,
)
from backend.services.context_link_targets import context_link_targets
from backend.services.context_media_legacy_cutover import (
    LegacyMediaCutoverConflictError,
    require_no_visible_legacy_media,
)
from backend.services.context_push import target_ids_for_operations
from backend.services.context_review_disposition import (
    context_version_review_metadata,
)
from backend.services.context_version_storage import ContextStorageServiceError
from backend.services.context_visibility import (
    editable_context_version_id,
    visible_context_version_expression,
    visible_context_version_id,
)
from backend.services.db_write_intent import acquire_write_intent
from backend.services.edit_receipt import (
    build_edit_receipt,
    patch_operation_receipts,
)
from backend.services.entity_ref import normalize_entity_name
from backend.services.job_queue import (
    JobQueueService,
    context_page_embed_idempotency_key,
)
from backend.services.lint import LintRegistry, publish_floor_findings
from backend.services.lint.rules_format import (
    body_relative_message,
)
from backend.services.mcp_write_idempotency import (
    execute_idempotent_write,
    request_fingerprint,
)
from backend.services.page_link_ordering import page_link_snippet_order
from backend.services.public_page_exposure import (
    clear_public_page_exposure,
    exposure_scope_for_workspace,
    grant_public_page_exposure,
    valid_public_exposed_version_id,
)
from backend.services.read_errors import (
    ReadNotFoundError,
    ReadServiceError,
    ReadValidationError,
    StaleBaseVersionError,
)
from backend.services.review_risk_policy import ai_writes_require_human_review
from backend.services.review_supersession import supersede_review_with_lineage
from backend.services.tree_service import ensure_primary_tree_placement
from backend.spi.types import AI_PROVENANCES
from backend.utils.actor_display import (
    resolve_actor_display_name,
    resolve_actor_display_names,
)
from backend.utils.block_identity import (
    block_map_is_stale,
    compute_block_map_with_ids,
    heal_block_map,
)
from backend.utils.content_hash import content_hash, same_authored_markdown
from backend.utils.markdown_links import (
    RenderedMarkdownAnalysis,
    rendered_markdown_links,
    rendered_wikilinks,
)
from backend.utils.markdown_outline import parse_outline
from backend.utils.page_frontmatter import (
    PAGE_TYPES,
    derive_nav_description,
    parse_page_frontmatter,
    title_from_markdown_heading,
    validate_page_markdown_input,
)
from backend.utils.page_frontmatter import (
    normalize_page_aliases as normalize_page_aliases,
)
from backend.utils.page_frontmatter import (
    normalize_page_tags as normalize_page_tags,
)

if TYPE_CHECKING:
    from backend.policy.auth import ActorContext


# ChangeSet.reason of a submission made by submit_context_edit_for_review.
SUBMIT_CONTEXT_EDIT_ACTION = "page.submit_context_edit_for_review"

_UNSET = object()


class PageServiceError(Exception):
    error_code = "internal_error"


class PageNotFoundError(PageServiceError):
    error_code = "not_found"


class PageValidationError(PageServiceError):
    error_code = "validation_failed"

    def __init__(self, message: str, *, cause_error_code: str | None = None) -> None:
        super().__init__(message)
        # The public error_code stays the one a 422 carries -- overriding it
        # with the inner failure's code hands a client a validation status
        # paired with, say, "not_found". The inner code is kept beside it so a
        # caller that wants to know exactly what refused the placement can ask.
        self.cause_error_code = cause_error_code


class PageContextStorageError(PageServiceError):
    def __init__(self, code: str) -> None:
        self.error_code = code
        super().__init__("Context version storage operation failed")


class PageMediaPolicyError(PageValidationError):
    error_code = MarkdownMediaPolicyError.error_code

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message, cause_error_code=reason_code)
        self.reason_code = reason_code


class PageContextMediaError(PageValidationError):
    """Stable write-ingress projection of a Context media domain refusal."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        page_id: UUID | None = None,
    ) -> None:
        super().__init__(message, cause_error_code=error_code)
        self.error_code = error_code
        self.page_id = page_id


class PageDiffTooLargeError(PageValidationError):
    error_code = "page_diff_too_large"


class PageHasNoPublishedVersionError(PageValidationError):
    """Publish requested on a page with no published HEAD (T16). 422 — the page
    must be published (current_published_version_id set) before it can be made
    public."""

    error_code = "page_has_no_published_version"


class WorkspacePublicSharingDisabledError(PageServiceError):
    """A page cannot be exposed unless its containing workspace is public."""

    error_code = "workspace_public_sharing_disabled"


class PagePublicExposureAcknowledgementRequiredError(PageValidationError):
    error_code = "public_exposure_acknowledgement_required"


class PagePublicExposureForbiddenError(PageServiceError):
    error_code = "public_exposure_forbidden"


class PagePublicVersionConflictError(PageServiceError):
    error_code = "public_share_version_changed"

    def __init__(self, current_published_version_id: UUID | None) -> None:
        super().__init__("the published version changed before sharing")
        self.current_published_version_id = current_published_version_id


class PageConflictError(PageServiceError):
    error_code = "stale_base_version"

    def __init__(
        self,
        server_version_id: UUID | None,
        server_content_hash: str | None,
        server_editing_revision: int | None = None,
        *,
        no_open_edit: bool = False,
        error_code: str | None = None,
    ) -> None:
        self.error_code = error_code or (
            "no_open_edit" if no_open_edit else "stale_base_version"
        )
        super().__init__(self.error_code.replace("_", " "))
        self.server_version_id = server_version_id
        self.server_content_hash = server_content_hash
        # Where the named draft stands now, so the caller knows what to
        # re-read. It is not a value to resend unchanged: the draft moved
        # because something else saved to it.
        self.server_editing_revision = server_editing_revision
        # The caller named its open edit, and it has none any more: that edit
        # was submitted or discarded since the caller last read it.
        self.no_open_edit = no_open_edit


@dataclass(frozen=True)
class PageListResult:
    pages: list[PageSummary]
    next_cursor: str | None = None


@dataclass(frozen=True)
class PageVersionListResult:
    versions: list[PageVersionEntry]
    next_cursor: str | None = None

    def __iter__(self):
        return iter(self.versions)

    def __len__(self) -> int:
        return len(self.versions)

    def __getitem__(self, index):
        return self.versions[index]


@dataclass(frozen=True)
class PagePublishResult:
    page: PagePayload
    findings: list[DraftReviewReason]


@dataclass(frozen=True)
class PageLinkProjectionDelta:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0

    def plus(
        self,
        *,
        inserted: int = 0,
        updated: int = 0,
        deleted: int = 0,
    ) -> PageLinkProjectionDelta:
        return PageLinkProjectionDelta(
            inserted=self.inserted + inserted,
            updated=self.updated + updated,
            deleted=self.deleted + deleted,
        )


ANCHOR_RESOLVE_CACHE_SIZE = 1024
PAGE_DIFF_MAX_INPUT_CHARS = 200_000
AnchorResolveCacheKey = tuple[str, str, UUID, str]
_anchor_resolve_cache: OrderedDict[
    AnchorResolveCacheKey,
    ResolvedAnchor,
] = OrderedDict()
_anchor_resolve_cache_lock = RLock()


def _clear_anchor_resolve_cache() -> None:
    with _anchor_resolve_cache_lock:
        _anchor_resolve_cache.clear()


def _anchor_cache_digest(anchor: PageInlineCommentAnchor) -> str:
    digest = hashlib.sha256()
    for part in (
        anchor.anchor_text,
        str(anchor.char_offset_hint),
        anchor.context_before,
        anchor.context_after,
    ):
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()


def _cached_resolve_anchor(
    *,
    current_body: str,
    current_content_hash: str,
    current_body_digest: str,
    anchor: PageInlineCommentAnchor,
) -> ResolvedAnchor:
    # current_body_digest is computed once per body by the caller (there can be
    # many anchors resolved against the same body, e.g. one per inline comment)
    # rather than re-hashed here on every call. content_hash alone is not a safe
    # cache key because it is computed on NORMALIZED text, so two different raw
    # bodies can share a content_hash while anchor offsets index the raw body -
    # the raw-body digest must stay in the key to avoid returning offsets
    # resolved against the wrong document.
    key = (
        current_content_hash,
        current_body_digest,
        anchor.version_id,
        _anchor_cache_digest(anchor),
    )
    with _anchor_resolve_cache_lock:
        cached = _anchor_resolve_cache.get(key)
        if cached is not None:
            _anchor_resolve_cache.move_to_end(key)
            return cached
    resolved = resolve_anchor(
        current_body,
        anchor_text=anchor.anchor_text,
        context_before=anchor.context_before,
        context_after=anchor.context_after,
        char_offset_hint=anchor.char_offset_hint,
    )
    with _anchor_resolve_cache_lock:
        _anchor_resolve_cache[key] = resolved
        _anchor_resolve_cache.move_to_end(key)
        while len(_anchor_resolve_cache) > ANCHOR_RESOLVE_CACHE_SIZE:
            _anchor_resolve_cache.popitem(last=False)
    return resolved


def _synthesize_or_resolve(
    *,
    current_body: str,
    current_content_hash: str,
    current_body_digest: str,
    current_version_id: UUID,
    anchor: PageInlineCommentAnchor,
) -> ResolvedAnchor:
    anchor_length = len(anchor.anchor_text)
    hint = anchor.char_offset_hint
    if (
        anchor.version_id == current_version_id
        and anchor_length > 0
        and 0 <= hint <= len(current_body) - anchor_length
        and current_body[hint : hint + anchor_length] == anchor.anchor_text
    ):
        return ResolvedAnchor(
            char_offset=hint,
            end_offset=hint + anchor_length,
            status=AnchorStatus.EXACT,
            resolved_text=anchor.anchor_text,
        )
    return _cached_resolve_anchor(
        current_body=current_body,
        current_content_hash=current_content_hash,
        current_body_digest=current_body_digest,
        anchor=anchor,
    )


class PageService:
    def __init__(
        self,
        session_factory,
        change_audit_recorder: ChangeReviewAuditRecorder | None = None,
        job_queue: JobQueueService | None = None,
        context_media_repository_factory: Callable[[Session], ContextMediaRepository]
        | None = None,
        lint_registry: LintRegistry | None = None,
        body_preparer: BodyPreparer | None = None,
        actor_context: ActorContext | None = None,
        context_version_writer=None,
        audit_service: AuditService | None = None,
    ) -> None:
        self.context_version_writer = context_version_writer
        self.session_factory = session_factory
        self.actor_context = actor_context
        self.body_preparer = (
            body_preparer if body_preparer is not None else BodyPreparer(lint_registry)
        )
        self.lint_registry = (
            lint_registry if lint_registry is not None else self.body_preparer.registry
        )
        # Publication owns an audit dependency independently of the optional
        # write-bundle recorder. A recorder implementing only its write
        # capability must still reach its intended failure and SQL rollback.
        self.audit_service = (
            audit_service
            or getattr(change_audit_recorder, "audit_service", None)
            or AuditService()
        )
        self.change_audit_recorder = change_audit_recorder or ChangeReviewAuditRecorder(
            audit_service=self.audit_service, lint_registry=self.lint_registry
        )
        self.job_queue = job_queue or JobQueueService()
        self.context_media_repository_factory = context_media_repository_factory

    def list_pages(
        self,
        workspace_slug: str,
        limit: int = 50,
        cursor: str | None = None,
        *,
        actor: ActorContext | None = None,
    ) -> PageListResult:
        offset = self._parse_cursor(cursor)
        if limit < 1 or limit > PAGE_LIST_LIMIT_MAX:
            raise PageValidationError(
                f"limit must be between 1 and {PAGE_LIST_LIMIT_MAX}"
            )
        with self.session_factory() as session:
            workspace = self._require_workspace(session, workspace_slug)
            page_versions = context_page_repo.list_pages_with_current_versions(
                session,
                workspace_id=workspace.id,
                limit=limit + 1,
                offset=offset,
                actor=actor,
            )
            visible_page_versions = page_versions[:limit]
            can_read_drafts = actor_can_read_drafts(actor)
            summaries = [
                self._summary_from_page(
                    session,
                    page,
                    version,
                    modified_at=published_at or version.created_at,
                    can_read_drafts=can_read_drafts,
                    actor=actor,
                )
                for page, version, published_at in visible_page_versions
            ]
            next_offset = offset + limit
            next_cursor = (
                self._encode_cursor(next_offset)
                if (
                    len(page_versions) > limit
                    and next_offset <= PAGE_LIST_CURSOR_MAX_OFFSET
                )
                else None
            )
            return PageListResult(pages=summaries, next_cursor=next_cursor)

    def read_page(
        self,
        page_id: UUID | None = None,
        workspace_slug: str | None = None,
        slug: str | None = None,
        version_id: UUID | None = None,
        *,
        actor: ActorContext | None = None,
    ) -> PagePayload:
        with self.session_factory() as session:
            _page, _version, payload = self.resolve_page_for_detail(
                session,
                page_id=page_id,
                workspace_slug=workspace_slug,
                slug=slug,
                version_id=version_id,
                actor=actor,
            )
            return payload

    def resolve_page_for_detail(
        self,
        session: Session,
        page_id: UUID | None = None,
        workspace_slug: str | None = None,
        slug: str | None = None,
        version_id: UUID | None = None,
        *,
        actor: ActorContext | None = None,
    ) -> tuple[ContextPage, ContextPageVersion, PagePayload]:
        """Session-scoped variant of ``read_page`` (P9.1).

        Returns the resolved ORM ``page``/``version`` alongside the payload so
        a caller sharing this session (the canonical
        ``/context/pages/{page_id}`` REST route) can layer additional
        read-only projection onto the same page/version without re-running
        this exact version-selection/visibility logic a second time.
        """
        page, version = self.resolve_page_version_for_read(
            session,
            page_id=page_id,
            workspace_slug=workspace_slug,
            slug=slug,
            version_id=version_id,
            actor=actor,
        )
        visible_version_id = (
            version.id
            if version_id is None
            else visible_context_version_id(page, actor)[0]
        )
        payload = self._payload_from_page(
            session,
            page,
            version,
            visible_version_id=visible_version_id,
            can_read_drafts=actor_can_read_drafts(actor),
            parent_visibility_actor=actor,
        )
        return page, version, payload

    def resolve_page_version_for_read(
        self,
        session: Session,
        page_id: UUID | None = None,
        workspace_slug: str | None = None,
        slug: str | None = None,
        version_id: UUID | None = None,
        *,
        actor: ActorContext | None = None,
    ) -> tuple[ContextPage, ContextPageVersion]:
        """Resolve an actor-visible version without constructing its payload."""
        page = self._resolve_page(session, page_id, workspace_slug, slug)
        selected_version_id = version_id
        if selected_version_id is None:
            selected_version_id = visible_context_version_id(page, actor)[0]
        version = self._require_page_version(session, page, selected_version_id)
        if not context_version_is_visible(version, actor):
            raise PageNotFoundError("page version not found")
        return page, version

    def read_public_page(
        self,
        workspace_slug: str,
        slug: str,
    ) -> PublicPagePayload:
        """Unauthenticated read of a page's PUBLISHED head (T16-B2).

        Security-critical: ``is_public`` alone is never authority. The complete
        stored scope/version grant must still match the Workspace visibility and
        current published snapshot. Missing, malformed, stale, and private cases
        all raise the same PageNotFoundError, and client version input is ignored.
        """
        with self.session_factory() as session:
            workspace = workspace_repo.get_workspace_by_slug(session, workspace_slug)
            page = (
                context_page_repo.get_page_by_slug(session, workspace.id, slug)
                if workspace is not None
                else None
            )
            exposed_version_id = (
                valid_public_exposed_version_id(page, workspace)
                if workspace is not None and page is not None
                else None
            )
            if exposed_version_id is None:
                # Uniform 404 for private / missing / draft-only / archived — the
                # Keep the archived gate as defense in depth even though the
                # archive cascade also revokes public exposure (Codex R14).
                raise PageNotFoundError("page not found")
            # Published head ONLY — never current_version_id, never a client value.
            version = self._require_page_version(
                session,
                page,
                exposed_version_id,
            )
            if version.version_status != "published_snapshot":
                raise PageNotFoundError("page not found")
            try:
                require_no_visible_legacy_media(
                    workspace_id=page.workspace_id,
                    body_markdown=version.body_md,
                )
            except ContextStorageServiceError as exc:
                raise PageContextStorageError(exc.error_code) from exc
            except LegacyMediaCutoverConflictError as exc:
                # Public reads never reveal whether the page is missing,
                # private, stale, or cut over from the historical resolver.
                raise PageNotFoundError("page not found") from exc
            # Strip the managed YAML frontmatter the write path injects
            # (title/slug/type/tags/aliases). Public readers get the rendered
            # body only, and the tags/aliases PublicPagePayload intentionally
            # drops never leak as frontmatter text (matches every internal body
            # consumer, which all parse before use).
            public_body = parse_page_frontmatter(version.body_md).body
            exposure_scope = exposure_scope_for_workspace(workspace)
            from backend.services.document_references import read_document_references

            return PublicPagePayload(
                references=read_document_references(
                    session,
                    version=version,
                    public=True,
                    self_origins=self.body_preparer.self_origins,
                ),
                id=page.id,
                slug=page.slug,
                # Title from the PUBLISHED body's heading — NOT page.title, which
                # tracks the (possibly unpublished) draft version, so a draft
                # rename would leak an unpublished title while the body stays the
                # published snapshot. Fall back to the slug (already in the public
                # URL), never to page.title.
                title=title_from_markdown_heading(public_body) or page.slug,
                body_md=public_body,
                # The PUBLISHED head's own timestamp — NOT page.updated_at, which
                # is re-stamped by draft saves that never change
                # the published body and would leak the timing of non-public
                # editing activity to anonymous observers.
                updated_at=version.created_at,
                exposure_scope=exposure_scope,
                link_targets=context_link_targets(
                    session,
                    page=page,
                    version=version,
                    public=True,
                    workspace=workspace,
                    self_origins=self.body_preparer.self_origins,
                ),
            )

    def set_page_public(
        self,
        page_id: UUID,
        is_public: bool,
        actor_kind: str,
        actor_id: str,
        *,
        actor_role: str | None = None,
        actor_provenance: str = "internal",
        acknowledge_public_exposure: bool = False,
        expected_published_version_id: UUID | None = None,
    ) -> PagePayload:
        """Publish/unpublish a page (T16-B1, editor+ gated at the endpoint).

        Public workspaces retain Editor+ sharing. A non-public workspace uses
        an unlisted direct URL and requires a browser-session Owner plus an
        explicit acknowledgement bound to the expected published head.
        Unpublishing keeps the published head for an explicit later re-share.
        """
        with self.session_factory.begin() as session:
            # SQLite SELECT ... FOR UPDATE is a no-op. Acquire its writer lock
            # before reading the version bound to the acknowledgement so a
            # concurrent publish cannot replace it between check and grant.
            acquire_write_intent(session)
            page_hint = context_page_repo.get_page_by_id(session, page_id)
            if page_hint is None:
                raise PageNotFoundError("page not found")
            workspace = session.scalar(
                select(Workspace)
                .where(Workspace.id == page_hint.workspace_id)
                .with_for_update()
            )
            page = session.scalar(
                select(ContextPage).where(ContextPage.id == page_id).with_for_update()
            )
            if page is None:
                raise PageNotFoundError("page not found")
            if workspace is None:
                raise PageNotFoundError("page not found")
            if is_public and page.lifecycle_status == "archived":
                raise PageValidationError("archived pages cannot be shared")
            if is_public and page.current_published_version_id is None:
                raise PageHasNoPublishedVersionError(
                    "publish a page version before enabling public access"
                )
            if is_public:
                published_version = self._require_page_version(
                    session,
                    page,
                    page.current_published_version_id,
                )
                if published_version.version_status != "published_snapshot":
                    raise PageHasNoPublishedVersionError(
                        "publish a page version before enabling public access"
                    )
                try:
                    require_no_visible_legacy_media(
                        workspace_id=page.workspace_id,
                        body_markdown=published_version.body_md,
                    )
                except ContextStorageServiceError as exc:
                    raise PageContextStorageError(exc.error_code) from exc
                except LegacyMediaCutoverConflictError as exc:
                    # Public exposure is a visibility transition in its own
                    # right.  A completed cutover must therefore refuse it
                    # before any grant field or audit state changes, even when
                    # the published snapshot predates the marker.
                    raise PageContextMediaError(
                        str(exc),
                        error_code=exc.error_code,
                        page_id=page.id,
                    ) from exc
            if (
                is_public
                and expected_published_version_id is not None
                and expected_published_version_id != page.current_published_version_id
            ):
                raise PagePublicVersionConflictError(page.current_published_version_id)
            non_public_workspace = workspace.visibility != "public"
            if is_public and non_public_workspace:
                if expected_published_version_id is None:
                    raise PagePublicVersionConflictError(
                        page.current_published_version_id
                    )
                if actor_role != "owner" or actor_provenance != "browser_session":
                    raise PagePublicExposureForbiddenError(
                        "only a Workspace owner using a browser session may "
                        "expose a page from a non-public Workspace"
                    )
                if not acknowledge_public_exposure:
                    raise PagePublicExposureAcknowledgementRequiredError(
                        "confirm that anyone with the link can read this published page"
                    )
            prior = page.is_public
            prior_scope = page.public_exposure_scope
            prior_exposed_version_id = page.public_exposed_version_id
            if is_public:
                assert page.current_published_version_id is not None
                grant_public_page_exposure(
                    page,
                    scope=exposure_scope_for_workspace(workspace),
                    version_id=page.current_published_version_id,
                )
            else:
                clear_public_page_exposure(page)
            session.flush()
            AuditService().emit(
                session=session,
                workspace_id=page.workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="page.publish" if is_public else "page.unpublish",
                target_kind="context_page",
                target_id=page.id,
                payload={
                    "prior_is_public": prior,
                    "prior_exposure_scope": prior_scope,
                    "prior_exposed_version_id": (
                        str(prior_exposed_version_id)
                        if prior_exposed_version_id is not None
                        else None
                    ),
                    "is_public": is_public,
                    "published_version_id": (
                        str(page.current_published_version_id)
                        if page.current_published_version_id is not None
                        else None
                    ),
                    "exposed_version_id": (
                        str(page.public_exposed_version_id)
                        if page.public_exposed_version_id is not None
                        else None
                    ),
                    "workspace_visibility": workspace.visibility,
                    "exposure_scope": (
                        "indexed_web"
                        if workspace.visibility == "public"
                        else "unlisted_web"
                    )
                    if is_public
                    else "none",
                    "acknowledged_public_exposure": bool(
                        is_public
                        and non_public_workspace
                        and acknowledge_public_exposure
                    ),
                },
            )
            # This operation changes public EXPOSURE, not the editable head, so
            # the published version is what the response should describe; a
            # page with no published version yet describes its current draft.
            version = self._require_page_version(
                session,
                page,
                page.current_published_version_id or page.current_version_id,
            )
            # `_payload_from_page`'s own `current_version_id` field defaults to
            # the raw page-global pointer (safe only for a self-view write
            # response, which this actor-independent exposure change is not)
            # -- pass the already-computed safe id explicitly instead.
            return self._payload_from_page(
                session, page, version, current_version_id_override=version.id
            )

    def list_backlinks(
        self,
        page_id: UUID,
        *,
        actor: ActorContext | None = None,
        limit: int = 50,
    ) -> list[PageBacklink]:
        if limit < 1 or limit > PAGE_LIST_LIMIT_MAX:
            raise PageValidationError(
                f"limit must be between 1 and {PAGE_LIST_LIMIT_MAX}"
            )
        with self.session_factory() as session:
            target_page = context_page_repo.get_page_by_id(session, page_id)
            if target_page is None:
                raise PageNotFoundError("page not found")
            # "Modified" here means last published, like every other list: the
            # raw page timestamp moves whenever a pointer does, so staging a
            # draft would reorder someone else's backlinks and date a source
            # page by an edit its readers cannot see.
            backlink_published = aliased(ContextPageVersion)
            backlink_modified_at = func.coalesce(
                backlink_published.created_at, ContextPage.updated_at
            )
            from backend.services.context_link_targets import page_link_target_condition

            source_statement = join_in_workspace(
                select_in_workspace(
                    PageLink,
                    workspace_id=target_page.workspace_id,
                    conditions=(
                        page_link_target_condition(target_page),
                        PageLink.source_page_id != target_page.id,
                        ContextPage.lifecycle_status != "archived",
                        PageLink.source_version_id
                        == visible_context_version_expression(actor),
                    ),
                ),
                PageLink,
                ContextPage,
                workspace_id=target_page.workspace_id,
                on=ContextPage.id == PageLink.source_page_id,
            )
            ranked_backlinks = (
                join_in_workspace(
                    source_statement,
                    PageLink,
                    backlink_published,
                    workspace_id=target_page.workspace_id,
                    on=backlink_published.id
                    == ContextPage.current_published_version_id,
                    isouter=True,
                )
                .with_only_columns(
                    ContextPage.id.label("page_id"),
                    ContextPage.slug.label("slug"),
                    ContextPage.title.label("title"),
                    backlink_modified_at.label("modified_at"),
                    func.sum(PageLink.occurrence_count)
                    .over(partition_by=PageLink.source_page_id)
                    .label("occurrence_count"),
                    PageLink.first_snippet.label("snippet"),
                    func.row_number()
                    .over(
                        partition_by=PageLink.source_page_id,
                        order_by=page_link_snippet_order(),
                    )
                    .label("snippet_rank"),
                )
                .subquery()
            )
            rows = session.execute(
                select(
                    ranked_backlinks.c.page_id,
                    ranked_backlinks.c.slug,
                    ranked_backlinks.c.title,
                    ranked_backlinks.c.modified_at,
                    ranked_backlinks.c.occurrence_count,
                    ranked_backlinks.c.snippet,
                )
                .where(ranked_backlinks.c.snippet_rank == 1)
                .order_by(
                    ranked_backlinks.c.modified_at.desc(),
                    ranked_backlinks.c.slug.asc(),
                )
                .limit(limit)
            ).all()
            backlinks = [
                PageBacklink(
                    id=page_id,
                    slug=slug,
                    title=title,
                    updated_at=modified_at,
                    occurrence_count=total_occurrence_count,
                    snippet=snippet,
                )
                for (
                    page_id,
                    slug,
                    title,
                    modified_at,
                    total_occurrence_count,
                    snippet,
                ) in rows
            ]
            return backlinks

    def list_tags(
        self,
        workspace_id: UUID,
        query: str | None = None,
        limit: int = 20,
        *,
        actor: ActorContext | None = None,
    ) -> list[str]:
        if limit < 1 or limit > 50:
            raise PageValidationError("limit must be between 1 and 50")
        normalized_query = (query or "").strip().casefold()
        with self.session_factory() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None:
                raise PageNotFoundError(f"workspace not found: {workspace_id}")
            visible_version_id = visible_context_version_expression(actor)
            page_versions = session.execute(
                select(ContextPage, ContextPageVersion)
                # Explicit left side -- see context_page_repo.py's
                # current_page_versions_statement for why this is required.
                .select_from(ContextPage)
                .join(ContextPageVersion, ContextPageVersion.id == visible_version_id)
                .where(ContextPage.workspace_id == workspace.id)
            ).all()
            counts: Counter[str] = Counter()
            for page, version in page_versions:
                _title, _page_type, tags, _aliases = effective_metadata_for_payload(
                    session, page, version
                )
                for tag in normalize_page_tags(tags):
                    if normalized_query and normalized_query not in tag.casefold():
                        continue
                    counts[tag] += 1
        return sorted(counts, key=lambda tag: (-counts[tag], tag.casefold()))[:limit]

    def list_versions(
        self,
        page_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
        *,
        actor: ActorContext | None = None,
    ) -> PageVersionListResult:
        if limit < 1 or limit > PAGE_LIST_LIMIT_MAX:
            raise PageValidationError(
                f"limit must be between 1 and {PAGE_LIST_LIMIT_MAX}"
            )
        with self.session_factory() as session:
            page = context_page_repo.get_page_by_id(session, page_id)
            if page is None:
                raise PageNotFoundError("page not found")
            before_version_no = self._parse_version_cursor(cursor, page.id)
            # The role decides visibility: draft readers see every version,
            # everyone else the published ones.
            versions = context_page_repo.list_versions(
                session,
                page.id,
                limit + 1,
                visible_statuses=(
                    None
                    if actor_can_read_drafts(actor)
                    else PUBLISHED_CONTEXT_VERSION_STATUSES
                ),
                before_version_no=before_version_no,
            )
            has_more = len(versions) > limit
            versions = versions[:limit]
            review_metadata = context_version_review_metadata(
                session, versions, workspace_id=page.workspace_id
            )
            missing_actor_keys = {
                (version.actor_type, version.actor_id)
                for version in versions
                if not version.actor_display_name
            }
            actor_names = resolve_actor_display_names(session, missing_actor_keys)
            visible_head_id = visible_context_version_id(page, actor)[0]
            expose_draft_state = actor_can_read_drafts(actor)
            parent_ids = {
                version.parent_version_id
                for version in versions
                if version.parent_version_id is not None
            }
            parents_by_id = (
                {
                    parent.id: parent
                    for parent in session.scalars(
                        select(ContextPageVersion).where(
                            ContextPageVersion.workspace_id == page.workspace_id,
                            ContextPageVersion.page_id == page.id,
                            ContextPageVersion.id.in_(parent_ids),
                        )
                    ).all()
                }
                if parent_ids
                else {}
            )
            entries = [
                PageVersionEntry(
                    version_id=version.id,
                    version_no=version.version_no,
                    parent_version_id=context_parent_projection(
                        version,
                        parents_by_id.get(version.parent_version_id),
                        actor,
                    )[1],
                    version_status=version.version_status,
                    review_disposition=review_metadata[version.id].disposition,
                    **_page_version_projection_fields(
                        page,
                        version,
                        visible_version_id=visible_head_id,
                        expose_draft_state=expose_draft_state,
                    ),
                    actor_type=version.actor_type,
                    actor_display_name=version.actor_display_name
                    or actor_names[(version.actor_type, version.actor_id)],
                    created_at=version.created_at,
                    content_hash=version.content_hash,
                    is_current=(
                        version.id == page.current_version_id
                        if expose_draft_state
                        else version.id == visible_head_id
                    ),
                    is_current_published=(
                        version.id == page.current_published_version_id
                    ),
                    review_status=review_metadata[version.id].status,
                    review_item_id=review_metadata[version.id].review_item_id,
                )
                for version in versions
            ]
            next_cursor = (
                self._encode_version_cursor(page.id, versions[-1].version_no)
                if has_more and versions
                else None
            )
            return PageVersionListResult(
                versions=entries,
                next_cursor=next_cursor,
            )

    def _prepare_merge_candidate(
        self, session, page, version, candidate, actor_kind, actor_id, actor_provenance
    ):
        from dataclasses import replace

        from backend.context_versions.domain import PageMetadata
        from backend.context_versions.merge import result_hash

        prepared = self._prepare_body(
            BodyInput(
                candidate.body_md,
                metadata=PageMetadataInput(
                    title=candidate.metadata.title, slug=page.slug
                ),
                fallback_metadata=PreparedMetadata(
                    title=candidate.metadata.title,
                    page_type=candidate.metadata.page_type,
                    tags=list(candidate.metadata.tags),
                    aliases=list(candidate.metadata.aliases),
                ),
                baseline_body_md=version.body_md,
            ),
            WriteContext(
                actor=self.actor_context,
                workspace=self._workspace_for(session, page.workspace_id),
                session=session,
                page=page,
                version=version,
                source_version=version,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
            ),
        )
        metadata = PageMetadata(
            candidate.metadata.title,
            candidate.metadata.page_type,
            tuple(candidate.metadata.tags),
            tuple(normalize_page_aliases(list(candidate.metadata.aliases))),
            candidate.metadata.placement_parent_node_id,
        )
        block_map = (
            candidate.block_map
            if prepared.body == candidate.body_md
            else compute_block_map_with_ids(
                prepared.body,
                previous_body_md=candidate.body_md,
                previous_block_map=candidate.block_map,
                identity_seed=str(version.id),
            )
        )
        return replace(
            candidate,
            body_md=prepared.body,
            metadata=metadata,
            block_map=block_map,
            result_hash=result_hash(prepared.body, metadata, block_map),
        )

    def preview_context_merge(
        self,
        page_id: UUID,
        version_id: UUID,
        workspace_id: UUID | None = None,
        on_conflict: str | None = None,
        actor_kind: str = "human",
        actor_id: str = "",
        actor_provenance: str = "browser_session",
        body_md: str | None = None,
        page_metadata: dict | None = None,
        commit_version_id: UUID | None = None,
    ) -> dict:
        """The merge of a commit into this version, without saving.

        The target defaults to the current published version, which is what
        publication merges; a named commit is what bring-in would merge.
        """
        from dataclasses import replace

        from backend.services.context_draft_workflow import preview_merge

        with self.session_factory() as session:
            page = context_page_repo.get_page_by_id(session, page_id)
            if page is None or (
                workspace_id is not None and page.workspace_id != workspace_id
            ):
                raise PageNotFoundError("page not found")
            version = self._require_page_version(session, page, version_id)
            if version.version_status not in {
                "draft",
                "closed",
                "superseded_snapshot",
                "published_snapshot",
            }:
                raise PageNotFoundError("page version not found")
            preview = preview_merge(
                session,
                page,
                version,
                commit_version_id=commit_version_id,
                prepare=lambda candidate: self._prepare_merge_candidate(
                    session,
                    page,
                    version,
                    candidate,
                    actor_kind,
                    actor_id,
                    actor_provenance,
                ),
            )
            edited = body_md is not None or page_metadata is not None
            if on_conflict is None and not edited:
                return preview.payload()
            # `result` is what publication would publish for the same choice
            # and edits.
            chosen = preview.choose(on_conflict)
            if edited:
                from backend.services.context_draft_workflow import (
                    edit_merge_candidate,
                )

                chosen = self._prepare_merge_candidate(
                    session,
                    page,
                    version,
                    edit_merge_candidate(
                        chosen, body_md=body_md, page_metadata=page_metadata
                    ),
                    actor_kind,
                    actor_id,
                    actor_provenance,
                )
            conflict_bodies = dict(preview.conflict_bodies or {})
            conflict_bodies["result"] = conflict_bodies.get(
                on_conflict or "result", chosen.body_md
            )
            return replace(
                preview, clean=chosen, conflict_bodies=conflict_bodies
            ).payload()

    def bring_in_context_version(
        self,
        page_id: UUID,
        version_id: UUID,
        workspace_id: UUID,
        expected_editing_revision: int,
        idempotency_key: str,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        commit_version_id: UUID | None = None,
        on_conflict: str | None = None,
        retry_only: bool = False,
    ) -> PagePayload:
        from backend.services.context_draft_workflow import (
            history_for,
            merge_draft_references,
            metadata_dict,
            parents_for,
            preview_merge,
            record_draft_save,
            set_parents,
        )

        with self.session_factory.begin() as session:

            def execute():
                acquire_write_intent(session)
                self._lock_context_media_usage_intent_for_page(session, page_id)
                page = context_page_repo.get_page_by_id_for_update(session, page_id)
                if page is None or page.workspace_id != workspace_id:
                    raise PageNotFoundError("page not found")
                version = self._require_named_open_edit(
                    session, page, version_id, actor_kind, actor_id
                )
                if version.editing_revision != expected_editing_revision:
                    raise PageConflictError(
                        server_version_id=version.id,
                        server_content_hash=version.content_hash,
                        server_editing_revision=version.editing_revision,
                        error_code="editing_revision_conflict",
                    )
                target_id = commit_version_id or page.current_published_version_id
                preview = preview_merge(
                    session,
                    page,
                    version,
                    commit_version_id=target_id,
                    prepare=lambda candidate: self._prepare_merge_candidate(
                        session,
                        page,
                        version,
                        candidate,
                        actor_kind,
                        actor_id,
                        actor_provenance,
                    ),
                )
                # The draft already contains the target (or there is none):
                # nothing to merge, so nothing is saved.
                if preview.kind == "fast_forward":
                    return self._payload_from_page(session, page, version)
                candidate = preview.choose(on_conflict)
                target = self._require_page_version(session, page, target_id)
                version.body_md, version.block_map = (
                    candidate.body_md,
                    candidate.block_map,
                )
                version.content_hash = content_hash(candidate.body_md)
                version.page_metadata = metadata_dict(candidate.metadata)
                merge_draft_references(
                    session,
                    source=version,
                    head=target,
                    target=version,
                    self_origins=self.body_preparer.self_origins,
                )
                history, _ = history_for(session, page)
                parents = list(parents_for(session, version))
                parents = parents[:1] + [
                    key
                    for key in parents[1:]
                    if not history.is_ancestor(key, target_id)
                ]
                if target_id not in parents:
                    parents.append(target_id)
                set_parents(session, version, parents)
                version.editing_revision += 1
                version.last_saved_at = utc_now()
                record_draft_save(
                    session,
                    page,
                    version,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    reason="page.bring_in",
                )
                self._sync_version_usage(
                    session, page=page, version=version, body_md=version.body_md
                )
                self._refresh_page_links(session, page=page, version=version)
                return self._payload_from_page(
                    session, page, version, current_version_id_override=version.id
                )

            return execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                tool_name="bring_in_context_version",
                idempotency_key=idempotency_key,
                fingerprint=request_fingerprint(
                    {
                        "page_id": page_id,
                        "version_id": version_id,
                        "expected_editing_revision": expected_editing_revision,
                        "commit_version_id": commit_version_id,
                        "on_conflict": on_conflict,
                    }
                ),
                result_model=PagePayload,
                execute=execute,
                retry_only=retry_only,
            )

    def get_reconciliation(
        self,
        *,
        page_id: UUID,
        branch_id: UUID,
        actor: ActorContext | None = None,
    ):
        if not actor_can_read_drafts(actor):
            raise PageNotFoundError("reconciliation not found")
        from backend.services.context_reconciliation import (
            load_reconciliation_snapshot,
            project_reconciliation_detail,
        )

        with self.session_factory() as session:
            assert actor is not None
            assert actor.workspace_id is not None
            page = session.scalar(
                select(ContextPage).where(
                    ContextPage.workspace_id == actor.workspace_id,
                    ContextPage.id == page_id,
                )
            )
            if page is None:
                raise PageNotFoundError("page not found")
            snapshot = load_reconciliation_snapshot(
                session,
                page=page,
                branch_id=branch_id,
            )
            return project_reconciliation_detail(session, snapshot)

    def reconcile_branch(
        self,
        *,
        page_id: UUID,
        branch_id: UUID,
        request,
        actor: ActorContext | None = None,
    ):
        if not actor_can_read_drafts(actor):
            raise PageNotFoundError("reconciliation not found")
        if request.reconciles != [branch_id]:
            raise PageValidationError(
                "reconciles must contain exactly the branch id from the route"
            )
        from backend.schemas.context_edit import (
            PatchDraftRequest,
            ResolveConflictOperation,
        )
        from backend.schemas.context_reconciliation import (
            ContextReconciliationCommit,
            ContextReconciliationCommitResult,
            ContextReconciliationDryRunResult,
            ContextReconciliationPreview,
        )
        from backend.services.context_write_service import ContextWriteService

        operations = [
            ResolveConflictOperation(
                op="resolve_conflict",
                block=resolution.block_id,
                resolution=resolution.choice,
                expected=resolution.expected_hash,
                content=resolution.content,
            )
            for resolution in request.resolutions
        ]
        patch_request = PatchDraftRequest(
            page_id=page_id,
            operations=operations,
            reason=request.reason,
            base=str(request.expected_head_version_id),
            reconciles=request.reconciles,
            excluded_clean_change_ids=request.excluded_clean_change_ids,
            idempotency_key=request.idempotency_key,
            dry_run=request.dry_run,
        )
        assert actor is not None
        assert actor.workspace_id is not None
        with self.session_factory.begin() as session:
            result = ContextWriteService(
                context_version_writer=self.context_version_writer,
                audit_service=self.change_audit_recorder.audit_service,
                job_queue=self.job_queue,
                context_media_repository_factory=self.context_media_repository_factory,
                body_preparer=self.body_preparer,
                actor_context=actor,
            ).patch_draft(
                session=session,
                workspace_id=actor.workspace_id,
                request=patch_request,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                actor_provenance=actor.provenance,
                auto_approve_format_only=True,
                require_current_base=True,
            )
            if request.dry_run:
                if (
                    result.preview_body_markdown is None
                    or result.predicted_block_map is None
                ):
                    raise RuntimeError("reconciliation preview is incomplete")
                return ContextReconciliationDryRunResult(
                    dry_run=True,
                    preview=ContextReconciliationPreview(
                        head_version_id=request.expected_head_version_id,
                        merged_body_markdown=result.preview_body_markdown,
                        predicted_block_map=result.predicted_block_map,
                        unresolved_count=0,
                    ),
                )
            if result.draft_version_id is None:
                raise RuntimeError("reconciliation commit did not return a draft")
            return ContextReconciliationCommitResult(
                dry_run=False,
                commit=ContextReconciliationCommit(
                    draft_version_id=result.draft_version_id,
                    head_version_id=request.expected_head_version_id,
                    branch_status="superseded",
                    review_item_id=result.review_item_id,
                ),
            )

    def discard_alternative(
        self,
        *,
        page_id: UUID,
        version_id: UUID,
        reason: str,
        actor: ActorContext | None = None,
    ) -> PageVersionEntry:
        if reason is not None:
            validate_text(reason, field="reason", max_length=2000)
        if not actor_can_read_drafts(actor):
            raise PageNotFoundError("page version not found")
        with self.session_factory.begin() as session:
            assert actor is not None
            assert actor.workspace_id is not None
            page = session.scalar(
                select(ContextPage).where(
                    ContextPage.workspace_id == actor.workspace_id,
                    ContextPage.id == page_id,
                )
            )
            if page is None:
                raise PageNotFoundError("page not found")
            version = self._require_page_version(session, page, version_id)
            assert actor is not None
            from backend.services.context_reconciliation import (
                discard_reconciliation_branch,
            )

            discard_reconciliation_branch(
                session=session,
                page=page,
                branch=version,
                reason=reason,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                audit_service=self.change_audit_recorder.audit_service,
            )
            session.flush()
            review_metadata = context_version_review_metadata(
                session, (version,), workspace_id=page.workspace_id
            )[version.id]
            projection = project_context_version(
                page,
                version,
                visible_version_id=page.latest_draft_version_id
                or page.current_version_id,
                expose_draft_state=True,
            )
            return PageVersionEntry(
                version_id=version.id,
                version_no=version.version_no,
                parent_version_id=version.parent_version_id,
                version_status=version.version_status,
                review_disposition=review_metadata.disposition,
                history_status=projection.history_status,
                graph_status=projection.graph_status,
                actor_type=version.actor_type,
                actor_display_name=version.actor_display_name,
                created_at=version.created_at,
                content_hash=version.content_hash,
                is_current=False,
                is_current_published=(version.id == page.current_published_version_id),
                is_latest_draft=(version.id == page.latest_draft_version_id),
                review_status=review_metadata.status,
                review_item_id=review_metadata.review_item_id,
                is_visible_head=projection.is_visible_head,
                is_published_head=projection.is_published_head,
                is_draft_head=projection.is_draft_head,
                is_alternative=projection.is_alternative,
                is_discarded=projection.is_discarded,
                reconciliation_available=False,
            )

    def publish_draft(
        self,
        *,
        page_id: UUID,
        version_id: UUID,
        reason: str,
        publish_despite_findings: bool,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        workspace_id: UUID | None = None,
        expected_content_hash: str | None = None,
        expected_references_hash: str | None = None,
        expected_editing_revision: int | None = None,
        expected_published_version_id: UUID | None = None,
        result_hash: str | None = None,
        on_conflict: str | None = None,
        idempotency_key: str | None = None,
        result_body_md: str | None = None,
        result_page_metadata: dict | None = None,
    ) -> PagePublishResult:
        from backend.schemas.context_write import PublishContextPageRequest
        from backend.services.context_write_service import ContextWriteService

        if publish_despite_findings and actor_provenance != "browser_session":
            raise PageValidationError(
                "publishing despite format findings requires an interactive "
                "browser session"
            )
        with self.session_factory.begin() as session:
            owner = context_page_repo.get_page_by_id(session, page_id)
            if owner is None or (
                workspace_id is not None and owner.workspace_id != workspace_id
            ):
                raise PageNotFoundError("page not found")
            publication_findings = []

            def execute():
                acquire_write_intent(session)
                self._lock_context_media_usage_intent_for_page(session, page_id)
                page = context_page_repo.get_page_by_id_for_update(session, page_id)
                if page is None or (
                    workspace_id is not None and page.workspace_id != workspace_id
                ):
                    raise PageNotFoundError("page not found")
                staged = self._require_page_version(session, page, version_id)
                from backend.services.document_references import (
                    document_references_hash,
                )

                if (
                    expected_content_hash is not None
                    and staged.content_hash != expected_content_hash
                ):
                    raise PageConflictError(
                        server_version_id=staged.id,
                        server_content_hash=staged.content_hash,
                    )
                if (
                    expected_references_hash is not None
                    and document_references_hash(session, staged)
                    != expected_references_hash
                ):
                    raise PageConflictError(
                        server_version_id=staged.id,
                        server_content_hash=staged.content_hash,
                    )
                open_reviews = [
                    review
                    for review in ContextWriteService._reviews_for_version(
                        session, staged
                    )
                    if review.status in {"pending", "changes_requested"}
                    and review.review_type == "context_update"
                    and not (
                        (review.risk_signals or {})
                        .get("migration_parent_repaired", {})
                        .get("inspection_only")
                    )
                ]
                if open_reviews:
                    from backend.schemas.context_write import ReviewDecisionRequest
                    from backend.services.context_draft_workflow import (
                        direct_publication_risk,
                        publication_candidate,
                    )
                    from backend.services.review_risk_policy import (
                        ai_writes_require_human_review,
                        review_risk_snapshot,
                    )
                    from backend.services.review_service import ReviewService

                    approval_request = ReviewDecisionRequest(
                        decision_type="approve",
                        decision_reason=reason,
                        expected_editing_revision=expected_editing_revision,
                        expected_published_version_id=expected_published_version_id,
                        result_hash=result_hash,
                        on_conflict=on_conflict,
                        result_body_md=result_body_md,
                        result_page_metadata=result_page_metadata,
                    )
                    policy_workspace = self._workspace_for(session, page.workspace_id)
                    _, candidate = publication_candidate(
                        session,
                        page,
                        staged,
                        approval_request,
                        prepare=lambda candidate: self._prepare_merge_candidate(
                            session,
                            page,
                            staged,
                            candidate,
                            actor_kind,
                            actor_id,
                            actor_provenance,
                        ),
                    )
                    # Direct-write authorization follows the prepared chosen
                    # merge result, including explicit result edits. A staged
                    # overview cannot authorize a decision-page publication.
                    policy_resolution = direct_publication_risk(
                        session,
                        page,
                        candidate,
                        workspace=policy_workspace,
                        reviews=open_reviews,
                        actor_provenance=actor_provenance,
                        risk_resolver=self.change_audit_recorder.risk_resolver,
                        lint_registry=self.lint_registry,
                    )
                    policy_approval = (
                        actor_provenance in AI_PROVENANCES
                        and not ai_writes_require_human_review(policy_workspace)
                        and not policy_resolution.review_required
                        and all(
                            review.status == "pending"
                            and not (review.risk_signals or {}).get(
                                "human_review_required"
                            )
                            and not (review.risk_signals or {}).get(
                                "caller_needs_review"
                            )
                            for review in open_reviews
                        )
                    )
                    if policy_approval:
                        for review in open_reviews:
                            for key, value in review_risk_snapshot(
                                policy_resolution
                            ).items():
                                setattr(review, key, value)

                    review_result = ReviewService(
                        context_version_writer=self.context_version_writer,
                        audit_service=self.audit_service,
                        body_preparer=self.body_preparer,
                        context_media_repository_factory=(
                            self.context_media_repository_factory
                        ),
                    ).apply_decision(
                        session=session,
                        workspace_id=page.workspace_id,
                        review_item_id=open_reviews[0].id,
                        request=approval_request,
                        actor_kind="system" if policy_approval else actor_kind,
                        actor_id=(
                            "context-direct-publication-policy"
                            if policy_approval
                            else actor_id
                        ),
                        actor_provenance=(
                            "internal" if policy_approval else actor_provenance
                        ),
                        actor_user_id=getattr(self.actor_context, "user_id", None),
                        actor_role=getattr(
                            self.actor_context, "workspace_role", "editor"
                        ),
                        actor=self.actor_context,
                        publish_despite_findings=publish_despite_findings,
                    )
                    version = self._require_page_version(
                        session, page, review_result.published_version_id
                    )
                    publication_findings.extend(
                        DraftReviewReason(
                            code=finding.rule_id,
                            message=body_relative_message(
                                finding.suggested_action or finding.rule_id,
                                version.body_md,
                            ),
                        )
                        for finding in publish_floor_findings(
                            version.body_md, registry=self.lint_registry
                        )
                    )
                    return self._payload_from_page(session, page, version)
                result = ContextWriteService(
                    context_version_writer=self.context_version_writer,
                    audit_service=self.audit_service,
                    job_queue=self.job_queue,
                    body_preparer=self.body_preparer,
                    actor_context=self.actor_context,
                    context_media_repository_factory=self.context_media_repository_factory,
                ).publish_draft(
                    session=session,
                    workspace_id=page.workspace_id,
                    page_id=page.id,
                    request=PublishContextPageRequest(
                        version_id=version_id,
                        reason=reason,
                        expected_editing_revision=expected_editing_revision,
                        expected_published_version_id=expected_published_version_id,
                        result_hash=result_hash,
                        on_conflict=on_conflict,
                        result_body_md=result_body_md,
                        result_page_metadata=result_page_metadata,
                    ),
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    publish_despite_findings=publish_despite_findings,
                    actor_provenance=actor_provenance,
                )
                version = self._require_page_version(
                    session, page, result.published_version_id
                )
                publication_findings.extend(result.findings)
                return self._payload_from_page(
                    session,
                    page,
                    version,
                    prepared=self._inspect_saved_body(
                        session,
                        page,
                        version,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        actor_provenance=actor_provenance,
                    ),
                )

            result = execute_idempotent_write(
                session,
                workspace_id=owner.workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                tool_name="publish_context_edit",
                idempotency_key=idempotency_key,
                fingerprint=request_fingerprint(
                    {
                        "page_id": page_id,
                        "version_id": version_id,
                        "reason": reason,
                        "expected_editing_revision": expected_editing_revision,
                        "expected_published_version_id": expected_published_version_id,
                        "result_hash": result_hash,
                        "expected_references_hash": expected_references_hash,
                        "expected_content_hash": expected_content_hash,
                        "on_conflict": on_conflict,
                        "result_body_md": result_body_md,
                        "result_page_metadata": result_page_metadata,
                        "publish_despite_findings": publish_despite_findings,
                    }
                ),
                result_model=PagePayload,
                execute=execute,
            )
            return PagePublishResult(page=result, findings=publication_findings)

    def diff_versions(
        self,
        *,
        page_id: UUID,
        from_version_id: UUID | None,
        to_version_id: UUID,
        baseline: str | None = None,
        actor: ActorContext | None = None,
    ) -> PageVersionDiff:
        with self.session_factory() as session:
            page = context_page_repo.get_page_by_id(session, page_id)
            if page is None:
                raise PageNotFoundError("page not found")
            to_version = self._require_page_version(session, page, to_version_id)
            if baseline == "empty":
                if from_version_id is not None:
                    raise PageValidationError(
                        "baseline=empty cannot be combined with from_version_id"
                    )
                from_version = None
            else:
                if baseline is not None:
                    raise PageValidationError("baseline must be empty when provided")
                if from_version_id is None:
                    raise PageValidationError(
                        "from_version_id is required unless baseline=empty"
                    )
                from_version = self._require_page_version(
                    session, page, from_version_id
                )
            if (
                from_version is not None
                and not context_version_is_visible(from_version, actor)
            ) or not context_version_is_visible(to_version, actor):
                raise PageNotFoundError("page version not found")
            from_body = (
                ""
                if from_version is None
                else parse_page_frontmatter(from_version.body_md).body
            )
            to_body = parse_page_frontmatter(to_version.body_md).body
            if len(from_body) + len(to_body) > PAGE_DIFF_MAX_INPUT_CHARS:
                raise PageDiffTooLargeError(
                    "These versions are too large to compare here. Open each "
                    "version separately and compare smaller excerpts instead."
                )
            from_version_no = 0 if from_version is None else from_version.version_no
            lines = list(
                difflib.unified_diff(
                    from_body.splitlines(),
                    to_body.splitlines(),
                    fromfile=(
                        "empty" if from_version is None else f"v{from_version_no}"
                    ),
                    tofile=f"v{to_version.version_no}",
                    lineterm="",
                )
            )
            metadata_changes = _page_version_metadata_changes(
                session,
                page=page,
                from_version=from_version,
                to_version=to_version,
            )
            from backend.services.document_references import read_document_references

            return PageVersionDiff(
                references_before=read_document_references(
                    session,
                    version=from_version,
                    actor=actor,
                    self_origins=self.body_preparer.self_origins,
                )
                if from_version
                else [],
                references_after=read_document_references(
                    session,
                    version=to_version,
                    actor=actor,
                    self_origins=self.body_preparer.self_origins,
                ),
                baseline="empty" if from_version is None else "version",
                from_version_id=(None if from_version is None else from_version.id),
                to_version_id=to_version.id,
                from_version_no=from_version_no,
                to_version_no=to_version.version_no,
                lines=lines,
                additions=sum(
                    1
                    for line in lines
                    if line.startswith("+") and not line.startswith("+++")
                ),
                deletions=sum(
                    1
                    for line in lines
                    if line.startswith("-") and not line.startswith("---")
                ),
                metadata_changes=metadata_changes,
            )

    def restore_version(
        self,
        *,
        page_id: UUID,
        version_id: UUID,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
    ) -> PagePayload:
        """Restore old content as a new draft based on today's published commit."""
        from backend.services.context_draft_workflow import metadata_dict, metadata_for

        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            self._lock_context_media_usage_intent_for_page(session, page_id)
            page = context_page_repo.get_page_by_id_for_update(session, page_id)
            if page is None:
                raise PageNotFoundError("page not found")
            source = self._require_page_version(session, page, version_id)
            if not source.commit_oid:
                raise PageValidationError(
                    "Restore requires an immutable Context commit"
                )
            version = self._insert_version_copy(
                session,
                page_id=page.id,
                parent_version_id=page.current_published_version_id,
                source_version=source,
                actor_kind=actor_kind,
                actor_id=actor_id,
                version_status="draft",
            )
            version.page_metadata = metadata_dict(metadata_for(page, source))
            result = self.change_audit_recorder.record_context_page_write(
                session=session,
                workspace_id=page.workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="page.restore_version",
                page_id=page.id,
                version_id=version.id,
                actor_provenance=actor_provenance,
                body=version.body_md,
                payload={"restored_from_version_id": str(source.id)},
            )
            if actor_provenance == "browser_session":
                from backend.schemas.context_write import PublishContextPageRequest
                from backend.services.context_write_service import ContextWriteService

                published = ContextWriteService(
                    context_version_writer=self.context_version_writer,
                    body_preparer=self.body_preparer,
                    actor_context=self.actor_context,
                    context_media_repository_factory=self.context_media_repository_factory,
                ).publish_draft(
                    session=session,
                    workspace_id=page.workspace_id,
                    page_id=page.id,
                    request=PublishContextPageRequest(
                        version_id=version.id,
                        reason="Restored previous Context content",
                    ),
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
                version = self._require_page_version(
                    session, page, published.published_version_id
                )
            self._refresh_page_links(session, page=page, version=version)
            return self._payload_from_page(
                session,
                page,
                version,
                change_set_id=result.change_set_id,
                review_item_id=result.review_item_id,
            )

    def list_inline_comments(
        self,
        page_id: UUID,
        limit: int = 50,
        *,
        actor: ActorContext | None = None,
    ) -> list[PageInlineComment]:
        if limit < 1 or limit > PAGE_LIST_LIMIT_MAX:
            raise PageValidationError(
                f"limit must be between 1 and {PAGE_LIST_LIMIT_MAX}"
            )
        with self.session_factory() as session:
            page = context_page_repo.get_page_by_id(session, page_id)
            if page is None:
                raise PageNotFoundError("page not found")
            visible_version, _is_draft = visible_context_version_id(page, actor)
            current_version = self._require_page_version(
                session,
                page,
                visible_version,
            )
            current_body = parse_page_frontmatter(current_version.body_md).body
            # Hashed once here and reused for every anchor below - resolving N
            # inline comments against the same body must not re-hash the full
            # body N times.
            current_body_digest = hashlib.sha256(
                current_body.encode("utf-8")
            ).hexdigest()
            rows = session.execute(
                select_inline_comment_rows(page.id, limit=limit)
            ).all()
            comments: list[PageInlineComment] = []
            for operation, review in rows:
                anchor = parse_inline_comment_anchor(operation.diff_ref)
                if anchor is None:
                    continue
                resolved = _synthesize_or_resolve(
                    current_body=current_body,
                    current_content_hash=current_version.content_hash,
                    current_body_digest=current_body_digest,
                    current_version_id=current_version.id,
                    anchor=anchor,
                )
                comments.append(
                    inline_comment_payload(
                        page_id=page.id,
                        operation=operation,
                        review=review,
                        anchor=anchor,
                        resolved=resolved,
                    )
                )
            return comments

    def create_inline_comment(
        self,
        *,
        page_id: UUID,
        note: str,
        anchor: PageInlineCommentAnchor,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
    ) -> PageInlineComment:
        with self.session_factory.begin() as session:
            page = context_page_repo.get_page_by_id(session, page_id)
            if page is None:
                raise PageNotFoundError("page not found")
            version = self._require_page_version(session, page, anchor.version_id)
            result = self.change_audit_recorder.record_context_page_inline_comment(
                session=session,
                workspace_id=page.workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                page_id=page.id,
                version_id=anchor.version_id,
                note=validate_text(
                    note.strip(), field="note", max_length=5000, min_length=1
                ),
                anchor=anchor.model_dump(mode="json"),
                actor_provenance=actor_provenance,
            )
            operation = session.get(ChangeOperation, result.change_operation_id)
            review = session.get(ReviewItem, result.review_item_id)
            if operation is None or review is None:
                raise PageNotFoundError("inline comment not found")
            current_body = parse_page_frontmatter(version.body_md).body
            return inline_comment_payload(
                page_id=page.id,
                operation=operation,
                review=review,
                anchor=anchor,
                resolved=_synthesize_or_resolve(
                    current_body=current_body,
                    current_content_hash=version.content_hash,
                    current_body_digest=hashlib.sha256(
                        current_body.encode("utf-8")
                    ).hexdigest(),
                    current_version_id=version.id,
                    anchor=anchor,
                ),
            )

    def create_page_first(
        self,
        workspace_slug: str,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        title: str | None = None,
        body_md: str = "",
        page_type: str | None = None,
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        placement_parent_node_id: UUID | None = None,
        with_edit_receipt: bool = False,
        idempotency_key: str | None = None,
    ) -> PagePayload:
        """Create a page with its first shared draft.

        P2.3: used when the caller omits `slug` -- title defaults to "제목
        없음", slug to `p-{page_id}` (computed from a fresh id chosen before
        insert, never a server-side placeholder patched in afterward). This
        never upserts into an existing page the way `write_page`'s
        slug-keyed lookup does: the default slug is a fresh UUID, so it
        cannot collide, and an explicitly-named title is not a lookup key
        here at all -- two calls with the same title still create two pages.
        Empty `body_md` is allowed; publish-time content validation is a
        separate, later check.
        """

        with self.session_factory.begin() as session:
            workspace = self._require_workspace(session, workspace_slug)

            def _execute() -> PagePayload:
                acquire_write_intent(session)
                page_id = uuid4()
                default_slug = f"p-{page_id}"
                prepared = self._prepare_body(
                    BodyInput(
                        body_md=body_md,
                        metadata=PageMetadataInput(
                            title=title,
                            slug=default_slug,
                            page_type=page_type,
                            tags=tags,
                            aliases=aliases,
                        ),
                        fallback_metadata=PreparedMetadata(
                            title="제목 없음",
                            page_type=None,
                            tags=None,
                            aliases=None,
                        ),
                    ),
                    WriteContext(
                        actor=self.actor_context,
                        workspace=workspace,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        actor_provenance=actor_provenance,
                        page=None,
                        version=None,
                        session=session,
                    ),
                )
                next_title = prepared.metadata.title
                page = ContextPage(
                    id=page_id,
                    workspace_id=workspace.id,
                    slug=default_slug,
                    title=next_title,
                    page_type=prepared.metadata.page_type or "overview",
                    tags=prepared.metadata.tags or [],
                    aliases=prepared.metadata.aliases or [],
                )
                session.add(page)
                session.flush()
                version = self._insert_version(
                    session,
                    page_id=page.id,
                    parent_version_id=None,
                    body=prepared,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    version_status="draft",
                )
                # Without this, the page has no stored head at all: an ordinary
                # read_page/list_pages by its own author would 404 immediately
                # after a 201 create, since nothing else ever points at a fresh
                # page's first version for them (confirmed the hard way -- an
                # end-to-end create-then-read test failed until this was added).
                page.current_version_id = version.id
                page.placement_parent_node_id = placement_parent_node_id
                sync_page_link_projection(session, page=page, version=version)
                self._ensure_context_tree_placement(
                    session=session,
                    workspace_id=workspace.id,
                    page=page,
                    parent_id=placement_parent_node_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
                self._refresh_page_links(session, page=page, version=version)
                AuditService().emit(
                    session=session,
                    workspace_id=workspace.id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    action="page.create",
                    target_kind="context_page",
                    target_id=page.id,
                    payload={
                        "version_id": str(version.id),
                        "editing_revision": version.editing_revision,
                    },
                )
                payload = self._payload_from_page(
                    session,
                    page,
                    version,
                    current_version_id_override=version.id,
                    prepared=prepared,
                )
                if placement_parent_node_id is not None:
                    payload._placement_parent_node_id = placement_parent_node_id
                    payload._placement_changed = True
                if with_edit_receipt:
                    payload._edit_receipt = build_edit_receipt(
                        edit_mode="create",
                        before_body=None,
                        after_body=payload.body_md,
                        editing_revision_before=None,
                        editing_revision_after=payload.editing_revision,
                        requested_body=body_md,
                        server_fixes=prepared.fixes,
                    )
                return payload

            return execute_idempotent_write(
                session,
                workspace_id=workspace.id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                tool_name="create_context_page",
                idempotency_key=idempotency_key,
                fingerprint=request_fingerprint(
                    {
                        "title": title,
                        "body_md": body_md,
                        "page_type": page_type,
                        "tags": tags,
                        "aliases": aliases,
                        "placement_parent_node_id": (
                            str(placement_parent_node_id)
                            if placement_parent_node_id is not None
                            else None
                        ),
                    }
                ),
                result_model=PagePayload,
                execute=_execute,
            )

    def begin_context_edit(
        self,
        page_id: UUID,
        workspace_id: UUID,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        idempotency_key: str,
        base_version_id: UUID | None = None,
        base_content_hash: str | None = None,
        retry_only: bool = False,
        chat_session_id: UUID | None = None,
        from_version_id: UUID | list[UUID] | None = None,
        on_conflict: str | None = None,
    ) -> PagePayload:
        """Join a named shared draft or start a new draft from immutable commits."""
        from backend.services.context_draft_workflow import (
            merge_draft_references,
            metadata_dict,
            preview_merge,
            set_parents,
        )

        with self.session_factory.begin() as session:

            def execute():
                acquire_write_intent(session)
                self._lock_context_media_usage_intent_for_page(session, page_id)
                page = context_page_repo.get_page_by_id_for_update(session, page_id)
                if page is None or page.workspace_id != workspace_id:
                    raise PageNotFoundError("page not found")
                ids = (
                    from_version_id
                    if isinstance(from_version_id, list)
                    else ([from_version_id] if from_version_id else [])
                )
                if not ids and page.current_published_version_id:
                    ids = [page.current_published_version_id]
                if not ids:
                    # An omitted from means the published version; a page
                    # without one has no starting point to copy.
                    raise PageValidationError(
                        "This page has no published version; pass from with "
                        "the draft or commit version_id to start from"
                    )
                sources = [
                    self._require_page_version(session, page, key) for key in ids
                ]
                if len(sources) == 1 and sources[0].version_status == "draft":
                    version = sources[0]
                else:
                    if any(not source.commit_oid for source in sources):
                        raise PageValidationError(
                            "Draft starting points must be commits"
                        )
                    source = sources[0] if sources else None
                    if base_version_id is not None and (
                        source is None
                        or source.id != base_version_id
                        or source.content_hash != base_content_hash
                    ):
                        raise PageConflictError(
                            server_version_id=source.id if source else None,
                            server_content_hash=source.content_hash if source else None,
                        )
                    version = self._start_draft_from_commit(
                        session, page, source, actor_kind=actor_kind, actor_id=actor_id
                    )
                    for other in sources[1:]:
                        # The same preparation as bring-in: the merged body
                        # passes the body budget, format and media policy.
                        preview = preview_merge(
                            session,
                            page,
                            version,
                            commit_version_id=other.id,
                            prepare=lambda candidate: self._prepare_merge_candidate(
                                session,
                                page,
                                version,
                                candidate,
                                actor_kind,
                                actor_id,
                                actor_provenance,
                            ),
                        )
                        candidate = preview.choose(on_conflict)
                        version.body_md = candidate.body_md
                        version.content_hash = content_hash(candidate.body_md)
                        version.block_map = candidate.block_map
                        version.page_metadata = metadata_dict(candidate.metadata)
                        merge_draft_references(
                            session,
                            source=version,
                            head=other,
                            target=version,
                            self_origins=self.body_preparer.self_origins,
                        )
                        set_parents(
                            session, version, [*ids[: sources.index(other)], other.id]
                        )
                    if len(sources) > 1:
                        self._sync_version_usage(
                            session, page=page, version=version, body_md=version.body_md
                        )
                self._refresh_page_links(session, page=page, version=version)
                AuditService().emit(
                    session=session,
                    workspace_id=workspace_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    action="page.begin_context_edit",
                    target_kind="context_page",
                    target_id=page.id,
                    payload={
                        "version_id": str(version.id),
                        "base_version_id": str(version.parent_version_id)
                        if version.parent_version_id
                        else None,
                        "editing_revision": version.editing_revision,
                    },
                )
                return self._payload_from_page(
                    session,
                    page,
                    version,
                    current_version_id_override=version.id,
                    prepared=self._inspect_saved_body(
                        session,
                        page,
                        version,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        actor_provenance=actor_provenance,
                    ),
                )

            return execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                tool_name="begin_context_edit",
                idempotency_key=idempotency_key,
                fingerprint=request_fingerprint(
                    {
                        "page_id": str(page_id),
                        "from": from_version_id,
                        "base_version_id": base_version_id,
                        "base_content_hash": base_content_hash,
                        "on_conflict": on_conflict,
                    }
                ),
                result_model=PagePayload,
                execute=execute,
                retry_only=retry_only,
            )

    def submit_context_edit_for_review(
        self,
        page_id: UUID,
        version_id: UUID,
        workspace_id: UUID,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        expected_editing_revision: int,
        idempotency_key: str,
        reason: str = "author submitted for review",
        retry_only: bool = False,
        chat_session_id: UUID | None = None,
        feedback_id: UUID | None = None,
    ) -> PagePayload:
        """Request review of the named shared draft and keep it editable.

        A person's request always waits for a reviewer. An agent's submission
        completes its write like any other agent write: the workspace review
        policy publishes it or leaves it for a person.
        The draft ID remains stable through later saves. An existing pending
        or changes-requested request is returned instead of creating another.
        The idempotency ledger preserves retry receipts for this command.
        """
        if reason is not None:
            validate_text(reason, field="reason", max_length=2000)

        with self.session_factory.begin() as session:
            return self._submit_context_edit_in_session(
                session=session,
                page_id=page_id,
                version_id=version_id,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                expected_editing_revision=expected_editing_revision,
                idempotency_key=idempotency_key,
                reason=reason,
                retry_only=retry_only,
                needs_review=False,
                complete=actor_provenance in AI_PROVENANCES,
                chat_session_id=chat_session_id,
                feedback_id=feedback_id,
            )

    def _submit_context_edit_in_session(
        self,
        *,
        session: Session,
        page_id: UUID,
        version_id: UUID,
        workspace_id: UUID,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        expected_editing_revision: int,
        idempotency_key: str,
        reason: str,
        needs_review: bool,
        force_pending_review: bool = True,
        complete: bool = False,
        prepared: PreparedBody | None = None,
        retry_only: bool = False,
        risk_signals: dict[str, bool] | None = None,
        chat_session_id: UUID | None = None,
        feedback_id: UUID | None = None,
    ) -> PagePayload:
        """Submit in the caller's transaction with its explicit review intent.

        ``complete`` finishes a write instead: the review waits only when
        _completion_requires_review says so, and an approved one is published.
        """
        if reason is not None:
            validate_text(reason, field="reason", max_length=2000)

        def link_feedback(change_set_id: UUID | None) -> None:
            if feedback_id is None:
                return
            feedback = get_in_workspace(
                session,
                FeedbackItem,
                workspace_id=workspace_id,
                resource_id=feedback_id,
            )
            if feedback is None:
                raise ReadNotFoundError("feedback not found")
            feedback.status = "draft_created"
            feedback.resolved_by_change_set_id = change_set_id

        def _execute() -> PagePayload:
            acquire_write_intent(session)
            self._lock_context_media_usage_intent_for_page(session, page_id)
            page = context_page_repo.get_page_by_id_for_update(session, page_id)
            if page is None or page.workspace_id != workspace_id:
                raise PageNotFoundError("page not found")
            version = self._own_page_version(
                session, page, version_id, actor_kind, actor_id
            )
            if version is None or version.version_status != "draft":
                version = self._require_named_open_edit(
                    session, page, version_id, actor_kind, actor_id
                )
            open_reviews = [
                review
                for review in self._context_reviews_for_version(
                    session=session, page=page, version=version
                )
                if review.status in {"pending", "changes_requested"}
            ]
            if open_reviews:
                review = open_reviews[0]
                link_feedback(review.change_set_id)
                return self._payload_from_page(
                    session,
                    page,
                    version,
                    review_item_id=review.id,
                    change_set_id=review.change_set_id,
                    current_version_id_override=version.id,
                )
            if version.editing_revision != expected_editing_revision:
                # Name where the draft stands now, as patch and update do, so
                # the caller knows what to re-read before submitting it.
                raise PageConflictError(
                    server_version_id=version.id,
                    server_content_hash=version.content_hash,
                    server_editing_revision=version.editing_revision,
                    error_code="editing_revision_conflict",
                )
            validate_page_markdown_input(version.body_md)
            from backend.services.document_references import document_references_hash

            parent = (
                get_in_workspace(
                    session,
                    ContextPageVersion,
                    workspace_id=version.workspace_id,
                    resource_id=version.parent_version_id,
                )
                if version.parent_version_id
                else None
            )
            effective_risk_signals = dict(risk_signals or {})
            from backend.services.document_references import (
                authored_document_references,
            )

            has_reference_change = (
                document_references_hash(session, parent)
                != document_references_hash(session, version)
                if parent is not None
                else bool(authored_document_references(session, version))
            )
            if has_reference_change:
                effective_risk_signals["manual_citation"] = True
            pending = (
                self._completion_requires_review(
                    session,
                    page,
                    version,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                )
                if complete
                else force_pending_review
            )
            result = self.change_audit_recorder.record_context_page_write(
                session=session,
                workspace_id=page.workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action=SUBMIT_CONTEXT_EDIT_ACTION,
                page_id=page.id,
                version_id=version.id,
                payload={"reason": reason},
                actor_provenance=actor_provenance,
                body=version.body_md,
                needs_review=needs_review,
                force_pending_review=pending,
                risk_signals=effective_risk_signals,
                origin_chat_session_id=chat_session_id,
            )
            if result.change_set_id is not None:
                record_metadata_snapshot(
                    session,
                    change_set_id=result.change_set_id,
                    page=page,
                    version=version,
                )
            link_feedback(result.change_set_id)
            self._refresh_page_links(session, page=page, version=version)
            review = get_in_workspace(
                session,
                ReviewItem,
                workspace_id=page.workspace_id,
                resource_id=result.review_item_id,
            )
            if complete and review is not None and review.status == "approved":
                from backend.schemas.context_write import PublishContextPageRequest
                from backend.services.context_write_service import ContextWriteService

                published = ContextWriteService(
                    context_version_writer=self.context_version_writer,
                    body_preparer=self.body_preparer,
                    actor_context=self.actor_context,
                    context_media_repository_factory=(
                        self.context_media_repository_factory
                    ),
                ).publish_draft(
                    session=session,
                    workspace_id=page.workspace_id,
                    page_id=page.id,
                    request=PublishContextPageRequest(
                        version_id=version.id, reason=reason
                    ),
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
                final = self._require_page_version(
                    session, page, published.published_version_id
                )
                return self._payload_from_page(
                    session,
                    page,
                    final,
                    review_item_id=result.review_item_id,
                    change_set_id=result.change_set_id,
                    prepared=(
                        prepared
                        if prepared is not None and final.body_md == prepared.body
                        else None
                    ),
                )
            return self._payload_from_page(
                session,
                page,
                version,
                review_item_id=result.review_item_id,
                change_set_id=result.change_set_id,
                current_version_id_override=version.id,
                prepared=self._inspect_saved_body(
                    session,
                    page,
                    version,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                ),
            )

        return execute_idempotent_write(
            session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            tool_name="submit_context_edit_for_review",
            idempotency_key=idempotency_key,
            fingerprint=request_fingerprint(
                {
                    "page_id": str(page_id),
                    "version_id": str(version_id),
                    "expected_editing_revision": expected_editing_revision,
                    "reason": reason,
                    **({"feedback_id": str(feedback_id)} if feedback_id else {}),
                    **({"needs_review": False} if not needs_review else {}),
                    **(
                        {"risk_signals": risk_signals}
                        if risk_signals is not None
                        else {}
                    ),
                }
            ),
            result_model=PagePayload,
            execute=_execute,
            retry_only=retry_only,
        )

    def discard_context_edit(
        self,
        page_id: UUID,
        version_id: UUID,
        workspace_id: UUID,
        actor_kind: str,
        actor_id: str,
        idempotency_key: str,
        retry_only: bool = False,
    ) -> DiscardContextEditResult:
        """Withdraw a shared draft, retaining its immutable content and ancestry."""
        from backend.services.context_lifecycle import record_context_decision

        with self.session_factory.begin() as session:

            def execute():
                acquire_write_intent(session)
                self._lock_context_media_usage_intent_for_page(session, page_id)
                page = context_page_repo.get_page_by_id_for_update(session, page_id)
                if page is None or page.workspace_id != workspace_id:
                    raise PageNotFoundError("page not found")
                version = self._require_named_open_edit(
                    session, page, version_id, actor_kind, actor_id
                )
                record_context_decision(
                    session,
                    action="close",
                    page=page,
                    version=version,
                    context_version_writer=self.context_version_writer,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    reason="Draft withdrawn",
                    event_action="page.discard_context_edit",
                )
                for review in self._context_reviews_for_version(
                    session=session, page=page, version=version
                ):
                    if review.status in {"pending", "changes_requested"}:
                        review.status = "withdrawn"
                self._refresh_page_links(session, page=page, version=version)
                return DiscardContextEditResult(
                    version_id=version.id, page_archived=False
                )

            return execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                tool_name="discard_context_edit",
                idempotency_key=idempotency_key,
                fingerprint=request_fingerprint(
                    {"page_id": str(page_id), "version_id": str(version_id)}
                ),
                result_model=DiscardContextEditResult,
                execute=execute,
                retry_only=retry_only,
            )

    def draft_still_open(
        self, session: Session, page: ContextPage, draft: ContextPageVersion
    ) -> bool:
        return draft.page_id == page.id and draft.version_status == "draft"

    def list_my_editing_versions(
        self,
        workspace_id: UUID,
        actor_kind: str,
        actor_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ListMyContextEditsResult:
        """The open drafts this actor started, across the workspace.

        Read-only, so unlike the write commands above this needs no
        idempotency ledger. Cursor pagination reuses the same opaque
        offset-token shape as ``list_pages`` (``_parse_cursor``/
        ``_encode_cursor`` are page-id-agnostic).
        """

        offset = self._parse_cursor(cursor)
        if limit < 1 or limit > PAGE_LIST_LIMIT_MAX:
            raise PageValidationError(
                f"limit must be between 1 and {PAGE_LIST_LIMIT_MAX}"
            )
        from backend.services.context_draft_workflow import metadata_for

        with self.session_factory() as session:
            contributions = select(
                func.replace(AuditEvent.payload["version_id"].as_string(), "-", "")
            ).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.actor_kind == actor_kind,
                AuditEvent.actor_id == actor_id,
                AuditEvent.action == "context_page.draft.save",
            )
            statement = select_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                conditions=(
                    ContextPageVersion.version_status == "draft",
                    or_(
                        (ContextPageVersion.created_by_actor_kind == actor_kind)
                        & (ContextPageVersion.created_by_actor_id == actor_id),
                        func.replace(
                            ContextPageVersion.id.cast(__import__("sqlalchemy").String),
                            "-",
                            "",
                        ).in_(contributions),
                    ),
                ),
            )
            statement = join_in_workspace(
                statement,
                ContextPageVersion,
                ContextPage,
                workspace_id=workspace_id,
                on=ContextPage.id == ContextPageVersion.page_id,
            ).with_only_columns(ContextPageVersion, ContextPage)
            rows = session.execute(
                statement.order_by(
                    ContextPageVersion.last_saved_at.desc(), ContextPageVersion.id
                )
                .offset(offset)
                .limit(limit + 1)
            ).all()
            visible_rows = rows[:limit]
            items = [
                MyContextEditSummary(
                    page_id=page.id,
                    version_id=version.id,
                    title=metadata_for(page, version).title,
                    slug=page.slug,
                    base_version_id=version.parent_version_id,
                    editing_revision=version.editing_revision,
                    content_hash=version.content_hash,
                    last_saved_at=version.last_saved_at,
                )
                for version, page in visible_rows
            ]
            next_cursor = (
                self._encode_cursor(offset + limit) if len(rows) > limit else None
            )
            return ListMyContextEditsResult(items=items, next_cursor=next_cursor)

    def patch_context_edit(
        self,
        page_id: UUID,
        version_id: UUID,
        workspace_id: UUID,
        operations: list[EditOperation],
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        expected_editing_revision: int,
        idempotency_key: str,
        chat_session_id: UUID | None = None,
    ) -> PagePayload:
        """Apply addressed operations to one named shared draft in place.

        The revision check covers every contributor. The idempotency ledger
        replays a successful receipt before retrying its now-stale revision.
        Body preparation, block identities and projections use the same path
        as a whole-body save.
        """

        with self.session_factory.begin() as session:

            def _execute() -> PagePayload:
                acquire_write_intent(session)
                self._lock_context_media_usage_intent_for_page(session, page_id)
                page = context_page_repo.get_page_by_id_for_update(session, page_id)
                if page is None or page.workspace_id != workspace_id:
                    raise PageNotFoundError("page not found")
                version = self._own_page_version(
                    session, page, version_id, actor_kind, actor_id
                )
                if version is None or version.version_status != "draft":
                    version = self._require_named_open_edit(
                        session, page, version_id, actor_kind, actor_id
                    )
                if version.editing_revision != expected_editing_revision:
                    raise PageConflictError(
                        server_version_id=version.id,
                        server_content_hash=version.content_hash,
                        server_editing_revision=version.editing_revision,
                        error_code="editing_revision_conflict",
                    )

                current_body = version.body_md
                block_only = target_ids_for_operations(operations) is not None
                outline = None if block_only else parse_outline(current_body)

                def relay(exc: ContextEditError) -> ReadValidationError:
                    # Relay the recovery facts: the caller never holds the
                    # document, so a bare failure would force a full re-read.
                    return ReadValidationError(
                        str(exc),
                        error_code=exc.error_code,
                        warnings=list(exc.warnings),
                        details=list(exc.details),
                    )

                def apply_with(block_map):
                    try:
                        return apply_operations_with_block_map(
                            current_body,
                            outline,
                            operations,
                            block_map=block_map,
                            identity_seed=str(version_id),
                        )
                    except ContextEditError as exc:
                        raise relay(exc) from exc

                # A map left describing an older body is rebuilt in memory
                # (and persisted below) rather than refused; read_structure_map
                # derives the same ids. A block-only batch has every addressed
                # block checked against the body by the edit itself, so its
                # stored map is rebuilt only when that check refuses it.
                if block_only and isinstance(version.block_map, list):
                    block_map = version.block_map
                    try:
                        applied = apply_operations_with_block_map(
                            current_body,
                            outline,
                            operations,
                            block_map=block_map,
                            identity_seed=str(version_id),
                        )
                    except ContextEditError as exc:
                        healed, repaired = heal_block_map(
                            current_body, block_map, identity_seed=str(version.id)
                        )
                        if not repaired:
                            raise relay(exc) from exc
                        block_map = healed
                        applied = apply_with(block_map)
                else:
                    block_map, _ = heal_block_map(
                        current_body, version.block_map, identity_seed=str(version.id)
                    )
                    applied = apply_with(block_map)

                prepared = self._autosave_prepare(
                    session=session,
                    page=page,
                    current=version,
                    title=None,
                    body_md=applied.body,
                    page_type=None,
                    tags=None,
                    aliases=None,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                )
                new_body = prepared.body
                next_block_map = (
                    applied.predicted_block_map.value
                    if applied.predicted_block_map is not None
                    else None
                )
                if new_body != applied.body:
                    # The explicit operation proves identity continuity even
                    # when formatting changes a replacement beyond fuzzy
                    # similarity (for example, aligning a rewritten table).
                    next_block_map = compute_block_map_with_ids(
                        new_body,
                        previous_body_md=current_body,
                        previous_block_map=block_map,
                        operation_provenance=block_operation_provenance(applied.edits),
                        identity_seed=str(version.id),
                    )
                baseline_patch = dict(version.page_metadata or {})
                metadata_patch = page_metadata_delta(
                    page,
                    title=prepared.metadata.title,
                    page_type=prepared.metadata.page_type,
                    tags=prepared.metadata.tags,
                    aliases=prepared.metadata.aliases,
                    baseline_patch=baseline_patch,
                )
                payload = self._autosave_persist_editing_row(
                    session=session,
                    page=page,
                    current=version,
                    prepared=prepared,
                    next_title=prepared.metadata.title,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    metadata_patch=metadata_patch,
                    base_block_map=block_map,
                    prepared_block_map=next_block_map,
                )
                AuditService().emit(
                    session=session,
                    workspace_id=page.workspace_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    action="page.patch_context_edit",
                    target_kind="context_page",
                    target_id=page.id,
                    payload={
                        "version_id": str(version.id),
                        "editing_revision": version.editing_revision,
                        "operation_count": len(operations),
                    },
                )
                anchor_operation_count = sum(
                    isinstance(getattr(operation, "at", None), AnchorAddress)
                    for operation in operations
                )
                if anchor_operation_count:
                    # Usage-based deprecation signal for anchor addresses. It
                    # holds no authored anchor text; the chat id supplies the
                    # conversation dimension.
                    AuditService().emit(
                        session=session,
                        workspace_id=page.workspace_id,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        action="mcp.patch_draft.anchor_address_used",
                        target_kind="context_page",
                        target_id=page.id,
                        payload={
                            "conversation_session_id": (
                                str(chat_session_id)
                                if chat_session_id is not None
                                else None
                            ),
                            "operation_count": anchor_operation_count,
                        },
                    )
                payload._edit_receipt = build_edit_receipt(
                    edit_mode="patch",
                    before_body=current_body,
                    after_body=new_body,
                    editing_revision_before=expected_editing_revision,
                    editing_revision_after=version.editing_revision,
                    operations=patch_operation_receipts(
                        body=current_body,
                        operations=operations,
                        edits=applied.edits,
                        block_map=block_map,
                    ),
                    before_block_map=block_map,
                    after_block_map=version.block_map,
                    requested_body=applied.body,
                    server_fixes=prepared.fixes,
                )
                return payload

            return execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                tool_name="patch_context_edit",
                idempotency_key=idempotency_key,
                fingerprint=request_fingerprint(
                    {
                        "page_id": str(page_id),
                        "version_id": str(version_id),
                        "expected_editing_revision": expected_editing_revision,
                        "operations": [
                            operation.model_dump(mode="json")
                            for operation in operations
                        ],
                    }
                ),
                result_model=PagePayload,
                execute=_execute,
            )

    def write_page(
        self,
        workspace_slug: str,
        slug: str,
        title: str,
        body_md: str,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        base_version_id: UUID | None = None,
        base_content_hash: str | None = None,
        placement_parent_node_id: UUID | None = None,
        page_type: str | None = None,
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        as_draft: bool = False,
    ) -> PagePayload:
        with self.session_factory() as lookup_session:
            workspace = self._require_workspace(lookup_session, workspace_slug)
            existing = context_page_repo.get_page_by_slug(
                lookup_session, workspace.id, slug
            )
            if existing is not None:
                source_id = base_version_id or existing.current_published_version_id
                if source_id is not None:
                    source = self._require_page_version(
                        lookup_session, existing, source_id
                    )
                    return self.update_page(
                        page_id=existing.id,
                        title=title,
                        body_md=body_md,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        base_version_id=source_id,
                        base_content_hash=base_content_hash or source.content_hash,
                        actor_provenance=actor_provenance,
                        page_type=page_type,
                        tags=tags,
                        aliases=aliases,
                        destination_node_id=placement_parent_node_id,
                    )
        with self.session_factory.begin() as session:
            # SQLite: FOR UPDATE is a no-op, so take write intent before the
            # guarded read (see db_write_intent). PostgreSQL keeps the row lock.
            acquire_write_intent(session)
            workspace = self._require_workspace(session, workspace_slug)
            self._lock_context_media_usage_intent(session, workspace.id)
            page = context_page_repo.get_page_by_slug_for_update(
                session,
                workspace.id,
                slug,
            )
            authored_body = body_md
            current = None
            if page is not None:
                current = self._require_page_version(
                    session, page, editable_context_version_id(page)
                )
                self._validate_base(
                    session,
                    page,
                    current,
                    base_version_id,
                    base_content_hash,
                )
            prepared = self._prepare_body(
                BodyInput(
                    body_md=body_md,
                    metadata=PageMetadataInput(
                        title=title,
                        slug=slug,
                        page_type=page_type,
                        tags=tags,
                        aliases=aliases,
                    ),
                    fallback_metadata=PreparedMetadata(
                        page_type=page.page_type if page is not None else None,
                        tags=list(page.tags or []) if page is not None else None,
                        aliases=list(page.aliases or []) if page is not None else None,
                    ),
                ),
                WriteContext(
                    actor=self.actor_context,
                    workspace=workspace,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                    page=page,
                    version=current,
                    source_version=current,
                    link_source="draft",
                    session=session,
                ),
            )
            body_md = prepared.body
            page_created = False

            if page is None:
                page, page_created = context_page_repo.insert_page_or_get_locked(
                    session,
                    workspace_id=workspace.id,
                    slug=slug,
                    title=title,
                    page_type=prepared.metadata.page_type or "overview",
                    tags=prepared.metadata.tags or [],
                    aliases=prepared.metadata.aliases or [],
                )
                if not page_created:
                    current = self._require_page_version(
                        session, page, editable_context_version_id(page)
                    )
                    self._validate_base(
                        session,
                        page,
                        current,
                        base_version_id,
                        base_content_hash,
                    )
                    prepared = self._prepare_body(
                        BodyInput(
                            body_md=authored_body,
                            metadata=PageMetadataInput(
                                title=title,
                                slug=slug,
                                page_type=page_type,
                                tags=tags,
                                aliases=aliases,
                            ),
                            fallback_metadata=PreparedMetadata(
                                page_type=page.page_type,
                                tags=list(page.tags or []),
                                aliases=list(page.aliases or []),
                            ),
                        ),
                        WriteContext(
                            actor=self.actor_context,
                            workspace=workspace,
                            actor_kind=actor_kind,
                            actor_id=actor_id,
                            actor_provenance=actor_provenance,
                            page=page,
                            version=current,
                            source_version=current,
                            link_source="draft",
                            session=session,
                        ),
                    )
                    body_md = prepared.body

            requires_human_review = self._stages_as_draft(
                workspace=workspace,
                actor_provenance=actor_provenance,
                body_md=body_md,
            )
            if page_created:
                create_as_draft = requires_human_review or as_draft
                version = self._insert_version(
                    session,
                    page_id=page.id,
                    parent_version_id=None,
                    body=prepared,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    # Insert through the draft state so the partial unique
                    # published-head index is never transiently violated. The
                    # canonical transition below promotes it in this same
                    # transaction for direct browser publication.
                    version_status="draft",
                )
                self._set_current_version(
                    session,
                    page,
                    version,
                    title,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    publish=not create_as_draft,
                )
                self._refresh_page_links(session, page=page, version=version)
                page.placement_parent_node_id = placement_parent_node_id
                self._ensure_context_tree_placement(
                    session=session,
                    workspace_id=workspace.id,
                    page=page,
                    parent_id=placement_parent_node_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
                self.change_audit_recorder.record_context_page_write(
                    session=session,
                    workspace_id=workspace.id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    action="page.create",
                    page_id=page.id,
                    version_id=version.id,
                    payload={"content_hash": version.content_hash},
                    actor_provenance=actor_provenance,
                    body=body_md if requires_human_review or not as_draft else None,
                )
                if not create_as_draft:
                    self._process_publish_maintenance(
                        session, page, version, reason="page.create"
                    )
                return self._payload_from_page(
                    session, page, version, prepared=prepared
                )

            # `write_page`'s upsert-by-slug write targets the page's shared head.
            stage_metadata = should_stage_page_metadata(page, current)
            metadata_patch = page_metadata_delta(
                page,
                title=title,
                page_type=prepared.metadata.page_type,
                tags=prepared.metadata.tags,
                aliases=prepared.metadata.aliases,
                baseline_patch=(
                    pending_page_metadata_patch(
                        session,
                        page=page,
                        version_id=current.id,
                    )
                    if stage_metadata
                    else None
                ),
            )
            metadata_changed = any(
                key in metadata_patch for key in ("page_type", "tags", "aliases")
            )
            title_changed = "title" in metadata_patch
            if (
                placement_parent_node_id is not None
                and placement_parent_node_id != page.placement_parent_node_id
            ):
                metadata_patch["placement_parent_node_id"] = str(
                    placement_parent_node_id
                )
            # Preserve NFC/newline no-ops without ignoring Markdown hard breaks.
            if (
                not _block_map_needs_save(current)
                and same_authored_markdown(body_md, current.body_md)
                and (
                    (current.version_status != "draft" and not requires_human_review)
                    or (
                        current.created_by_actor_kind == actor_kind
                        and current.created_by_actor_id == actor_id
                        and current.version_status == "draft"
                    )
                )
            ):
                if title_changed or metadata_changed:
                    current.last_saved_at = utc_now()
                    if not stage_metadata:
                        apply_page_metadata(
                            page,
                            page_type=prepared.metadata.page_type,
                            tags=prepared.metadata.tags,
                            aliases=prepared.metadata.aliases,
                        )
                    self._set_current_version(
                        session,
                        page,
                        current,
                        title,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                    )
                    if not stage_metadata:
                        self._ensure_context_tree_placement(
                            session=session,
                            workspace_id=workspace.id,
                            page=page,
                            parent_id=placement_parent_node_id,
                            actor_kind=actor_kind,
                            actor_id=actor_id,
                        )
                    result = self.change_audit_recorder.record_context_page_write(
                        session=session,
                        workspace_id=workspace.id,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        action="page.update",
                        page_id=page.id,
                        version_id=current.id,
                        payload={
                            "before_content_hash": current.content_hash,
                            "after_content_hash": current.content_hash,
                            "metadata_only": metadata_changed and not title_changed,
                            "title_only": title_changed and not metadata_changed,
                            "staged_metadata": stage_metadata,
                        },
                        actor_provenance=actor_provenance,
                        body=body_md,
                    )
                    if metadata_patch:
                        stage_page_metadata_patch(
                            session,
                            change_set_id=result.change_set_id,
                            page=page,
                            version=current,
                            patch=metadata_patch,
                        )
                    self._refresh_page_links(session, page=page, version=current)
                return self._payload_from_page(
                    session, page, current, prepared=prepared
                )

            version = self._insert_version(
                session,
                page_id=page.id,
                parent_version_id=current.id,
                body=prepared,
                actor_kind=actor_kind,
                actor_id=actor_id,
                version_status="draft",
            )
            # Direct browser writes publish in this transaction, so their
            # canonical metadata must advance with the published snapshot.
            # Only AI/background writes that remain drafts stage a patch.
            version_stages_metadata = (
                requires_human_review and should_stage_page_metadata(page, version)
            )
            if (
                version_stages_metadata
                and placement_parent_node_id is not None
                and placement_parent_node_id != page.placement_parent_node_id
            ):
                metadata_patch["placement_parent_node_id"] = str(
                    placement_parent_node_id
                )
            if not version_stages_metadata:
                apply_page_metadata(
                    page,
                    page_type=prepared.metadata.page_type,
                    tags=prepared.metadata.tags,
                    aliases=prepared.metadata.aliases,
                )
            self._set_current_version(
                session,
                page,
                version,
                title,
                actor_kind=actor_kind,
                actor_id=actor_id,
                publish=not requires_human_review,
            )
            self._refresh_page_links(session, page=page, version=version)
            if not version_stages_metadata:
                self._ensure_context_tree_placement(
                    session=session,
                    workspace_id=workspace.id,
                    page=page,
                    parent_id=placement_parent_node_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
            result = self.change_audit_recorder.record_context_page_write(
                session=session,
                workspace_id=workspace.id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="page.update",
                page_id=page.id,
                version_id=version.id,
                payload={
                    "before_content_hash": current.content_hash,
                    "after_content_hash": version.content_hash,
                    "parent_version_id": str(current.id),
                },
                actor_provenance=actor_provenance,
                body=body_md,
            )
            if metadata_patch:
                stage_page_metadata_patch(
                    session,
                    change_set_id=result.change_set_id,
                    page=page,
                    version=version,
                    patch=metadata_patch,
                )
            superseded_review_count = (
                self._supersede_unresolved_context_reviews_for_version(
                    session=session,
                    page=page,
                    replaced_version=current,
                    new_change_set_id=result.change_set_id,
                    new_review_item_id=result.review_item_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
            )
            if not requires_human_review:
                self._process_publish_maintenance(
                    session, page, version, reason="page.update"
                )
            return self._payload_from_page(
                session,
                page,
                version,
                superseded_review_count=superseded_review_count,
                prepared=prepared,
            )

    def update_page(
        self,
        page_id: UUID,
        title: str | None,
        body_md: str,
        actor_kind: str,
        actor_id: str,
        base_version_id: UUID,
        base_content_hash: str,
        actor_provenance: str,
        expected_editing_revision: int | None = None,
        on_stale: str = "conflict",
        page_type: str | None = None,
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        destination_node_id: UUID | None = None,
    ) -> PagePayload:
        from backend.services.context_draft_workflow import metadata_for, preview_merge

        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            self._lock_context_media_usage_intent_for_page(session, page_id)
            page = context_page_repo.get_page_by_id_for_update(session, page_id)
            if page is None:
                raise PageNotFoundError("page not found")
            source = self._require_page_version(session, page, base_version_id)
            is_draft = source.version_status == "draft"
            if not is_draft and not source.commit_oid:
                raise PageConflictError(
                    server_version_id=source.id, server_content_hash=source.content_hash
                )
            # A shared draft is checked against the revision the caller read. A
            # commit is a starting point like any other; it merges at publish.
            stale = source.content_hash != base_content_hash or (
                expected_editing_revision is not None
                and source.editing_revision != expected_editing_revision
            )
            preserve = on_stale == "preserve_as_alternative"
            if stale and not preserve:
                raise PageConflictError(
                    server_version_id=source.id,
                    server_content_hash=source.content_hash,
                    server_editing_revision=source.editing_revision,
                )
            source_metadata = metadata_for(page, source)
            if (
                not stale
                and same_authored_markdown(body_md, source.body_md)
                and (title is None or title == source_metadata.title)
                and (page_type is None or page_type == source_metadata.page_type)
                and (tags is None or list(tags) == list(source_metadata.tags))
                and (aliases is None or list(aliases) == list(source_metadata.aliases))
                and (
                    destination_node_id is None
                    or destination_node_id == source_metadata.placement_parent_node_id
                )
            ):
                return self._payload_from_page(session, page, source)
            # A stale write is kept as its own draft from the commit the shared
            # draft started from; it is neither published nor sent to review.
            start_id = source.parent_version_id if stale and is_draft else source.id
            if is_draft and not stale:
                version = source
            elif start_id is None:
                raise PageConflictError(
                    server_version_id=source.id,
                    server_content_hash=source.content_hash,
                    server_editing_revision=source.editing_revision,
                )
            else:
                version = self._start_draft_from_commit(
                    session,
                    page,
                    self._require_page_version(session, page, start_id),
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
            prepared = self._autosave_prepare(
                session=session,
                page=page,
                current=version,
                title=title,
                body_md=body_md,
                page_type=page_type,
                tags=tags,
                aliases=aliases,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
            )
            patch = {}
            if destination_node_id is not None:
                patch["placement_parent_node_id"] = str(destination_node_id)
            payload = self._autosave_persist_editing_row(
                session=session,
                page=page,
                current=version,
                prepared=prepared,
                next_title=prepared.metadata.title,
                actor_kind=actor_kind,
                actor_id=actor_id,
                metadata_patch=patch,
            )
            merge = None if stale else preview_merge(session, page, version)
            if stale or (preserve and merge.clean is None):
                payload.alternative_draft_id = version.id
                return payload
            # A direct write that does not merge with the published version is
            # refused, not queued: its caller resolves the conflict.
            merge.choose(None)
            # The write is recorded like a submission and completed by the one
            # rule every writer shares (_completion_requires_review).
            return self._submit_context_edit_in_session(
                session=session,
                page_id=page.id,
                version_id=version.id,
                workspace_id=page.workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                expected_editing_revision=version.editing_revision,
                idempotency_key=f"page-update:{version.id}:{version.editing_revision}",
                reason="page.update",
                needs_review=False,
                complete=True,
                prepared=prepared,
            )

    def _refresh_page_links(
        self, session: Session, *, page: ContextPage, version: ContextPageVersion
    ) -> None:
        self_origins = getattr(self.body_preparer, "self_origins", None)
        refresh_page_links(
            session,
            page=page,
            version=version,
            self_origins=self_origins,
        )

    def _prepare_body(
        self,
        input: BodyInput,
        ctx: WriteContext,
    ) -> PreparedBody:
        try:
            return self.body_preparer.prepare(input, ctx)
        except MarkdownMediaPolicyError as exc:
            raise PageMediaPolicyError(str(exc), reason_code=exc.reason_code) from exc

    def _insert_version(
        self, session: Session, *, body: PreparedBody, **kwargs
    ) -> ContextPageVersion:
        repository = (
            self.context_media_repository_factory(session)
            if self.context_media_repository_factory is not None
            else None
        )
        try:
            parent_id = kwargs.get("parent_version_id")
            source = (
                context_page_repo.get_version(session, parent_id) if parent_id else None
            )
            # A whole-body save may fork a reviewed/restored version. Repair
            # the input map here as part of saving authored bytes, preserving
            # matching identities without modifying the historical source.
            if (
                source is not None
                and kwargs.get("prepared_block_map") is None
                and _block_map_needs_save(source)
            ):
                previous_map, _ = heal_block_map(
                    source.body_md, source.block_map, identity_seed=str(source.id)
                )
                kwargs["prepared_block_map"] = compute_block_map_with_ids(
                    body.body,
                    previous_body_md=source.body_md,
                    previous_block_map=previous_map,
                    identity_seed=kwargs.get("identity_seed"),
                )
            version = context_page_repo.insert_version(
                session,
                body=body,
                context_media_repository=repository,
                **kwargs,
            )
            if source is not None:
                from backend.services.context_version_copy import (
                    copy_context_version_references,
                )

                copy_context_version_references(session, source=source, target=version)
            from backend.services.context_draft_workflow import (
                metadata_dict,
                metadata_for,
            )

            owner = get_in_workspace(
                session,
                ContextPage,
                workspace_id=version.workspace_id,
                resource_id=version.page_id,
            )
            version.page_metadata = (
                metadata_dict(metadata_for(owner, source)) if owner is not None else {}
            )
            version.page_metadata.update(
                {
                    "title": body.metadata.title or (owner.title if owner else ""),
                    "page_type": body.metadata.page_type
                    or (owner.page_type if owner else "overview"),
                    "tags": list(body.metadata.tags or []),
                    "aliases": list(body.metadata.aliases or []),
                }
            )
            return version
        except MarkdownMediaPolicyError as exc:
            raise PageMediaPolicyError(str(exc), reason_code=exc.reason_code) from exc
        except ContextMediaError as exc:
            raise PageContextMediaError(str(exc), error_code=exc.error_code) from exc
        except ValueError as exc:
            if "dtx-media://asset/" in body.body:
                raise PageValidationError(str(exc)) from exc
            raise

    def _insert_version_copy(
        self,
        session: Session,
        *,
        source_version: ContextPageVersion | None,
        **kwargs,
    ) -> ContextPageVersion:
        repository = (
            self.context_media_repository_factory(session)
            if self.context_media_repository_factory is not None
            else None
        )
        try:
            version = context_page_repo.insert_version_copy(
                session,
                source_version=source_version,
                context_media_repository=repository,
                **kwargs,
            )
            if source_version is not None:
                from backend.services.context_version_copy import (
                    copy_context_version_references,
                )

                copy_context_version_references(
                    session, source=source_version, target=version
                )
            from backend.services.context_draft_workflow import (
                metadata_dict,
                metadata_for,
            )

            owner = get_in_workspace(
                session,
                ContextPage,
                workspace_id=version.workspace_id,
                resource_id=version.page_id,
            )
            version.page_metadata = (
                metadata_dict(metadata_for(owner, source_version))
                if owner is not None
                else {}
            )
            return version
        except MarkdownMediaPolicyError as exc:
            raise PageMediaPolicyError(str(exc), reason_code=exc.reason_code) from exc

        except ContextMediaError as exc:
            raise PageContextMediaError(str(exc), error_code=exc.error_code) from exc
        except ValueError as exc:
            if (
                source_version is not None
                and "dtx-media://asset/" in source_version.body_md
            ):
                raise PageValidationError(str(exc)) from exc
            raise

    def _start_draft_from_commit(
        self,
        session: Session,
        page: ContextPage,
        source: ContextPageVersion,
        *,
        actor_kind: str,
        actor_id: str,
    ) -> ContextPageVersion:
        """A new shared draft whose first parent is the commit it starts from."""
        from backend.services.context_draft_workflow import set_parents

        version = self._insert_version_copy(
            session,
            page_id=page.id,
            parent_version_id=source.id,
            source_version=source,
            actor_kind=actor_kind,
            actor_id=actor_id,
            version_status="draft",
        )
        set_parents(session, version, [source.id])
        return version

    def _lock_context_media_usage_intent(
        self,
        session: Session,
        workspace_id: UUID,
    ) -> None:
        """Acquire the workspace media lock before any ContextPage row lock."""

        if self.context_media_repository_factory is None:
            return
        self.context_media_repository_factory(session).lock_workspace_for_usage(
            workspace_id
        )

    def _lock_context_media_usage_intent_for_page(
        self,
        session: Session,
        page_id: UUID,
    ) -> None:
        if self.context_media_repository_factory is None:
            return
        workspace_id = session.scalar(
            select(ContextPage.workspace_id).where(ContextPage.id == page_id)
        )
        if workspace_id is None:
            raise PageNotFoundError("page not found")
        self._lock_context_media_usage_intent(session, workspace_id)

    def _sync_version_usage(
        self,
        session: Session,
        *,
        page: ContextPage,
        version: ContextPageVersion,
        body_md: str,
    ) -> None:
        self._validate_media_markdown(body_md)
        if self.context_media_repository_factory is None:
            if "dtx-media://asset/" in body_md:
                raise PageValidationError("Context media is not configured")
            return
        try:
            SyncVersionUsage().execute(
                self.context_media_repository_factory(session),
                workspace_id=page.workspace_id,
                page_id=page.id,
                page_version_id=version.id,
                body_markdown=body_md,
            )
        except MarkdownMediaPolicyError as exc:
            raise PageMediaPolicyError(str(exc), reason_code=exc.reason_code) from exc
        except ContextMediaError as exc:
            raise PageContextMediaError(str(exc), error_code=exc.error_code) from exc
        except ValueError as exc:
            raise PageValidationError(str(exc)) from exc

    @staticmethod
    def _validate_media_markdown(body_md: str) -> None:
        try:
            validate_markdown_media_policy(body_md)
        except MarkdownMediaPolicyError as exc:
            raise PageMediaPolicyError(str(exc), reason_code=exc.reason_code) from exc

    def _process_publish_maintenance(
        self, session: Session, page: ContextPage, version, *, reason: str
    ) -> None:
        """Keep publish maintenance symmetric across direct-write surfaces.

        The embed job is written in the page's data transaction on purpose:
        page commit and job creation remain atomic in Tier-B. A split provider
        must relay this domain trigger through the S0 outbox instead of moving
        the job write to a separate control transaction.
        """
        from backend.services.change_propagation import (
            process_page_publish_maintenance,
        )

        process_page_publish_maintenance(
            session,
            page=page,
            published_version_id=version.id,
            reason=reason,
        )
        marker = "draft" if version.version_status == "draft" else "published"
        self.job_queue.enqueue(
            session=session,
            workspace_id=page.workspace_id,
            job_type="embed",
            trigger_type=f"context_{marker}",
            idempotency_key=context_page_embed_idempotency_key(
                page.id,
                marker,
                version.id,
                version.content_hash,
            ),
            target_ref={"kind": "context_page", "id": str(page.id)},
        )

    def autosave_page(
        self,
        page_id: UUID,
        workspace_id: UUID,
        title: str | None,
        body_md: str,
        actor_kind: str,
        actor_id: str,
        base_version_id: UUID | None,
        base_content_hash: str | None,
        actor_provenance: str,
        idempotency_key: str,
        expected_editing_revision: int | None = None,
        session_version_id: UUID | None = None,
        on_stale: str = "conflict",
        page_type: str | None = None,
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        destination_node_id: UUID | None = None,
        retry_only: bool = False,
        with_edit_receipt: bool = False,
        editing_version_id: UUID | None = None,
        version_id: UUID | None = None,
        base_snapshot: dict | None = None,
        on_conflict: str | None = None,
    ) -> PagePayload:
        from dataclasses import replace

        from backend.app.context_version_storage import build_context_merger
        from backend.services.context_draft_workflow import (
            metadata_dict,
            metadata_for,
            snapshot,
        )

        target_id = (
            version_id or editing_version_id or session_version_id or base_version_id
        )
        with self.session_factory.begin() as session:

            def execute():
                acquire_write_intent(session)
                self._lock_context_media_usage_intent_for_page(session, page_id)
                page = context_page_repo.get_page_by_id_for_update(session, page_id)
                if page is None or page.workspace_id != workspace_id:
                    raise PageNotFoundError("page not found")
                if target_id is None:
                    raise PageValidationError("version_id is required to save a draft")
                current = self._require_page_version(session, page, target_id)
                expected_revision = expected_editing_revision
                if current.version_status in {
                    "published_snapshot",
                    "superseded_snapshot",
                }:
                    # Saving onto a published version starts a draft from the
                    # version the editor read, so opening an editor creates
                    # nothing. An ended draft (closed, discarded) still refuses.
                    if expected_revision != current.editing_revision or (
                        base_content_hash is not None
                        and base_content_hash != current.content_hash
                    ):
                        raise PageConflictError(
                            server_version_id=current.id,
                            server_content_hash=current.content_hash,
                            server_editing_revision=current.editing_revision,
                            error_code="editing_revision_conflict",
                        )
                    current = self._start_draft_from_commit(
                        session, page, current, actor_kind=actor_kind, actor_id=actor_id
                    )
                    expected_revision = current.editing_revision
                current = self._require_named_open_edit(
                    session, page, current.id, actor_kind, actor_id
                )
                before, revision_before = current.body_md, current.editing_revision
                prepared = self._autosave_prepare(
                    session=session,
                    page=page,
                    current=current,
                    title=title,
                    body_md=body_md,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    page_type=page_type,
                    tags=tags,
                    aliases=aliases,
                    actor_provenance=actor_provenance,
                )
                data = metadata_dict(metadata_for(page, current))
                data.update(
                    {
                        "title": prepared.metadata.title,
                        "page_type": prepared.metadata.page_type,
                        "tags": prepared.metadata.tags or [],
                        "aliases": prepared.metadata.aliases or [],
                    }
                )
                if destination_node_id is not None:
                    data["placement_parent_node_id"] = str(destination_node_id)
                stale = expected_revision != revision_before or (
                    base_content_hash is not None
                    and base_content_hash != current.content_hash
                )
                if base_snapshot is not None and (
                    base_snapshot.get("body_md") != before
                    or base_snapshot.get("page_metadata")
                    != metadata_dict(metadata_for(page, current))
                ):
                    stale = True
                merged = False
                if stale:
                    if base_snapshot is None:
                        raise PageConflictError(
                            server_version_id=current.id,
                            server_content_hash=current.content_hash,
                            server_editing_revision=current.editing_revision,
                            error_code="editing_revision_conflict",
                        )
                    if actor_provenance != "browser_session":
                        raise PageValidationError(
                            "base_snapshot is only supported for browser saves"
                        )
                    base_data = base_snapshot.get("page_metadata") or {}
                    old_metadata = current.page_metadata
                    try:
                        current.page_metadata = base_data
                        base = replace(
                            snapshot(page, current), body_md=base_snapshot["body_md"]
                        )
                        current.page_metadata = data
                        ours = replace(
                            snapshot(page, current),
                            body_md=prepared.body,
                            last_saved_at=utc_now(),
                        )
                    finally:
                        current.page_metadata = old_metadata
                    outcome = build_context_merger().merge(
                        base,
                        ours,
                        snapshot(page, current),
                        published_side="theirs",
                        identity_seed=f"context-save:{current.id}:{revision_before}",
                    )
                    candidate = (
                        getattr(outcome, on_conflict)
                        if on_conflict in {"keep_both", "latest"}
                        else outcome.clean
                    )
                    if candidate is None:
                        raise PageConflictError(
                            server_version_id=current.id,
                            server_content_hash=current.content_hash,
                            server_editing_revision=current.editing_revision,
                            error_code="context_merge_conflict",
                        )
                    prepared = self._autosave_prepare(
                        session=session,
                        page=page,
                        current=current,
                        title=candidate.metadata.title,
                        body_md=candidate.body_md,
                        page_type=candidate.metadata.page_type,
                        tags=list(candidate.metadata.tags),
                        aliases=list(candidate.metadata.aliases),
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        actor_provenance=actor_provenance,
                    )
                    data = metadata_dict(candidate.metadata)
                    merged = True
                result = self._autosave_persist_editing_row(
                    session=session,
                    page=page,
                    current=current,
                    prepared=prepared,
                    next_title=prepared.metadata.title,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    metadata_patch=data,
                )
                result.autosave_merged = merged
                if with_edit_receipt:
                    result._edit_receipt = build_edit_receipt(
                        edit_mode="whole_body",
                        before_body=before,
                        after_body=result.body_md,
                        editing_revision_before=revision_before,
                        editing_revision_after=result.editing_revision,
                        requested_body=body_md,
                        server_fixes=prepared.fixes,
                    )
                return result

            return execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                tool_name="autosave_page",
                idempotency_key=idempotency_key,
                fingerprint=request_fingerprint(
                    {
                        "page_id": page_id,
                        "version_id": target_id,
                        "body_md": body_md,
                        "title": title,
                        "page_type": page_type,
                        "tags": tags,
                        "aliases": aliases,
                        "destination_node_id": destination_node_id,
                        "expected_editing_revision": expected_editing_revision,
                        "base_content_hash": base_content_hash,
                        "base_snapshot": base_snapshot,
                        "on_conflict": on_conflict,
                    }
                ),
                result_model=PagePayload,
                execute=execute,
                retry_only=retry_only,
            )

    def _save_prepared_context_edit_in_session(
        self,
        *,
        session: Session,
        page_id: UUID,
        workspace_id: UUID,
        editing_version_id: UUID,
        prepared: PreparedBody,
        base_content_hash: str,
        expected_editing_revision: int,
        actor_kind: str,
        actor_id: str,
    ) -> PagePayload:
        """Persist a trusted preparation without reinterpreting its source.

        The caller owns the transaction and idempotency savepoint. Use the
        ordinary autosave ownership, CAS, persistence and receipt helpers,
        while leaving metadata and the prepared Markdown unchanged.
        """
        if not session.in_transaction():
            raise RuntimeError("Prepared edit requires an active caller transaction")
        if not isinstance(prepared, PreparedBody):
            raise TypeError("Prepared edit requires a PreparedBody")
        validate_body(prepared.body)
        self._validate_media_markdown(prepared.body)
        if content_hash(prepared.body) != prepared.content_hash:
            raise RuntimeError("Prepared edit body and hash do not match")
        page = self._autosave_lock_and_load(
            session=session,
            page_id=page_id,
            editing_version_id=editing_version_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if page.workspace_id != workspace_id:
            raise PageNotFoundError("page not found")
        current = self._require_page_version(session, page, editing_version_id)
        self._validate_base(
            session,
            page,
            current,
            editing_version_id,
            base_content_hash,
            expected_editing_revision,
        )
        receipt_base = {
            "body": current.body_md,
            "revision": current.editing_revision,
            "requested": prepared.body,
            "fixes": prepared.fixes,
        }
        result = self._autosave_persist_editing_row(
            session=session,
            page=page,
            current=current,
            prepared=prepared,
            next_title=page.title,
            actor_kind=actor_kind,
            actor_id=actor_id,
            metadata_patch={},
        )
        return self._autosave_receipt(
            result=result, receipt_base=receipt_base, with_edit_receipt=True
        )

    def _autosave_lock_and_load(
        self,
        *,
        session: Session,
        page_id: UUID,
        editing_version_id: UUID | None,
        actor_kind: str,
        actor_id: str,
    ) -> ContextPage:
        # SQLite: FOR UPDATE is a no-op, so take write intent before the
        # guarded read. This path rewrites the draft row in place below,
        # which is what a concurrent publish_draft is checking the hash of.
        acquire_write_intent(session)
        self._lock_context_media_usage_intent_for_page(session, page_id)
        page = context_page_repo.get_page_by_id_for_update(session, page_id)
        if page is None:
            raise PageNotFoundError("page not found")
        if editing_version_id is not None:
            self._require_named_open_edit(
                session, page, editing_version_id, actor_kind, actor_id
            )
        return page

    def _autosave_prepare(
        self,
        *,
        session: Session,
        page: ContextPage,
        current: ContextPageVersion,
        title: str | None,
        body_md: str,
        page_type: str | None,
        tags: list[str] | None,
        aliases: list[str] | None,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        source_version: ContextPageVersion | None = None,
    ) -> PreparedBody:
        workspace = self._workspace_for(session, page.workspace_id)
        effective_title, effective_type, effective_tags, effective_aliases = (
            effective_metadata_for_payload(session, page, source_version or current)
        )
        return self._prepare_body(
            BodyInput(
                body_md=body_md,
                metadata=PageMetadataInput(
                    title=title,
                    slug=page.slug,
                    page_type=page_type,
                    tags=tags,
                    aliases=aliases,
                ),
                fallback_metadata=PreparedMetadata(
                    title=effective_title,
                    page_type=effective_type,
                    tags=effective_tags,
                    aliases=effective_aliases,
                ),
                baseline_body_md=(source_version or current).body_md,
            ),
            WriteContext(
                actor=self.actor_context,
                workspace=workspace,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                page=page,
                version=current,
                source_version=source_version or current,
                session=session,
            ),
        )

    def _autosave_persist_editing_row(
        self,
        *,
        session: Session,
        page: ContextPage,
        current: ContextPageVersion,
        prepared: PreparedBody,
        next_title: str,
        actor_kind: str,
        actor_id: str,
        metadata_patch: dict[str, object],
        base_block_map=None,
        prepared_block_map=None,
    ) -> PagePayload:
        from backend.services.context_draft_workflow import (
            metadata_dict,
            metadata_for,
            record_draft_save,
        )

        if current.version_status != "draft":
            raise PageConflictError(
                server_version_id=current.id, server_content_hash=current.content_hash
            )
        data = metadata_dict(metadata_for(page, current))
        data.update(
            {
                "title": next_title,
                "page_type": prepared.metadata.page_type or data["page_type"],
                "tags": list(
                    prepared.metadata.tags
                    if prepared.metadata.tags is not None
                    else data["tags"]
                ),
                "aliases": list(
                    prepared.metadata.aliases
                    if prepared.metadata.aliases is not None
                    else data["aliases"]
                ),
            }
        )
        data.update(metadata_patch)
        if (
            prepared.body == current.body_md
            and not _block_map_needs_save(current)
            and data == metadata_dict(metadata_for(page, current))
            and (prepared_block_map is None or prepared_block_map == current.block_map)
        ):
            return self._payload_from_page(session, page, current, prepared=prepared)
        self._rewrite_body_in_place(
            current,
            prepared,
            base_block_map=base_block_map,
            prepared_block_map=prepared_block_map,
        )
        current.editing_revision += 1
        current.page_metadata = data
        current.actor_display_name = resolve_actor_display_name(
            session, actor_kind=actor_kind, actor_id=actor_id
        )
        change_set = record_draft_save(
            session,
            page,
            current,
            actor_kind=actor_kind,
            actor_id=actor_id,
            lint_registry=self.lint_registry,
        )
        self._sync_version_usage(
            session, page=page, version=current, body_md=prepared.body
        )
        self._refresh_page_links(session, page=page, version=current)
        self.job_queue.enqueue(
            session=session,
            workspace_id=page.workspace_id,
            job_type="embed",
            trigger_type="context_draft",
            idempotency_key=f"context-draft:{current.id}:{current.editing_revision}",
            target_ref={"kind": "context_page", "id": str(page.id)},
        )
        # _rewrite_body_in_place stored a map built for this exact body.
        return self._payload_from_page(
            session,
            page,
            current,
            change_set_id=change_set.id,
            current_version_id_override=current.id,
            prepared=prepared,
            block_map_stale=False,
        )

    @staticmethod
    def _autosave_receipt(
        *,
        result: PagePayload,
        receipt_base: dict[str, object],
        with_edit_receipt: bool,
    ) -> PagePayload:
        if "placement_parent_node_id" in receipt_base:
            result._placement_parent_node_id = receipt_base["placement_parent_node_id"]
            result._placement_changed = True
        if with_edit_receipt and receipt_base:
            before_body = receipt_base["body"]
            revision_before = receipt_base["revision"]
            result._edit_receipt = build_edit_receipt(
                edit_mode="whole_body",
                before_body=before_body if isinstance(before_body, str) else None,
                after_body=result.body_md,
                editing_revision_before=(
                    revision_before if isinstance(revision_before, int) else None
                ),
                editing_revision_after=result.editing_revision,
                requested_body=str(receipt_base["requested"]),
                server_fixes=tuple(receipt_base["fixes"]),
            )
        return result

    def _supersede_unresolved_context_reviews_for_version(
        self,
        *,
        session: Session,
        page: ContextPage,
        replaced_version: ContextPageVersion,
        new_change_set_id: UUID,
        new_review_item_id: UUID,
        actor_kind: str,
        actor_id: str,
    ) -> int:
        pending_reviews = (
            session.scalars(
                select(ReviewItem)
                .join(ChangeSet, ChangeSet.id == ReviewItem.change_set_id)
                .join(
                    ChangeOperation,
                    ChangeOperation.change_set_id == ChangeSet.id,
                )
                .where(
                    ReviewItem.workspace_id == page.workspace_id,
                    ChangeSet.workspace_id == page.workspace_id,
                    ReviewItem.status.in_(("pending", "changes_requested")),
                    ReviewItem.review_type == "context_update",
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page.id,
                    ChangeOperation.diff_ref
                    == f"context_page_version:{replaced_version.id}",
                    ReviewItem.id != new_review_item_id,
                )
            )
            .unique()
            .all()
        )
        for review in pending_reviews:
            supersede_review_with_lineage(
                session=session,
                audit_service=self.audit_service,
                workspace_id=page.workspace_id,
                review=review,
                new_change_set_id=new_change_set_id,
                new_review_item_id=new_review_item_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                audit_action="review.superseded_by_human_page_write",
            )
        return len(pending_reviews)

    @staticmethod
    def _context_reviews_for_version(
        *,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
    ) -> list[ReviewItem]:
        """Return every review bound to one exact version.

        This deliberately does not select every review for the page. Inline
        comments, deleted-source notices, alternatives, and reviews for other
        versions are independent work and must survive a browser keystroke.
        """

        return list(
            session.scalars(
                select(ReviewItem)
                .join(ChangeSet, ChangeSet.id == ReviewItem.change_set_id)
                .join(
                    ChangeOperation,
                    ChangeOperation.change_set_id == ChangeSet.id,
                )
                .where(
                    ReviewItem.workspace_id == page.workspace_id,
                    ChangeSet.workspace_id == page.workspace_id,
                    ReviewItem.review_type == "context_update",
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page.id,
                    ChangeOperation.diff_ref == f"context_page_version:{version.id}",
                )
            )
            .unique()
            .all()
        )

    def _can_submit_for_review(
        self,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
        actor: ActorContext | None,
    ) -> bool:
        return (
            actor is not None
            and actor_can_read_drafts(actor)
            and version.version_status == "draft"
        )

    @staticmethod
    def _rewrite_body_in_place(
        version: ContextPageVersion,
        prepared: PreparedBody,
        *,
        base_block_map: list[dict] | None = None,
        prepared_block_map: list[dict] | None = None,
    ) -> None:
        """Replace a mutable row's body together with the map describing it.

        ``block_map`` is a durable claim about ``body_md``: read_structure_map
        hands its ids out and the next block-addressed patch validates against
        it. Only row insertion used to compute it, so every in-place save left
        it describing an earlier body. ``prepared_block_map`` is the map a
        block-addressed patch already proved; otherwise it is derived from the
        previous body and (healed) map, carrying ids of unchanged blocks.
        """

        if not isinstance(prepared, PreparedBody):
            raise TypeError("_rewrite_body_in_place requires a PreparedBody")
        body_md = prepared.body
        validate_page_markdown_input(body_md)
        if prepared_block_map is None:
            previous_map = base_block_map
            if previous_map is None:
                previous_map, _repaired = heal_block_map(
                    version.body_md,
                    version.block_map,
                    identity_seed=str(version.id),
                )
            prepared_block_map = compute_block_map_with_ids(
                body_md,
                previous_body_md=version.body_md,
                previous_block_map=previous_map,
                identity_seed=str(version.id),
            )
        version.block_map = prepared_block_map
        version.body_md = body_md
        version.content_hash = prepared.content_hash
        version.last_saved_at = utc_now()

    def _set_current_version(
        self,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
        title: str | None = None,
        *,
        actor_kind: str,
        actor_id: str,
        publish: bool | None = None,
    ) -> ContextPage:
        """Point ``current_version_id`` at ``version`` (optionally retitling).

        Only a non-draft snapshot may move the published head: an unpublished
        draft (e.g. a staged restore) must never be promoted to what
        read_public_page serves by a metadata/title-only save or an in-place
        draft autosave — publishing a draft is an explicit step.
        """
        should_publish = (
            version.version_status != "draft" if publish is None else publish
        )
        if not should_publish:
            if page.lifecycle_status != "archived":
                page.lifecycle_status = (
                    "published" if page.current_published_version_id else "draft"
                )
            session.add(page)
            session.flush()
            return page
        try:
            record_context_decision(
                session,
                action="publish",
                context_version_writer=self.context_version_writer,
                page=page,
                version=version,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor=self.actor_context,
                reason="Context content published",
                audit_service=self.audit_service,
                self_origins=self.body_preparer.self_origins,
            )
        except ContextStorageServiceError as exc:
            raise PageContextStorageError(exc.error_code) from exc
        except LegacyMediaCutoverConflictError as exc:
            raise PageContextMediaError(
                str(exc),
                error_code=exc.error_code,
                page_id=page.id,
            ) from exc
        if title is not None:
            page.title = title
        session.add(page)
        session.flush()
        return page

    @staticmethod
    def _ensure_context_tree_placement(
        *,
        session: Session,
        workspace_id: UUID,
        page: ContextPage,
        parent_id: UUID | None,
        actor_kind: str,
        actor_id: str,
        move_existing: bool = False,
    ) -> None:
        try:
            ensure_primary_tree_placement(
                session=session,
                workspace_id=workspace_id,
                target_kind="context_page",
                target_id=page.id,
                title=page.title,
                slug=page.slug,
                section="context",
                parent_id=parent_id,
                default_parent_slug="inbox",
                actor_kind=actor_kind,
                actor_id=actor_id,
                can_have_children=True,
                move_existing=move_existing,
            )
        except ReadServiceError as exc:
            requested = str(parent_id) if parent_id is not None else "default Inbox"
            raise PageValidationError(
                f"placement parent {requested} is invalid: {exc}. "
                "Walk get_index(format='nodes') and choose a node_id there; "
                "the page write was rolled back.",
                cause_error_code=exc.error_code,
            ) from exc

    def _require_workspace(self, session: Session, workspace_slug: str):
        workspace = workspace_repo.get_workspace_by_slug(session, workspace_slug)
        if workspace is None:
            raise PageNotFoundError(f"workspace not found: {workspace_slug}")
        return workspace

    def _fails_objective_floor(self, body_md: str | None) -> bool:
        """Whether the prepared body is structurally malformed.

        The floor is one rule -- an unclosed code fence -- chosen because it has
        no false positives. Auto-fix runs before this is asked, so a defect it
        could repair is already gone; the fence is not one it repairs, which is
        why a failure here means the defect genuinely remains.
        """
        return bool(body_md) and bool(
            publish_floor_findings(body_md, registry=self.lint_registry)
        )

    def _completion_requires_review(
        self,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
        *,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
    ) -> bool:
        """Whether finishing this write waits for a person.

        Judged on the prepared result the publication would store: the
        objective floor and the workspace switch (_stages_as_draft), then the
        review policy for an agent. A result that cannot merge on its own
        waits too.
        """
        from backend.schemas.context_write import PublishContextPageRequest
        from backend.services.context_draft_workflow import (
            direct_publication_risk,
            publication_candidate,
        )

        try:
            _, candidate = publication_candidate(
                session,
                page,
                version,
                PublishContextPageRequest(version_id=version.id, reason="complete"),
                prepare=lambda merged: self._prepare_merge_candidate(
                    session,
                    page,
                    version,
                    merged,
                    actor_kind,
                    actor_id,
                    actor_provenance,
                ),
            )
        except StaleBaseVersionError as exc:
            # Only a merge conflict can surface here (the request pins no
            # preview); a person chooses how it resolves.
            if exc.error_code != "context_merge_conflict":
                raise
            return True
        workspace = self._workspace_for(session, page.workspace_id)
        return self._stages_as_draft(
            workspace=workspace,
            actor_provenance=actor_provenance,
            body_md=candidate.body_md,
        ) or (
            actor_provenance in AI_PROVENANCES
            and direct_publication_risk(
                session,
                page,
                candidate,
                workspace=workspace,
                reviews=[],
                actor_provenance=actor_provenance,
                risk_resolver=self.change_audit_recorder.risk_resolver,
                lint_registry=self.lint_registry,
            ).review_required
        )

    def _stages_as_draft(
        self,
        *,
        workspace,
        actor_provenance: str,
        body_md: str | None,
    ) -> bool:
        """Whether this write waits instead of going live.

        Two separate reasons, in this order.

        A body that fails the objective floor never moves the published pointer,
        whoever wrote it. This is an invariant, not a policy: publishing a
        malformed body and *then* opening a review for it is the worst of both
        answers, because the content is already live and rejecting the review
        cannot un-publish it. It is checked first precisely so no setting and no
        provenance can get in front of it.

        The workspace's trust switch then answers the ordinary agent case.
        """
        if self._fails_objective_floor(body_md):
            return True
        if actor_provenance not in AI_PROVENANCES:
            return False
        return ai_writes_require_human_review(workspace)

    def _workspace_for(self, session: Session, workspace_id: UUID):
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise PageNotFoundError("workspace not found")
        return workspace

    def _parse_cursor(self, cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            raw = _decode_urlsafe_base64(cursor)
            payload = json.loads(raw)
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PageValidationError(
                "cursor must be an opaque pagination token"
            ) from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise PageValidationError("cursor must be an opaque pagination token")
        offset = payload.get("offset")
        if not isinstance(offset, int):
            raise PageValidationError("cursor must be an opaque pagination token")
        return self._validate_cursor_offset(offset)

    def _validate_cursor_offset(self, offset: int) -> int:
        if offset < 0:
            raise PageValidationError("cursor must be a non-negative integer")
        if offset > PAGE_LIST_CURSOR_MAX_OFFSET:
            raise PageValidationError(
                f"cursor must be between 0 and {PAGE_LIST_CURSOR_MAX_OFFSET}"
            )
        return offset

    def _encode_cursor(self, offset: int) -> str:
        payload = json.dumps(
            {"v": 1, "offset": offset},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    def _parse_version_cursor(self, cursor: str | None, page_id: UUID) -> int | None:
        if cursor is None:
            return None
        try:
            payload = json.loads(_decode_urlsafe_base64(cursor))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PageValidationError(
                "cursor must be an opaque version pagination token"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or payload.get("page_id") != str(page_id)
            or not isinstance(payload.get("before_version_no"), int)
            or payload["before_version_no"] < 1
        ):
            raise PageValidationError(
                "cursor must be an opaque version pagination token"
            )
        return payload["before_version_no"]

    def _encode_version_cursor(self, page_id: UUID, version_no: int) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "page_id": str(page_id),
                "before_version_no": version_no,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    def _resolve_page(
        self,
        session: Session,
        page_id: UUID | None,
        workspace_slug: str | None,
        slug: str | None,
    ) -> ContextPage:
        if page_id is not None:
            page = context_page_repo.get_page_by_id(session, page_id)
        elif workspace_slug is not None and slug is not None:
            workspace = self._require_workspace(session, workspace_slug)
            page = context_page_repo.get_page_by_slug(session, workspace.id, slug)
        else:
            raise PageValidationError("page_id or workspace_slug+slug is required")

        if page is None:
            raise PageNotFoundError("page not found")
        return page

    def _require_version(
        self,
        session: Session,
        version_id: UUID | None,
    ) -> ContextPageVersion:
        version = context_page_repo.get_version(session, version_id)
        if version is None:
            raise PageNotFoundError("page version not found")
        return version

    def _require_named_open_edit(
        self,
        session: Session,
        page: ContextPage,
        version_id: UUID,
        actor_kind: str,
        actor_id: str,
    ) -> ContextPageVersion:
        version = self._require_page_version(session, page, version_id)
        if version.version_status != "draft":
            raise PageConflictError(
                server_version_id=version.id,
                server_content_hash=version.content_hash,
                server_editing_revision=version.editing_revision,
                error_code="editing_revision_conflict",
            )
        return version

    @staticmethod
    def _own_page_version(
        session: Session,
        page: ContextPage,
        version_id: UUID,
        actor_kind: str,
        actor_id: str,
    ) -> ContextPageVersion | None:
        version = session.scalar(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=page.workspace_id,
                criteria=[
                    ContextPageVersion.page_id == page.id,
                    ContextPageVersion.id == version_id,
                ],
            )
        )
        if version is None:
            return None
        return version

    def _require_page_version(
        self,
        session: Session,
        page: ContextPage,
        version_id: UUID | None,
    ) -> ContextPageVersion:
        version = (
            session.scalar(
                select(ContextPageVersion).where(
                    ContextPageVersion.workspace_id == page.workspace_id,
                    ContextPageVersion.page_id == page.id,
                    ContextPageVersion.id == version_id,
                )
            )
            if version_id is not None
            else None
        )
        if version is None:
            raise PageNotFoundError("page version not found")
        return version

    def _validate_base(
        self,
        session: Session,
        page: ContextPage,
        current: ContextPageVersion,
        base_version_id: UUID | None,
        base_content_hash: str | None,
        expected_editing_revision: int | None = None,
    ) -> None:
        if base_version_id is None or base_content_hash is None:
            raise PageValidationError(
                "base_version_id and base_content_hash are required"
            )
        base_version = context_page_repo.get_version(session, base_version_id)
        if base_version is None or base_version.page_id != page.id:
            raise PageNotFoundError("page version not found")
        if base_version_id != current.id or base_content_hash != current.content_hash:
            raise PageConflictError(
                server_version_id=current.id,
                server_content_hash=current.content_hash,
                server_editing_revision=current.editing_revision,
            )
        # base_content_hash alone only covers the markdown body: a save that
        # changed only title/tags/aliases/metadata on this same row does not
        # move it, so a second, stale caller whose body still matches would
        # pass the check above and silently clobber the metadata change.
        # editing_revision is bumped on every save to this row regardless of
        # what changed (see ContextPageVersion.editing_revision's docstring).
        if (
            expected_editing_revision is not None
            and expected_editing_revision != current.editing_revision
        ):
            raise PageConflictError(
                server_version_id=current.id,
                server_content_hash=current.content_hash,
                server_editing_revision=current.editing_revision,
                error_code="editing_revision_conflict",
            )

    def _summary_from_page(
        self,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
        *,
        modified_at=None,
        can_read_drafts: bool = True,
        actor: ActorContext | None = None,
    ) -> PageSummary:
        title, page_type, tags, aliases = effective_metadata_for_payload(
            session,
            page,
            version,
        )
        projection = project_context_version(
            page,
            version,
            visible_version_id=version.id,
            expose_draft_state=can_read_drafts,
        )
        published_version = (
            context_page_repo.get_version(session, page.current_published_version_id)
            if page.current_published_version_id is not None
            else None
        )
        return PageSummary(
            id=page.id,
            slug=page.slug,
            title=title,
            page_type=page_type,
            tags=tags,
            aliases=aliases,
            visible_version_id=version.id,
            current_version_id=_editable_head(page, can_read_drafts),
            current_published_version_id=page.current_published_version_id,
            published_version_no=(
                published_version.version_no if published_version is not None else None
            ),
            version_status=version.version_status,
            history_status=projection.history_status,
            inconsistency_reasons=list(projection.inconsistency_reasons),
            graph_status=projection.graph_status,
            is_visible_head=projection.is_visible_head,
            is_published_head=projection.is_published_head,
            is_draft_head=projection.is_draft_head,
            is_alternative=projection.is_alternative,
            is_discarded=projection.is_discarded,
            updated_at=modified_at
            or _published_or_visible_instant(session, page, version),
            content_hash=version.content_hash,
            is_public=page.is_public,
        )

    def _inspect_saved_body(
        self,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
        *,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
    ) -> PreparedBody:
        return self.body_preparer.inspect(
            version.body_md,
            WriteContext(
                actor=self.actor_context,
                workspace=self._workspace_for(session, page.workspace_id),
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                page=page,
                version=version,
                session=session,
            ),
        )

    def _payload_from_page(
        self,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
        alternative_draft_id: UUID | None = None,
        change_set_id: UUID | None = None,
        review_item_id: UUID | None = None,
        preserved_as_alternative: bool = False,
        visible_version_id: UUID | None = None,
        superseded_review_count: int = 0,
        can_read_drafts: bool = True,
        parent_visibility_actor: ActorContext | None = None,
        current_version_id_override: UUID | None | object = _UNSET,
        prepared: PreparedBody | None = None,
        block_map_stale: bool | None = None,
    ) -> PagePayload:
        from backend.services.document_references import (
            deleted_document_references,
            document_references_hash,
            read_document_references,
        )

        title, page_type, tags, aliases = effective_metadata_for_payload(
            session,
            page,
            version,
        )
        projected_visible_id = visible_version_id or version.id
        projection = project_context_version(
            page,
            version,
            visible_version_id=projected_visible_id,
            expose_draft_state=can_read_drafts,
        )
        published_version = (
            context_page_repo.get_version(session, page.current_published_version_id)
            if page.current_published_version_id is not None
            else None
        )
        parent_version_id = version.parent_version_id
        # Project the parent through the reader's visibility: a reader without
        # draft access sees only published parents.
        if not can_read_drafts or parent_visibility_actor is not None:
            parent = (
                context_page_repo.get_version(session, version.parent_version_id)
                if version.parent_version_id is not None
                else None
            )
            parent_version_id = context_parent_projection(
                version,
                parent,
                parent_visibility_actor,
            )[1]
        deleted_references = deleted_document_references(session, version)
        from backend.services.context_draft_workflow import parents_for

        visible_parents = []
        for key in parents_for(session, version):
            candidate_parent = context_page_repo.get_version(session, key)
            if candidate_parent is not None and (
                (
                    can_read_drafts
                    or candidate_parent.version_status
                    in PUBLISHED_CONTEXT_VERSION_STATUSES
                )
                if parent_visibility_actor is None
                else context_version_is_visible(
                    candidate_parent, parent_visibility_actor
                )
            ):
                visible_parents.append(key)
        payload = PagePayload(
            id=page.id,
            workspace_id=page.workspace_id,
            slug=page.slug,
            title=title,
            page_type=page_type,
            tags=tags,
            aliases=aliases,
            visible_version_id=projected_visible_id,
            current_version_id=(
                _editable_head(page, can_read_drafts)
                if current_version_id_override is _UNSET
                else current_version_id_override
            ),
            current_published_version_id=page.current_published_version_id,
            published_version_no=(
                published_version.version_no if published_version is not None else None
            ),
            version_status=version.version_status,
            history_status=projection.history_status,
            inconsistency_reasons=list(projection.inconsistency_reasons),
            graph_status=projection.graph_status,
            is_visible_head=projection.is_visible_head,
            is_published_head=projection.is_published_head,
            is_draft_head=projection.is_draft_head,
            is_alternative=projection.is_alternative,
            is_discarded=projection.is_discarded,
            updated_at=_published_or_visible_instant(session, page, version),
            content_hash=version.content_hash,
            is_public=page.is_public,
            body_md=version.body_md,
            block_map_stale=(
                _block_map_needs_save(version)
                if block_map_stale is None
                else block_map_stale
            ),
            references=read_document_references(
                session,
                version=version,
                actor=parent_visibility_actor or self.actor_context,
                self_origins=self.body_preparer.self_origins,
                _deleted=deleted_references,
            ),
            references_hash=document_references_hash(
                session, version, _deleted=deleted_references
            ),
            version_id=version.id,
            editing_revision=version.editing_revision,
            parent_version_id=parent_version_id,
            parent_version_ids=visible_parents,
            page_metadata=dict(version.page_metadata or {}),
            created_at=version.created_at,
            alternative_draft_id=alternative_draft_id,
            change_set_id=change_set_id,
            review_item_id=review_item_id,
            preserved_as_alternative=preserved_as_alternative,
            superseded_review_count=superseded_review_count,
            draft_saved_at=(
                page.updated_at if version.version_status == "draft" else None
            ),
            publish_blocked_reasons=_publish_blocked_reasons(
                session, page, version, registry=self.lint_registry
            ),
            lint_report=(
                prepared.lint_report
                if prepared is not None
                else {"fixes": [], "findings": []}
            ),
        )
        payload._can_submit_for_review = self._can_submit_for_review(
            session, page, version, parent_visibility_actor or self.actor_context
        )
        if prepared is not None:
            payload._metadata_overrides = list(prepared.metadata_overrides)
        return payload


def _block_map_needs_save(version: ContextPageVersion) -> bool:
    # A SQL FULL row can report its missing map without opening Git. Historical
    # rows read the map archived with their body; neither path repairs it.
    stored = version._block_map if version._body_md is not None else version.block_map
    return block_map_is_stale(stored, version.body_md)


def _publish_blocked_reasons(
    session: Session,
    page: ContextPage,
    version: ContextPageVersion,
    *,
    registry: LintRegistry | None = None,
) -> list[DraftReviewReason]:
    """Why this version is not published, from body + workflow evidence."""
    if version.version_status != "draft":
        return []
    reasons = [
        DraftReviewReason(
            code=finding.rule_id,
            message=body_relative_message(
                finding.suggested_action or finding.rule_id,
                version.body_md,
            ),
        )
        for finding in publish_floor_findings(version.body_md, registry=registry)
    ]
    diff_ref = f"context_page_version:{version.id}"
    evidence_rows = session.execute(
        select(
            ChangeSet.id,
            ChangeSet.trigger_type,
            ChangeSet.reason,
            ChangeSet.created_at,
            ReviewItem.status,
            ReviewItem.review_type,
        )
        .outerjoin(
            ChangeOperation,
            ChangeOperation.change_set_id == ChangeSet.id,
        )
        .outerjoin(ReviewItem, ReviewItem.change_set_id == ChangeSet.id)
        .where(
            or_(
                ChangeSet.id == version.change_set_id,
                ChangeOperation.diff_ref == diff_ref,
            )
        )
        .order_by(ChangeSet.created_at.desc(), ChangeSet.id.desc())
    ).all()

    # One version may be referenced by more than one ChangeSet. Keep the newest
    # origin first and retain live review state from every referencing set.
    seen_change_sets: set[UUID] = set()
    origins: list[tuple[str, str | None]] = []
    live_review_statuses: list[str] = []
    for (
        change_set_id,
        trigger_type,
        reason,
        _created_at,
        review_status,
        review_type,
    ) in evidence_rows:
        if change_set_id not in seen_change_sets:
            seen_change_sets.add(change_set_id)
            origins.append((trigger_type, reason))
        if (
            review_type == "context_update"
            and review_status is not None
            and review_status != "superseded"
            and review_status not in live_review_statuses
        ):
            live_review_statuses.append(review_status)

    origin_reason = _draft_origin_reason(version, origins)
    if origin_reason is not None:
        reasons.append(origin_reason)
    review_reason = _draft_review_reason(live_review_statuses)
    if review_reason is not None:
        reasons.append(review_reason)

    if reasons:
        return reasons
    if version.id == page.latest_draft_version_id:
        if page.current_published_version_id is None:
            return [
                DraftReviewReason(
                    code="draft.never_published",
                    message="This page has no published version yet.",
                )
            ]
        return [
            DraftReviewReason(
                code="draft.unpublished_changes",
                message="This draft contains changes newer than the published version.",
            )
        ]
    return [
        DraftReviewReason(
            code="draft.unpublished",
            message="This version is an unpublished draft.",
        )
    ]


def _draft_origin_reason(
    version: ContextPageVersion,
    origins: list[tuple[str, str | None]],
) -> DraftReviewReason | None:
    if version.graph_status == "branched":
        return DraftReviewReason(
            code="draft.alternative",
            message="This version is a preserved alternative draft.",
        )
    for trigger_type, reason in origins:
        pair = (trigger_type, reason)
        if pair == ("manual_edit", "page.autosave.draft"):
            return DraftReviewReason(
                code="draft.autosave",
                message="This version is an unpublished browser autosave draft.",
            )
        if pair == ("manual_edit", "page.create"):
            return DraftReviewReason(
                code="draft.page_create",
                message="This newly created page has not been published yet.",
            )
        if pair == ("manual_edit", "page.update"):
            return DraftReviewReason(
                code="draft.page_update",
                message="This page update is staged as an unpublished draft.",
            )
        if reason == "page.restore_version" and trigger_type in {
            "manual_edit",
            "agent_authenticated_write",
        }:
            return DraftReviewReason(
                code="draft.restore",
                message="This restored version is staged as an unpublished draft.",
            )
        if pair == ("manual_stale_recovery", "page.preserve_alternative"):
            return DraftReviewReason(
                code="draft.alternative",
                message="This version is a preserved alternative draft.",
            )
        if trigger_type == "mcp_write_draft":
            return DraftReviewReason(
                code="draft.mcp_write",
                message="This MCP-created version is an unpublished draft.",
            )
        if trigger_type == "mcp_update_draft":
            return DraftReviewReason(
                code="draft.mcp_update",
                message="This MCP-updated version is an unpublished draft.",
            )
        if trigger_type == "context_ingress_accept":
            return DraftReviewReason(
                code="draft.ingress_accept",
                message="This accepted ingress proposal is staged as a draft.",
            )
        if trigger_type == "agent_authenticated_write" and reason in {
            "page.autosave.draft",
            "page.create",
            "page.update",
        }:
            return DraftReviewReason(
                code="draft.ai_staged",
                message="This authenticated agent write is staged as a draft.",
            )
    return None


def _draft_review_reason(statuses: list[str]) -> DraftReviewReason | None:
    for status, code, message in (
        (
            "changes_requested",
            "draft.review_changes_requested",
            "A reviewer requested changes to this draft.",
        ),
        ("rejected", "draft.review_rejected", "A reviewer rejected this draft."),
        ("pending", "draft.review_pending", "This draft is awaiting human review."),
    ):
        if status in statuses:
            return DraftReviewReason(code=code, message=message)
    return None


def _editable_head(page: ContextPage, can_read_drafts: bool) -> UUID | None:
    """The edit head a reader may see: drafts only for draft readers."""

    if not can_read_drafts:
        return page.current_published_version_id
    return page.current_version_id


def _page_version_projection_fields(
    page: ContextPage,
    version: ContextPageVersion,
    *,
    visible_version_id: UUID | None,
    expose_draft_state: bool,
) -> dict[str, object]:
    projection = project_context_version(
        page,
        version,
        visible_version_id=visible_version_id,
        expose_draft_state=expose_draft_state,
    )
    return {
        "history_status": projection.history_status,
        "graph_status": projection.graph_status,
        "is_visible_head": projection.is_visible_head,
        "is_published_head": projection.is_published_head,
        "is_draft_head": projection.is_draft_head,
        "is_alternative": projection.is_alternative,
        "is_discarded": projection.is_discarded,
    }


def _published_or_visible_instant(
    session: Session,
    page: ContextPage,
    visible_version: ContextPageVersion,
):
    if page.current_published_version_id is None:
        return visible_version.created_at
    if page.current_published_version_id == visible_version.id:
        return visible_version.created_at
    published = session.get(ContextPageVersion, page.current_published_version_id)
    return published.created_at if published is not None else visible_version.created_at


def metadata_for_payload(
    page: ContextPage,
    version: ContextPageVersion,
) -> tuple[str, list[str], list[str]]:
    parsed = parse_page_frontmatter(version.body_md)
    page_type = parsed.page_type or page.page_type
    if page_type not in PAGE_TYPES:
        page_type = "overview"
    tags = parsed.tags if parsed.tags is not None else list(page.tags or [])
    aliases = parsed.aliases if parsed.aliases is not None else list(page.aliases or [])
    return page_type, normalize_page_tags(tags), normalize_page_aliases(aliases)


def effective_metadata_for_payload(
    session: Session, page: ContextPage, version: ContextPageVersion
) -> tuple[str, str, list[str], list[str]]:
    from backend.services.context_draft_workflow import metadata_for

    metadata = metadata_for(page, version)
    return (
        metadata.title,
        metadata.page_type,
        list(metadata.tags),
        list(metadata.aliases),
    )


def _page_version_metadata_changes(
    session: Session,
    *,
    page: ContextPage,
    from_version: ContextPageVersion | None,
    to_version: ContextPageVersion,
) -> list[str]:
    patches = page_metadata_patches_for_versions(
        session,
        page=page,
        version_ids=tuple(
            version.id for version in (from_version, to_version) if version is not None
        ),
    )

    def snapshot(version: ContextPageVersion) -> dict[str, object]:
        parsed = parse_page_frontmatter(version.body_md)
        patch = patches.get(version.id, {})
        title = parsed.title or title_from_markdown_heading(parsed.body) or page.title
        page_type = parsed.page_type or page.page_type
        tags = parsed.tags if parsed.tags is not None else list(page.tags or [])
        aliases = (
            parsed.aliases if parsed.aliases is not None else list(page.aliases or [])
        )
        if isinstance(patch.get("title"), str):
            title = patch["title"]
        if isinstance(patch.get("page_type"), str):
            page_type = patch["page_type"]
        if isinstance(patch.get("tags"), list):
            tags = [str(tag) for tag in patch["tags"]]
        if isinstance(patch.get("aliases"), list):
            aliases = [str(alias) for alias in patch["aliases"]]
        placement = patch.get("placement_parent_node_id")
        if placement is None and version.id in {
            page.current_version_id,
            page.current_published_version_id,
            page.latest_draft_version_id,
        }:
            placement = (
                str(page.placement_parent_node_id)
                if page.placement_parent_node_id is not None
                else None
            )
        return {
            "title": title,
            "tags": tuple(normalize_page_tags(tags)),
            "aliases": tuple(normalize_page_aliases(aliases)),
            "page_type": page_type,
            "placement": placement,
            "references": document_references_hash(session, version),
        }

    from backend.services.document_references import document_references_hash

    before = (
        snapshot(from_version)
        if from_version is not None
        else {
            "title": None,
            "tags": (),
            "aliases": (),
            "page_type": None,
            "placement": None,
            "references": document_references_hash(session, None),
        }
    )
    after = snapshot(to_version)
    return [
        key
        for key in ("title", "tags", "aliases", "page_type", "placement", "references")
        if before[key] != after[key]
    ]


def sync_page_projections(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion | None = None,
    record_graph_observations: bool = True,
    sync_graph_edges: bool = True,
    self_origins: frozenset[str] | None = None,
) -> None:
    """Synchronize write-through projections for a Context page.

    PageLink rows are version-scoped so draft and published link graphs can
    coexist. page_name_index is a rebuildable projection of the currently live
    published/draft heads. Full rebuilders can defer graph edges until every
    page's source facts are synchronized, then reconcile them once together.
    """

    _sync_page_name_index(session, page=page)
    if version is not None:
        # Re-derive so ``dtxwiki reindex`` doubles as the NULL backfill path
        # for legacy rows (nav_description is rebuildable by contract).
        version.nav_description = derive_nav_description(version.body_md)
        sync_page_link_projection(
            session, page=page, version=version, self_origins=self_origins
        )
    else:
        _delete_stale_page_link_versions(session, page=page)
    _sync_page_doc_source(session, page=page)

    # G1 write-through: keep the page's curated edge projection (wikilink/
    # citation, published-head grain) fresh with the link projection — the
    # neighbor block must not wait for a reindex.
    from backend.services.indexing.edge_registry import (
        refresh_page_edge_projection,
    )

    if sync_graph_edges and (
        version is None
        or page.current_published_version_id is None
        or version.id == page.current_published_version_id
    ):
        refresh_page_edge_projection(
            session,
            page.workspace_id,
            page.id,
            record_observations=record_graph_observations,
        )
        from backend.services.indexing.edge_registry import (
            refresh_document_citation_sources,
        )

        refresh_document_citation_sources(
            session, page.workspace_id, "context_page", page.id
        )


def refresh_page_links(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
    self_origins: frozenset[str] | None = None,
) -> None:
    """Compatibility wrapper for older call sites."""

    sync_page_projections(
        session, page=page, version=version, self_origins=self_origins
    )


def sync_page_link_projection(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
    observed_at: datetime | None = None,
    self_origins: frozenset[str] | None = None,
) -> PageLinkProjectionDelta:
    """Reconcile one version's authored links without changing graph revisions."""

    delta = _sync_page_links_for_version(
        session,
        page=page,
        version=version,
        observed_at=observed_at,
        self_origins=self_origins,
    )
    return delta.plus(deleted=prune_stale_page_link_projections(session, page=page))


def prune_stale_page_link_projections(
    session: Session,
    *,
    page: ContextPage,
) -> int:
    """Remove PageLink rows outside the page's current live-version set."""

    return _delete_stale_page_link_versions(session, page=page)


def _sync_page_links_for_version(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
    self_origins: frozenset[str] | None = None,
    observed_at: datetime | None = None,
) -> PageLinkProjectionDelta:
    from backend.services.context_links import (
        LinkOccurrence,
        classify,
        configured_self_origins,
    )

    origins = configured_self_origins() if self_origins is None else self_origins
    preserved_pins: dict[str, UUID] = {}
    pin_rows = session.execute(
        join_in_workspace(
            select_in_workspace(
                PageLink,
                workspace_id=page.workspace_id,
                conditions=(
                    PageLink.source_page_id == page.id,
                    PageLink.target_page_id.is_not(None),
                ),
            ),
            PageLink,
            ContextPageVersion,
            workspace_id=page.workspace_id,
            on=ContextPageVersion.id == PageLink.source_version_id,
        )
        .with_only_columns(
            PageLink.locator_key,
            PageLink.target_page_id,
            ContextPageVersion.version_no,
        )
        .order_by(
            case(
                (ContextPageVersion.id == version.id, 0),
                (ContextPageVersion.id == version.parent_version_id, 1),
                else_=2,
            ),
            ContextPageVersion.version_no.desc(),
        )
    ).all()
    for locator_key, target_page_id, _version_no in pin_rows:
        preserved_pins.setdefault(locator_key, target_page_id)
    # Graph relations follow the page renderer's explicit Markdown and
    # decoded wikilink tokens. Locator classification and durable pins still
    # share the workspace link contract used by save-time lint and readers.
    from backend.context_media.domain.markdown_media import (
        SourceSpan,
        _rendered_page_body,
    )

    body, _ = _rendered_page_body(version.body_md or "")
    markdown_analysis = RenderedMarkdownAnalysis(body)
    occurrences = []
    for link in rendered_markdown_links(body, analysis=markdown_analysis):
        occurrences.append(
            LinkOccurrence(
                href=link.destination,
                kind="markdown",
                span=SourceSpan(
                    link.source_start,
                    link.source_end,
                    len(body[: link.source_start].encode("utf-8")),
                    len(body[: link.source_end].encode("utf-8")),
                ),
                snippet=snippet_around_span(body, link.source_start, link.source_end),
            )
        )
    for link in rendered_wikilinks(body, analysis=markdown_analysis):
        occurrences.append(
            LinkOccurrence(
                href=f"[[{link.target}]]",
                kind="wikilink",
                span=SourceSpan(
                    link.source_start,
                    link.source_end,
                    len(body[: link.source_start].encode("utf-8")),
                    len(body[: link.source_end].encode("utf-8")),
                ),
                snippet=snippet_around_span(body, link.source_start, link.source_end),
            )
        )
    occurrences.sort(key=lambda occurrence: occurrence.span.char_start)
    locators = [classify(item.href, origins) for item in occurrences]
    eligible = [
        (occurrence, locator)
        for occurrence, locator in zip(occurrences, locators, strict=True)
        if locator.kind
        not in {"external", "public_reader", "local", "media", "unknown"}
        and locator.workspace_id in {None, page.workspace_id}
    ]
    # This writer projection records same-workspace identity without requiring
    # a reader-visible version. Read paths reauthorize each pinned target.
    locator_slugs = {locator.slug for _, locator in eligible if locator.slug}
    node_ids = {
        locator.node_id for _, locator in eligible if locator.node_id is not None
    }
    locator_page_ids = {
        locator.page_id for _, locator in eligible if locator.page_id is not None
    } | node_ids

    def chunks(values: list, size: int = 400):
        for start in range(0, len(values), size):
            yield values[start : start + size]

    existing_by_slug: dict[str, UUID] = {}
    for slug_chunk in chunks(list(locator_slugs)):
        existing_by_slug.update(
            session.execute(
                select_in_workspace(
                    ContextPage,
                    workspace_id=page.workspace_id,
                    conditions=(ContextPage.slug.in_(slug_chunk),),
                ).with_only_columns(ContextPage.slug, ContextPage.id)
            ).all()
        )
    node_to_page_id: dict[UUID, UUID] = {}
    if node_ids:
        from backend.models.tree import WorkspaceTreeNode

        for node_chunk in chunks(list(node_ids)):
            node_to_page_id.update(
                session.execute(
                    select_in_workspace(
                        WorkspaceTreeNode,
                        workspace_id=page.workspace_id,
                        conditions=(
                            WorkspaceTreeNode.id.in_(node_chunk),
                            WorkspaceTreeNode.target_kind == "context_page",
                        ),
                    ).with_only_columns(
                        WorkspaceTreeNode.id, WorkspaceTreeNode.target_id
                    )
                ).all()
            )
    candidate_page_ids = locator_page_ids | {
        page_id for page_id in node_to_page_id.values() if page_id is not None
    }
    existing_page_ids: set[UUID] = set()
    for page_id_chunk in chunks(list(candidate_page_ids)):
        existing_page_ids.update(
            session.scalars(
                select_in_workspace(
                    ContextPage,
                    workspace_id=page.workspace_id,
                    conditions=(ContextPage.id.in_(page_id_chunk),),
                ).with_only_columns(ContextPage.id)
            )
        )

    grouped: dict[str, dict] = {}
    for occurrence, locator in eligible:
        key = locator.key
        if locator.node_id is not None:
            page_key = f"page_id:{locator.node_id}"
            if page_key in preserved_pins or locator.node_id in existing_page_ids:
                key = page_key
        pin = preserved_pins.get(key)
        if pin is None and locator.page_id in existing_page_ids:
            pin = locator.page_id
        if pin is None and locator.node_id is not None:
            candidate_id = (
                locator.node_id
                if locator.node_id in existing_page_ids
                else node_to_page_id.get(locator.node_id)
            )
            if candidate_id in existing_page_ids:
                pin = candidate_id
        if pin is None and locator.slug is not None:
            pin = existing_by_slug.get(locator.slug)
        if key not in grouped:
            grouped[key] = {
                "link_syntax": "wikilink"
                if locator.kind == "wikilink"
                else "context_url",
                "target_slug": locator.slug,
                "target_page_id": pin,
                "occurrence_count": 0,
                "first_snippet": occurrence.snippet,
            }
        grouped[key]["occurrence_count"] += 1
    existing_rows = list(
        session.scalars(
            select_in_workspace(
                PageLink,
                workspace_id=page.workspace_id,
                conditions=(
                    PageLink.source_page_id == page.id,
                    PageLink.source_version_id == version.id,
                ),
            )
            .order_by(PageLink.id.asc())
            .with_for_update()
        )
    )
    existing_by_key = {row.locator_key: row for row in existing_rows}
    target_ids = {
        values["target_page_id"]
        for values in grouped.values()
        if values["target_page_id"] is not None
    }
    target_created_at_by_id = {}
    for target_chunk in chunks(list(target_ids)):
        target_created_at_by_id.update(
            session.execute(
                select_in_workspace(
                    ContextPage,
                    workspace_id=page.workspace_id,
                    conditions=(ContextPage.id.in_(target_chunk),),
                ).with_only_columns(ContextPage.id, ContextPage.created_at)
            ).all()
        )
    stale_ids = [row.id for row in existing_rows if row.locator_key not in grouped]
    if stale_ids:
        session.execute(
            delete_in_workspace(
                PageLink,
                workspace_id=page.workspace_id,
                conditions=(PageLink.id.in_(stale_ids),),
            )
        )
        session.flush()
    inserted = updated = 0
    for key, values in grouped.items():
        existing = existing_by_key.get(key)
        if existing is None:
            session.add(
                PageLink(
                    workspace_id=page.workspace_id,
                    source_page_id=page.id,
                    source_version_id=version.id,
                    locator_key=key,
                    created_at=_page_link_observed_at(
                        observed_at=observed_at,
                        target_page_id=values["target_page_id"],
                        target_created_at_by_id=target_created_at_by_id,
                    ),
                    **values,
                )
            )
            inserted += 1
            continue
        if any(getattr(existing, name) != value for name, value in values.items()):
            updated += 1
        if existing.target_page_id != values["target_page_id"]:
            existing.created_at = _page_link_observed_at(
                observed_at=observed_at,
                target_page_id=values["target_page_id"],
                target_created_at_by_id=target_created_at_by_id,
            )
        for name, value in values.items():
            setattr(existing, name, value)
    return PageLinkProjectionDelta(
        inserted=inserted,
        updated=updated,
        deleted=len(stale_ids),
    )


def _page_link_observed_at(
    *,
    observed_at: datetime | None,
    target_page_id: object,
    target_created_at_by_id: dict[UUID, datetime],
) -> datetime:
    if observed_at is None:
        return utc_now()
    if isinstance(target_page_id, UUID):
        target_created_at = target_created_at_by_id.get(target_page_id)
        if target_created_at is not None:
            return max(observed_at, target_created_at)
    return observed_at


def _delete_stale_page_link_versions(session: Session, *, page: ContextPage) -> int:
    live_version_ids = context_page_repo.live_page_link_version_ids(session, page)
    statement = delete_in_workspace(
        PageLink,
        workspace_id=page.workspace_id,
        conditions=(PageLink.source_page_id == page.id,),
    )
    if live_version_ids:
        statement = statement.where(PageLink.source_version_id.notin_(live_version_ids))
    result = session.execute(statement)
    return max(int(result.rowcount or 0), 0)


def _sync_page_name_index(session: Session, *, page: ContextPage) -> None:
    session.execute(delete(PageNameIndex).where(PageNameIndex.page_id == page.id))
    index_rows: list[PageNameIndex] = []
    if page.lifecycle_status == "archived":
        # B-8: archived pages resolve nowhere — entity lane and read_concept
        # must stop finding them the moment the archive cascades.
        _sync_page_name_trigram_rows(session, page=page, rows=index_rows)
        return
    for version_id, scope in _name_index_live_versions(session, page):
        version = session.get(ContextPageVersion, version_id)
        if version is None:
            continue
        metadata_patch = dict(version.page_metadata or {})
        names = _page_names_for_index(page, metadata_patch=metadata_patch)
        for name, kind in names:
            name_norm = normalize_entity_name(name)
            if not name_norm:
                continue
            row = PageNameIndex(
                workspace_id=page.workspace_id,
                page_id=page.id,
                source_version_id=version.id,
                visibility_scope=scope,
                name_norm=name_norm,
                name=name.strip(),
                name_kind=kind,
            )
            session.add(row)
            index_rows.append(row)
    _sync_page_name_trigram_rows(session, page=page, rows=index_rows)


def _sync_page_name_trigram_rows(
    session: Session,
    *,
    page: ContextPage,
    rows: list[PageNameIndex],
) -> None:
    from backend.services.indexing.fts5 import sync_page_name_trigrams

    sync_page_name_trigrams(
        session,
        workspace_id=page.workspace_id,
        page_id=page.id,
        rows=rows,
    )


def _sync_page_doc_source(session: Session, *, page: ContextPage) -> None:
    from backend.services.indexing.fts5 import sync_context_page_doc_sources

    sync_context_page_doc_sources(session, page)


def _name_index_live_versions(
    session: Session, page: ContextPage
) -> list[tuple[UUID, str]]:
    versions = (
        [(page.current_published_version_id, "published")]
        if page.current_published_version_id
        else []
    )
    drafts = session.scalars(
        select_projection_in_workspace(
            ContextPageVersion,
            workspace_id=page.workspace_id,
            columns=(ContextPageVersion.id,),
            predicates=(
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.version_status == "draft",
            ),
        )
    ).all()
    return versions + [(key, "draft") for key in drafts]


def _page_names_for_index(
    page: ContextPage,
    *,
    metadata_patch: dict[str, object] | None = None,
) -> list[tuple[str, str]]:
    # Canonical name sources only: ContextPage.title and ContextPage.aliases
    # are the authored records (§5.3), and tree renames / metadata-only edits
    # mutate them without touching any version body. Deriving names from
    # version frontmatter let a stale embedded title override a rename.
    candidates: list[tuple[str, str]] = [
        (page.slug, "slug"),
        ((metadata_patch or {}).get("title", page.title), "title"),
        *[
            (alias, "alias")
            for alias in (
                (metadata_patch or {}).get("aliases", page.aliases or []) or []
            )
        ],
    ]
    if metadata_patch:
        patched_title = metadata_patch.get("title")
        if isinstance(patched_title, str):
            candidates.append((patched_title, "title"))
        patched_aliases = metadata_patch.get("aliases")
        if isinstance(patched_aliases, list):
            candidates.extend(
                (str(alias), "alias") for alias in patched_aliases if str(alias).strip()
            )
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple[str, str]] = []
    for name, kind in candidates:
        normalized = normalize_entity_name(name)
        key = (normalized, kind)
        if not normalized or key in seen:
            continue
        seen.add(key)
        deduped.append((name, kind))
    return deduped


def page_metadata_delta(
    page: ContextPage,
    *,
    title: str | None = None,
    page_type: str | None = None,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    baseline_patch: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return values that differ from the effective metadata for a version."""

    patch: dict[str, object] = {}
    effective_title = (
        baseline_patch.get("title", page.title) if baseline_patch else page.title
    )
    if title is not None and title != effective_title:
        patch["title"] = title
    effective_page_type = (
        baseline_patch.get("page_type", page.page_type)
        if baseline_patch
        else page.page_type
    )
    if page_type is not None and page_type != effective_page_type:
        patch["page_type"] = page_type
    if tags is not None:
        normalized_tags = normalize_page_tags(tags)
        effective_tags = normalize_page_tags(
            baseline_patch.get("tags", list(page.tags or []))
            if baseline_patch
            else list(page.tags or [])
        )
        if normalized_tags != effective_tags:
            patch["tags"] = normalized_tags
    if aliases is not None:
        normalized_aliases = normalize_page_aliases(aliases)
        effective_aliases = normalize_page_aliases(
            baseline_patch.get("aliases", list(page.aliases or []))
            if baseline_patch
            else list(page.aliases or [])
        )
        if normalized_aliases != effective_aliases:
            patch["aliases"] = normalized_aliases
    return patch


def stage_page_metadata_patch(
    session: Session,
    *,
    change_set_id: UUID,
    page: ContextPage,
    version: ContextPageVersion,
    patch: dict[str, object] | None,
) -> None:
    if patch:
        from backend.services.context_draft_workflow import metadata_dict, metadata_for

        metadata = metadata_dict(metadata_for(page, version))
        metadata.update(patch)
        version.page_metadata = metadata


def pending_page_metadata_patch(
    session: Session,
    *,
    page: ContextPage,
    version_id: UUID,
) -> dict[str, object]:
    """Merge metadata patches bound to one draft in deterministic order."""

    return page_metadata_patches_for_versions(
        session,
        page=page,
        version_ids=(version_id,),
    ).get(version_id, {})


def page_metadata_patches_for_versions(
    session: Session,
    *,
    page: ContextPage,
    version_ids: Iterable[UUID],
) -> dict[UUID, dict[str, object]]:
    """Load metadata ledgers for multiple versions with one ordered query."""

    version_ids = tuple(dict.fromkeys(version_ids))
    if not version_ids:
        return {}
    diff_refs = {
        f"context_page_version:{version_id}": version_id for version_id in version_ids
    }
    operations = session.scalars(
        select(ChangeOperation)
        .where(
            ChangeOperation.target_type == "context_page",
            ChangeOperation.target_id == page.id,
            ChangeOperation.operation == "metadata_patch",
            ChangeOperation.diff_ref.in_(diff_refs),
        )
        .order_by(ChangeOperation.created_at.asc(), ChangeOperation.id.asc())
    ).all()
    merged: dict[UUID, dict[str, object]] = {
        version_id: {} for version_id in version_ids
    }
    for operation in operations:
        version_id = diff_refs.get(operation.diff_ref or "")
        if version_id is None:
            continue
        try:
            value = json.loads(operation.diff_summary or "")
        except ValueError:
            continue
        if isinstance(value, dict):
            merged[version_id].update(value)
    return merged


def should_stage_page_metadata(
    page: ContextPage,
    version: ContextPageVersion,
) -> bool:
    # `page.title`/`tags`/`aliases` are page-level columns shared by every
    # version. A draft's metadata stays in the draft until it is published,
    # so readers of the published version never see it early.
    return version.version_status == "draft"


def slugify_wikilink_target(value: str | None) -> str:
    from backend.services.context_links import normalize_wikilink_target

    return normalize_wikilink_target(value)


def apply_page_metadata(
    page: ContextPage,
    *,
    page_type: str | None,
    tags: list[str] | None,
    aliases: list[str] | None,
) -> bool:
    changed = False
    if page_type is not None and page.page_type != page_type:
        page.page_type = page_type
        changed = True
    if tags is not None:
        next_tags = normalize_page_tags(tags)
        if list(page.tags or []) != next_tags:
            page.tags = next_tags
            changed = True
    if aliases is not None:
        next_aliases = normalize_page_aliases(aliases)
        if list(page.aliases or []) != next_aliases:
            page.aliases = next_aliases
            changed = True
    return changed


def snippet_around_span(
    body_md: str,
    source_start: int,
    source_end: int,
    *,
    radius: int = 90,
) -> str:
    source_start = max(0, min(source_start, len(body_md)))
    source_end = max(source_start, min(source_end, len(body_md)))
    content_limit = max(1, radius * 2)
    matched_length = source_end - source_start
    if matched_length >= content_limit:
        start = source_start
        end = min(len(body_md), start + content_limit)
    else:
        remaining = content_limit - matched_length
        before = min(source_start, remaining // 2)
        after = min(len(body_md) - source_end, remaining - before)
        before = min(source_start, remaining - after)
        start = source_start - before
        end = source_end + after
    snippet = re.sub(r"\s+", " ", body_md[start:end]).strip()
    if start > 0:
        snippet = f"...{snippet}"
    if end < len(body_md):
        snippet = f"{snippet}..."
    return snippet


def select_inline_comment_rows(page_id: UUID, *, limit: int):
    return (
        select(ChangeOperation, ReviewItem)
        .join(ReviewItem, ReviewItem.change_set_id == ChangeOperation.change_set_id)
        .where(
            ChangeOperation.target_type == "context_page",
            ChangeOperation.target_id == page_id,
            ChangeOperation.operation == "inline_comment",
        )
        .order_by(ReviewItem.created_at.desc(), ReviewItem.id.desc())
        .limit(limit)
    )


def parse_inline_comment_anchor(diff_ref: str | None) -> PageInlineCommentAnchor | None:
    if not diff_ref:
        return None
    try:
        payload = json.loads(diff_ref)
        if payload.get("kind") != "page_inline_comment_anchor":
            return None
        anchor = payload.get("anchor")
        if not isinstance(anchor, dict):
            return None
        return PageInlineCommentAnchor.model_validate(anchor)
    except (TypeError, ValueError):
        return None


def inline_comment_payload(
    *,
    page_id: UUID,
    operation: ChangeOperation,
    review: ReviewItem,
    anchor: PageInlineCommentAnchor,
    resolved: ResolvedAnchor,
) -> PageInlineComment:
    return PageInlineComment(
        id=review.id,
        page_id=page_id,
        review_item_id=review.id,
        change_set_id=review.change_set_id,
        change_operation_id=operation.id,
        status=review.status,
        risk_level=review.risk_level,
        note=review.decision_summary or "",
        anchor=anchor,
        resolved={
            "char_offset": resolved.char_offset,
            "end_offset": resolved.end_offset,
            "status": resolved.status,
            "resolved_text": resolved.resolved_text,
        },
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


def _decode_urlsafe_base64(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}").decode("utf-8")
