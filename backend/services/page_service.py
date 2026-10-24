from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import json
import re
import unicodedata
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, aliased

from backend.context_media.application import SyncVersionUsage
from backend.context_media.domain import (
    ContextMediaError,
    MarkdownMediaPolicyError,
    validate_markdown_media_policy,
)
from backend.context_media.ports import ContextMediaRepository
from backend.domain.input_policy import validate_text
from backend.domain.prepared_body import PreparedBody, PreparedMetadata
from backend.models.base import utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import (
    ContextPage,
    ContextPageVersion,
    PageLink,
    PageNameIndex,
)
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.policy.trust_boundaries import actor_can_read_drafts
from backend.repositories import context_page_repo, workspace_repo
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.common import DraftReviewReason
from backend.schemas.context_edit import EditOperation
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
    apply_context_publication,
    project_context_version,
)
from backend.services.context_media_legacy_cutover import (
    LegacyMediaCutoverConflictError,
    require_no_visible_legacy_media,
)
from backend.services.context_push import target_ids_for_operations
from backend.services.context_review_disposition import (
    context_version_review_metadata,
)
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
from backend.services.public_page_exposure import (
    clear_public_page_exposure,
    exposure_scope_for_workspace,
    grant_public_page_exposure,
    valid_public_exposed_version_id,
)
from backend.services.read_errors import ReadServiceError, ReadValidationError
from backend.services.review_risk_policy import ai_writes_require_human_review
from backend.services.review_supersession import supersede_review_with_lineage
from backend.services.tree_service import ensure_primary_tree_placement
from backend.spi.types import AI_PROVENANCES
from backend.utils.actor_display import (
    resolve_actor_display_name,
    resolve_actor_display_names,
)
from backend.utils.block_identity import (
    compute_block_map_with_ids,
    heal_block_map,
)
from backend.utils.content_hash import same_authored_markdown
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
from backend.utils.uuid import parse_uuid_or_none

if TYPE_CHECKING:
    from backend.policy.auth import ActorContext


MAX_ACTIVE_ALTERNATIVE_DRAFTS = 20

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
    ) -> None:
        super().__init__("stale base version")
        self.server_version_id = server_version_id
        self.server_content_hash = server_content_hash
        # Only ever set for the caller's OWN open edit (the same disclosure
        # rule as the two fields above). It says where that edit stands now
        # so the caller knows what to re-read. It is not a value to resend
        # unchanged: the edit moved because something else saved to it.
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


# Page links are page-level backlink edges. The regex captures the target text
# and intentionally discards #anchor fragments and |aliases; storing those
# would require a separate anchor-aware backlink contract. Title-style targets
# are slugified before PageLink rows are written.
WIKILINK_PATTERN = re.compile(
    r"(?<!\\)\[\[\s*(?P<target>[^\]\n|#]*?)(?:#[^\]\n|]+)?(?:\|[^\]\n]+)?\s*\]\]"
)

# Fenced code block boundary: ``` or ~~~ at the line start, optional language
# tag, captured by line so we can strip them before scanning for wikilinks.
# Without this, documentation pages demonstrating wikilink syntax inside a
# code block silently create phantom PageLink rows. Review §3.1 P-H1.
_FENCE_LINE = re.compile(r"^(?P<fence>`{3,}|~{3,})\s*([^\s`~]*)\s*$")
_INLINE_CODE = re.compile(r"`+")


def _strip_code_regions(body: str) -> str:
    """Replace fenced code blocks and inline code with whitespace of equal
    length so character offsets used by snippet generation stay aligned.

    The replacement preserves line count and column positions, which means the
    snippet_around_match radius math still produces stable previews relative to
    the original body.
    """
    lines = body.split("\n")
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        if fence is None:
            match = _FENCE_LINE.match(line.lstrip())
            if match:
                fence = match.group("fence")[0]  # ` or ~
                out.append(" " * len(line))
                continue
        else:
            match = _FENCE_LINE.match(line.lstrip())
            if match and match.group("fence")[0] == fence:
                fence = None
            out.append(" " * len(line))
            continue
        # Outside any fence: blank out inline `code` spans. The matcher walks
        # pairs of backtick runs so triple-backtick inline (rare) still pairs.
        result = []
        cursor = 0
        backtick_match = _INLINE_CODE.search(line, cursor)
        while backtick_match:
            run = backtick_match.group(0)
            close = line.find(run, backtick_match.end())
            if close == -1:
                break
            result.append(line[cursor : backtick_match.start()])
            result.append(" " * (close + len(run) - backtick_match.start()))
            cursor = close + len(run)
            backtick_match = _INLINE_CODE.search(line, cursor)
        result.append(line[cursor:])
        out.append("".join(result))
    return "\n".join(out)


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
    ) -> None:
        self.session_factory = session_factory
        self.body_preparer = (
            body_preparer if body_preparer is not None else BodyPreparer(lint_registry)
        )
        self.lint_registry = (
            lint_registry if lint_registry is not None else self.body_preparer.registry
        )
        self.change_audit_recorder = change_audit_recorder or ChangeReviewAuditRecorder(
            lint_registry=self.lint_registry
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
        page = self._resolve_page(session, page_id, workspace_slug, slug)
        visible_version_id, _is_draft = visible_context_version_id(page, actor)
        version = self._require_page_version(
            session,
            page,
            version_id or visible_version_id,
        )
        if not context_version_is_visible(version, actor):
            raise PageNotFoundError("page version not found")
        payload = self._payload_from_page(
            session,
            page,
            version,
            visible_version_id=visible_version_id,
            can_read_drafts=actor_can_read_drafts(actor),
            parent_visibility_actor=actor,
        )
        return page, version, payload

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
            return PublicPagePayload(
                id=page.id,
                slug=page.slug,
                # Title from the PUBLISHED body's heading — NOT page.title, which
                # tracks the (possibly unpublished) draft version, so a draft
                # rename would leak a private title while the body stays the
                # published snapshot. Fall back to the slug (already in the public
                # URL), never to page.title.
                title=title_from_markdown_heading(public_body) or page.slug,
                body_md=public_body,
                # The PUBLISHED head's own timestamp — NOT page.updated_at, which
                # is re-stamped by private draft/autosave edits that never change
                # the published body and would leak the timing of non-public
                # editing activity to anonymous observers.
                updated_at=version.created_at,
                exposure_scope=exposure_scope,
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
            # the published version is what the response should describe. The
            # `current_version_id` fallback below only applies to a page with
            # no published version yet, and even then must not hand back a
            # DIFFERENT author's private `editing` row: this actor may hold no
            # relationship to it at all beyond editor/owner role.
            fallback_id = _hidden_editing_id_replaced_with_published(
                page, page.current_version_id, actor_kind, actor_id
            )
            version = self._require_page_version(
                session,
                page,
                page.current_published_version_id or fallback_id,
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
            rows = session.execute(
                select(PageLink, ContextPage, backlink_modified_at)
                .join(ContextPage, ContextPage.id == PageLink.source_page_id)
                .outerjoin(
                    backlink_published,
                    backlink_published.id == ContextPage.current_published_version_id,
                )
                .where(PageLink.workspace_id == target_page.workspace_id)
                .where(
                    (PageLink.target_page_id == target_page.id)
                    | (
                        PageLink.target_page_id.is_(None)
                        & (PageLink.target_slug == target_page.slug)
                    )
                )
                .where(PageLink.source_page_id != target_page.id)
                .where(ContextPage.lifecycle_status != "archived")
                .where(
                    PageLink.source_version_id
                    == visible_context_version_expression(actor)
                )
                .order_by(backlink_modified_at.desc(), ContextPage.slug.asc())
                .limit(limit)
            ).all()
            backlinks = [
                PageBacklink(
                    id=page.id,
                    slug=page.slug,
                    title=page.title,
                    updated_at=modified_at or page.updated_at,
                    occurrence_count=link.occurrence_count,
                    snippet=link.first_snippet,
                )
                for link, page, modified_at in rows
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
            # P2.2/P2.6: `context_version_is_visible` additionally hides a
            # DIFFERENT author's "editing" row that the coarser role-based
            # `visible_statuses` filter above lets through. Filtering after a
            # single `limit + 1` fetch would silently undercount `has_more`
            # whenever a hidden row lands in that window -- fetch again from
            # where the batch left off until a full page of actually-visible
            # versions is assembled or the table is exhausted.
            versions: list[ContextPageVersion] = []
            fetch_cursor = before_version_no
            while len(versions) <= limit:
                batch = context_page_repo.list_versions(
                    session,
                    page.id,
                    limit + 1,
                    visible_statuses=(
                        None
                        if actor_can_read_drafts(actor)
                        else PUBLISHED_CONTEXT_VERSION_STATUSES
                    ),
                    before_version_no=fetch_cursor,
                )
                if not batch:
                    break
                fetch_cursor = batch[-1].version_no
                versions.extend(
                    version
                    for version in batch
                    if context_version_is_visible(version, actor)
                )
                if len(batch) <= limit:
                    # The repository had no more rows before this cursor --
                    # nothing later to find regardless of visibility.
                    break
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
            from backend.services.context_reconciliation import (
                verified_reconciliation_branch_is_available,
            )

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
                    is_latest_draft=(
                        actor_can_read_drafts(actor)
                        and version.id == page.latest_draft_version_id
                    ),
                    review_status=review_metadata[version.id].status,
                    review_item_id=review_metadata[version.id].review_item_id,
                    reconciliation_available=(
                        expose_draft_state
                        and verified_reconciliation_branch_is_available(
                            session,
                            page=page,
                            version=version,
                            actor_kind=actor.actor_kind if actor is not None else None,
                            actor_id=actor.actor_id if actor is not None else None,
                        )
                    ),
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
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
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
                audit_service=self.change_audit_recorder.audit_service,
                job_queue=self.job_queue,
                context_media_repository_factory=self.context_media_repository_factory,
                body_preparer=self.body_preparer,
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
        expected_content_hash: str | None = None,
    ) -> PagePublishResult:
        if reason is not None:
            validate_text(reason, field="reason", max_length=2000)
        from backend.schemas.context_write import PublishContextPageRequest
        from backend.services.context_write_service import ContextWriteService

        # Waiving the objective floor means "a person read the warning and said
        # yes". Only the editor shows that warning, so only a browser session
        # can answer it. Every other channel holds the same `draft-write` scope
        # and reaches this same route, so the gate is on provenance rather than
        # on the route -- and it refuses rather than ignoring the flag, because
        # a caller that asked to waive the floor must not be told it published
        # cleanly.
        if publish_despite_findings and actor_provenance != "browser_session":
            raise PageValidationError(
                "publishing despite format findings requires an interactive "
                "browser session"
            )

        with self.session_factory.begin() as session:
            # SQLite's SELECT ... FOR UPDATE is a no-op and pysqlite defers
            # BEGIN until the first DML, so the hash check below would not be
            # atomic with the publish: a concurrent autosave_page could rewrite
            # the draft row in place after the check and this call would ship
            # bytes it never verified. PostgreSQL keeps the narrower row lock.
            acquire_write_intent(session)
            self._lock_context_media_usage_intent_for_page(session, page_id)
            page = context_page_repo.get_page_by_id_for_update(session, page_id)
            if page is None:
                raise PageNotFoundError("page not found")
            staged = self._require_page_version(session, page, version_id)
            # A draft row is mutable, so naming the version does not name the
            # bytes. Another session of the same author -- a second tab -- can
            # overwrite that row between this caller staging its buffer and
            # asking to publish it, and publishing then ships somebody else's
            # work in progress under this caller's decision. Publishing states
            # the content it meant, the same way every other write does.
            if expected_content_hash is not None:
                if staged.content_hash != expected_content_hash:
                    disclosed_id, disclosed_hash = self._disclosable_version_fields(
                        staged, actor_kind, actor_id
                    )
                    raise PageConflictError(
                        server_version_id=disclosed_id,
                        server_content_hash=disclosed_hash,
                    )
            # Page-first browser creation and autosave intentionally keep the
            # author's working row private as ``editing``. The explicit Publish
            # action may submit that exact row without manufacturing a review:
            # browser-authored writes are trusted to go live directly, while
            # AI/bearer writes retain the staged review lifecycle. Perform this
            # narrow bridge under the page/write lock so the canonical publisher
            # below still owns every invariant and all changes roll back together.
            # Foreign, bearer-authenticated, alternative/stale editing rows and
            # pages whose submitted draft points elsewhere fall through to the
            # canonical stale-draft refusal without any state change.
            if (
                expected_content_hash is not None
                and actor_provenance == "browser_session"
                and actor_kind == "human"
                and staged.version_status == "editing"
                and staged.graph_status == "linear"
                and staged.created_by_actor_kind == actor_kind
                and staged.created_by_actor_id == actor_id
                and page.current_version_id == staged.id
                and page.latest_draft_version_id in {None, staged.id}
            ):
                staged.version_status = "draft"
                page.current_version_id = staged.id
                page.latest_draft_version_id = staged.id
                session.add_all([staged, page])
                session.flush()
            try:
                result = ContextWriteService(
                    audit_service=self.change_audit_recorder.audit_service,
                    job_queue=self.job_queue,
                    body_preparer=self.body_preparer,
                    context_media_repository_factory=(
                        self.context_media_repository_factory
                    ),
                ).publish_draft(
                    session=session,
                    workspace_id=page.workspace_id,
                    page_id=page.id,
                    request=PublishContextPageRequest(
                        version_id=version_id,
                        reason=reason,
                    ),
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    publish_despite_findings=publish_despite_findings,
                )
            except LegacyMediaCutoverConflictError as exc:
                raise PageContextMediaError(
                    str(exc),
                    error_code=exc.error_code,
                    page_id=page.id,
                ) from exc
            version = self._require_page_version(
                session,
                page,
                result.published_version_id,
            )
            return PagePublishResult(
                page=self._payload_from_page(session, page, version),
                findings=result.findings,
            )

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
            return PageVersionDiff(
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
        with self.session_factory.begin() as session:
            # SQLite: FOR UPDATE is a no-op, so take write intent before the
            # guarded read (see db_write_intent). PostgreSQL keeps the row lock.
            acquire_write_intent(session)
            self._lock_context_media_usage_intent_for_page(session, page_id)
            page = context_page_repo.get_page_by_id_for_update(session, page_id)
            if page is None:
                raise PageNotFoundError("page not found")
            target = self._require_page_version(session, page, version_id)
            current = self._require_page_version(session, page, page.current_version_id)
            if target.id == current.id:
                return self._payload_from_page(session, page, target)

            parsed = parse_page_frontmatter(target.body_md)
            title = title_from_markdown_heading(parsed.body) or page.title
            was_archived = page.lifecycle_status == "archived"
            version = self._insert_version_copy(
                session,
                page_id=page.id,
                parent_version_id=current.id,
                source_version=target,
                actor_kind=actor_kind,
                actor_id=actor_id,
                version_status="draft",
            )
            metadata_patch = page_metadata_delta(
                page,
                title=title,
                page_type=parsed.page_type,
                tags=parsed.tags,
                aliases=parsed.aliases,
            )
            stage_metadata = should_stage_page_metadata(page, version)
            if not stage_metadata:
                apply_page_metadata(
                    page,
                    page_type=parsed.page_type,
                    tags=parsed.tags,
                    aliases=parsed.aliases,
                )
                page.title = title
            page.current_version_id = version.id
            page.latest_draft_version_id = version.id
            if not (was_archived and actor_provenance in AI_PROVENANCES):
                page.lifecycle_status = (
                    "published" if page.current_published_version_id else "draft"
                )
            session.add(page)
            session.flush()
            if was_archived and actor_provenance not in AI_PROVENANCES:
                # restoring an archived page resurrects it — revive the tree
                # node with it (no searchable-but-tree-invisible ghosts, B-8)
                self._ensure_context_tree_placement(
                    session=session,
                    workspace_id=page.workspace_id,
                    page=page,
                    parent_id=None,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
            result = self.change_audit_recorder.record_context_page_write(
                session=session,
                workspace_id=page.workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="page.restore_version",
                page_id=page.id,
                version_id=version.id,
                payload={
                    "from_version_id": str(current.id),
                    "restored_from_version_id": str(target.id),
                    "draft_version_id": str(version.id),
                    "before_content_hash": current.content_hash,
                    "after_content_hash": version.content_hash,
                    "staged_metadata": stage_metadata and bool(metadata_patch),
                },
                actor_provenance=actor_provenance,
                body=target.body_md,
            )
            # `write_page`/`update_page` already supersede any unresolved
            # review pointed at the version they replace; restore_version
            # moves the same edit-head pointer but skipped this, leaving a
            # review targeting `current` unapprovable (pointer check fails)
            # and un-superseded (still pending) permanently (CTX-03).
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
            if metadata_patch:
                stage_page_metadata_patch(
                    session,
                    change_set_id=result.change_set_id,
                    page=page,
                    version=version,
                    patch=metadata_patch,
                )
            refresh_page_links(session, page=page, version=version)
            return self._payload_from_page(
                session,
                page,
                version,
                change_set_id=result.change_set_id,
                review_item_id=result.review_item_id,
                superseded_review_count=superseded_review_count,
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
    ) -> PagePayload:
        """Atomically create a page and its first, private "editing" version.

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
            acquire_write_intent(session)
            workspace = self._require_workspace(session, workspace_slug)
            next_title = title or "제목 없음"
            page_id = uuid4()
            default_slug = f"p-{page_id}"
            prepared = self._prepare_body(
                BodyInput(
                    body_md=body_md,
                    metadata=PageMetadataInput(
                        title=next_title,
                        slug=default_slug,
                        page_type=page_type,
                        tags=tags,
                        aliases=aliases,
                    ),
                    fallback_metadata=PreparedMetadata(
                        page_type=None,
                        tags=None,
                        aliases=None,
                    ),
                ),
                WriteContext(
                    workspace=workspace,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                    page=None,
                    version=None,
                    session=session,
                ),
            )
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
                version_status="editing",
            )
            # Without this, the page has no stored head at all: an ordinary
            # read_page/list_pages by its own author would 404 immediately
            # after a 201 create, since nothing else ever points at a fresh
            # page's first version for them (confirmed the hard way -- an
            # end-to-end create-then-read test failed until this was added).
            # Never `latest_draft_version_id`: that pointer is reserved for a
            # SUBMITTED draft (P2.2) and this version is still private
            # "editing", exactly like `begin_editing`'s fresh-session case.
            page.current_version_id = version.id
            page.placement_parent_node_id = placement_parent_node_id
            self._ensure_context_tree_placement(
                session=session,
                workspace_id=workspace.id,
                page=page,
                parent_id=placement_parent_node_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
            AuditService().emit(
                session=session,
                workspace_id=workspace.id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="page.create",
                target_kind="context_page",
                target_id=page.id,
                payload={"version_id": str(version.id)},
            )
            return self._payload_from_page(
                session,
                page,
                version,
                current_version_id_override=version.id,
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
    ) -> PagePayload:
        """Start or resume this actor's own editing session on ``page_id`` (P8.3).

        Idempotent: an existing open "editing" row for this (page, author) is
        returned unchanged, never overwritten by a second call -- enforced by
        the ``uq_cpv_current_editing_per_author`` partial unique index. A
        fresh row is parented on the current published head (or the page's
        own current head for a page that has never published), matching the
        "base version/hash the caller means to build from" contract. Shared
        by Browser REST and MCP -- there is exactly one implementation.

        P8.5: ``idempotency_key`` is required and replayed through the shared
        Context-command ledger (``execute_idempotent_write``), and
        ``retry_only`` asks for a prior receipt's outcome without touching the
        page at all -- the lookup happens before anything below is entered.
        """

        with self.session_factory.begin() as session:

            def _execute() -> PagePayload:
                acquire_write_intent(session)
                self._lock_context_media_usage_intent_for_page(session, page_id)
                page = context_page_repo.get_page_by_id_for_update(session, page_id)
                if page is None:
                    raise PageNotFoundError("page not found")
                existing = session.scalar(
                    select(ContextPageVersion).where(
                        ContextPageVersion.page_id == page.id,
                        ContextPageVersion.version_status == "editing",
                        ContextPageVersion.created_by_actor_kind == actor_kind,
                        ContextPageVersion.created_by_actor_id == actor_id,
                    )
                )
                if existing is not None:
                    return self._payload_from_page(
                        session,
                        page,
                        existing,
                        current_version_id_override=existing.id,
                    )
                base_id = page.current_published_version_id or page.current_version_id
                base = (
                    self._require_page_version(session, page, base_id)
                    if base_id is not None
                    else None
                )
                if base is not None and base.version_status == "editing":
                    # `page.current_version_id` is a page-global pointer that
                    # ordinary autosaves (`_set_current_version`) legitimately
                    # leave pointing at an "editing" row while it is still
                    # private to its own author (P8.1). We already know this
                    # actor has no open "editing" row of their own (the
                    # `existing` check above returned early otherwise), so an
                    # "editing" row found here always belongs to a DIFFERENT
                    # author. Using it as a base would copy that author's
                    # private, unsubmitted content into this actor's new
                    # editing row -- exactly the cross-author leak P8 exists to
                    # close. Treat it as "nothing to build from" instead.
                    base = None
                if base_version_id is not None or base_content_hash is not None:
                    actual_id = base.id if base is not None else None
                    actual_hash = base.content_hash if base is not None else None
                    if base_version_id != actual_id or base_content_hash != actual_hash:
                        raise PageConflictError(
                            server_version_id=actual_id,
                            server_content_hash=actual_hash,
                        )
                version = self._insert_version_copy(
                    session,
                    page_id=page.id,
                    parent_version_id=base.id if base is not None else None,
                    source_version=base,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    version_status="editing",
                )
                previous_revision = self._highest_editing_revision(
                    session, page, actor_kind, actor_id, excluding=version.id
                )
                if previous_revision is not None:
                    version.editing_revision = previous_revision + 1
                    session.add(version)
                    session.flush()
                AuditService().emit(
                    session=session,
                    workspace_id=page.workspace_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    action="page.begin_context_edit",
                    target_kind="context_page",
                    target_id=page.id,
                    payload={
                        "version_id": str(version.id),
                        "base_version_id": (str(base.id) if base is not None else None),
                    },
                )
                return self._payload_from_page(
                    session,
                    page,
                    version,
                    current_version_id_override=version.id,
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
                        "base_version_id": (
                            str(base_version_id)
                            if base_version_id is not None
                            else None
                        ),
                        "base_content_hash": base_content_hash,
                    }
                ),
                result_model=PagePayload,
                execute=_execute,
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
    ) -> PagePayload:
        """ "Save draft": lock the caller's own editing content and submit it (P8.1).

        Author-only -- this is not a reviewer action. Requires the caller's
        own current ``editing_revision`` (covers body, title, slug, tags,
        aliases, staged metadata -- not just ``content_hash``) so a stale
        client cannot silently overwrite a newer autosave with an older one
        just because the body happens to still match. Once locked to
        ``draft``, further edits must open a new editing version via
        ``begin_context_edit`` -- this method never mutates the version it
        just submitted.

        P8.5: ``idempotency_key`` is required -- a retried "Save draft" must
        never create a second ``ReviewItem`` for the same submission.
        """
        if reason is not None:
            validate_text(reason, field="reason", max_length=2000)

        with self.session_factory.begin() as session:

            def _execute() -> PagePayload:
                acquire_write_intent(session)
                self._lock_context_media_usage_intent_for_page(session, page_id)
                page = context_page_repo.get_page_by_id_for_update(session, page_id)
                if page is None:
                    raise PageNotFoundError("page not found")
                version = self._require_page_version(session, page, version_id)
                # A DIFFERENT author's version -- editing or not -- must be
                # opaque not-found here (P2.2/P2.6): a 409 Conflict would itself
                # confirm "this id exists" to a non-owner regardless of which
                # fields come back populated. The version's OWN author gets a
                # clear conflict instead when it simply is not "editing" any
                # more (already submitted, published, or discarded) -- that is
                # ordinary same-author feedback, not a disclosure to someone
                # else.
                if (
                    version.created_by_actor_kind != actor_kind
                    or version.created_by_actor_id != actor_id
                ):
                    raise PageNotFoundError("page version not found")
                if version.version_status != "editing":
                    raise PageConflictError(
                        server_version_id=version.id,
                        server_content_hash=version.content_hash,
                    )
                if version.editing_revision != expected_editing_revision:
                    # The caller's own open edit (ownership checked above):
                    # name where it stands now, as patch and update do, so the
                    # caller knows what to re-read before submitting it.
                    raise PageConflictError(
                        server_version_id=version.id,
                        server_content_hash=version.content_hash,
                        server_editing_revision=version.editing_revision,
                    )
                # page.latest_draft_version_id is still a page-global pointer
                # (P8.2's full removal is out of scope this pass) -- overwriting
                # it out from under a DIFFERENT author's still-pending draft
                # would not just move the pointer, it would make
                # review_service.py's supersession check and
                # assert_context_publish_candidate(require_latest_draft=True)
                # silently treat that other, still-unreviewed submission as
                # stale. Fail this second submit closed instead of corrupting
                # the first one.
                existing_draft_id = page.latest_draft_version_id
                if existing_draft_id is not None and existing_draft_id != version.id:
                    existing_draft = context_page_repo.get_version(
                        session, existing_draft_id
                    )
                    if (
                        existing_draft is not None
                        and existing_draft.version_status == "draft"
                    ):
                        raise PageConflictError(
                            server_version_id=existing_draft.id,
                            server_content_hash=existing_draft.content_hash,
                        )
                # Historical editing versions remain readable, but a new
                # submission must satisfy today's complete stored-body policy.
                validate_page_markdown_input(version.body_md)
                version.version_status = "draft"
                # ReviewService.apply_decision's supersession check requires
                # page.latest_draft_version_id == the reviewed version (a
                # pre-existing page-global-pointer dependency, not something
                # this method introduces). Without this, approval would reject
                # every submission from this flow as "stale" unconditionally.
                page.current_version_id = version.id
                page.latest_draft_version_id = version.id
                session.add_all([version, page])
                session.flush()
                result = self.change_audit_recorder.record_context_page_write(
                    session=session,
                    workspace_id=page.workspace_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    action="page.submit_context_edit_for_review",
                    page_id=page.id,
                    version_id=version.id,
                    payload={"reason": reason},
                    actor_provenance=actor_provenance,
                    body=version.body_md,
                    needs_review=True,
                )
                refresh_page_links(session, page=page, version=version)
                return self._payload_from_page(
                    session,
                    page,
                    version,
                    review_item_id=result.review_item_id,
                    change_set_id=result.change_set_id,
                    current_version_id_override=version.id,
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
        """Discard the caller's own open "editing" row (P8.3).

        Terminal, not a hard-delete: the row becomes ``discarded`` so audit
        and History are preserved, and the ``uq_cpv_current_editing_per_
        author`` partial unique slot (scoped to ``version_status =
        'editing'``) is released for a future ``begin_context_edit``. If
        this page has never published or otherwise submitted anything
        (its only version is the one being discarded), the page aggregate
        moves to ``archived`` so it drops out of normal listings; a page
        with prior published/shared history keeps its existing head
        untouched.

        P8.5: ``idempotency_key`` is required. Returns a small result (rather
        than ``None``) so the shared Context-command ledger has a durable
        ``BaseModel`` to replay.
        """

        with self.session_factory.begin() as session:

            def _execute() -> DiscardContextEditResult:
                acquire_write_intent(session)
                self._lock_context_media_usage_intent_for_page(session, page_id)
                page = context_page_repo.get_page_by_id_for_update(session, page_id)
                if page is None:
                    raise PageNotFoundError("page not found")
                version = self._require_page_version(session, page, version_id)
                if (
                    version.created_by_actor_kind != actor_kind
                    or version.created_by_actor_id != actor_id
                ):
                    raise PageNotFoundError("page version not found")
                if version.version_status != "editing":
                    raise PageConflictError(
                        server_version_id=version.id,
                        server_content_hash=version.content_hash,
                    )
                version.version_status = "discarded"
                session.add(version)
                # Every save of an open edit points the page's draft and
                # current pointers at it (_set_current_version). Left naming
                # the discarded row, they made it the page's visible draft for
                # every reader who can see drafts -- editing_row_hidden_from_
                # actor only hides rows that are still "editing" -- and made a
                # never-published page look like it had history, so it was not
                # archived. Point them back at what the page had without it.
                # A rejected draft keeps its "draft" status but is not what
                # the page had: rejection already moved the pointers off it,
                # and restoring it brought the refused text back as the page.
                if page.latest_draft_version_id == version.id:
                    live_draft = next(
                        (
                            candidate
                            for candidate in context_page_repo.list_versions(
                                session, page.id, 20, visible_statuses=("draft",)
                            )
                            if candidate.id != version.id
                            and candidate.graph_status == "linear"
                            and self._draft_still_open(session, page, candidate)
                        ),
                        None,
                    )
                    page.latest_draft_version_id = (
                        live_draft.id if live_draft is not None else None
                    )
                if page.current_version_id == version.id:
                    page.current_version_id = (
                        page.latest_draft_version_id
                        or page.current_published_version_id
                    )
                # Earlier discarded sessions are not history either.
                has_other_history = (
                    page.current_published_version_id is not None
                    or page.latest_draft_version_id is not None
                    or any(
                        candidate.id != version.id
                        for candidate in context_page_repo.list_versions(
                            session,
                            page.id,
                            2,
                            visible_statuses=(
                                "draft",
                                "published_snapshot",
                                "superseded_snapshot",
                            ),
                        )
                    )
                )
                if not has_other_history:
                    page.lifecycle_status = "archived"
                    page.current_version_id = None
                elif page.lifecycle_status != "archived":
                    page.lifecycle_status = (
                        "published" if page.current_published_version_id else "draft"
                    )
                session.add(page)
                session.flush()
                # Local import: mcp_chat_service -> policy.auth -> app.state ->
                # page_service is an existing import chain.
                from backend.services.mcp_chat_service import purge_previews_for_page

                # The author threw this session's text away: drop the edit
                # previews Chats kept of it (digests and sizes stay).
                purge_previews_for_page(
                    session, page.workspace_id, page.id, version_id=version.id
                )
                AuditService().emit(
                    session=session,
                    workspace_id=page.workspace_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    action="page.discard_context_edit",
                    target_kind="context_page",
                    target_id=page.id,
                    payload={
                        "version_id": str(version.id),
                        "page_archived": not has_other_history,
                    },
                )
                return DiscardContextEditResult(
                    version_id=version.id,
                    page_archived=not has_other_history,
                )

            return execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                tool_name="discard_context_edit",
                idempotency_key=idempotency_key,
                fingerprint=request_fingerprint(
                    {
                        "page_id": str(page_id),
                        "version_id": str(version_id),
                    }
                ),
                result_model=DiscardContextEditResult,
                execute=_execute,
                retry_only=retry_only,
            )

    def _draft_still_open(
        self,
        session: Session,
        page: ContextPage,
        draft: ContextPageVersion,
    ) -> bool:
        """Whether a "draft" row is still a live draft of the page.

        A draft nobody was asked to review (a browser session draft) is. A
        reviewed one is only while a review of it is still open: once every
        review of it is resolved -- rejected, or superseded by a later draft
        -- it is history, even though its status still says "draft".
        """

        reviews = self._context_reviews_for_version(
            session=session, page=page, version=draft
        )
        return not reviews or any(
            review.status in {"pending", "changes_requested"} for review in reviews
        )

    def list_my_editing_versions(
        self,
        workspace_id: UUID,
        actor_kind: str,
        actor_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ListMyContextEditsResult:
        """This actor's own open "editing" rows across the workspace (P8.3/P8.6).

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
        with self.session_factory() as session:
            rows = context_page_repo.list_editing_versions_for_actor(
                session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                limit=limit + 1,
                offset=offset,
            )
            visible_rows = rows[:limit]
            items = [
                MyContextEditSummary(
                    page_id=page.id,
                    version_id=version.id,
                    title=page.title,
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
        retry_only: bool = False,
    ) -> PagePayload:
        """Apply typed block/section edit operations to the caller's own open
        "editing" row in place (P8.6's seventh tool).

        Ownership is checked directly against the caller-supplied
        ``version_id`` -- the same opaque-404 pattern
        ``submit_context_edit_for_review``/``discard_context_edit`` use, NOT
        ``autosave_page``'s page-global-pointer resolution -- so a different
        author's editing row reads as not_found, never a fork (P2.2/P2.6).
        ``operations`` (the same typed union ``patch_draft`` accepts, minus
        reconciliation) are resolved against THIS row's own current
        ``body_md`` via ``context_editing.apply_operations_with_block_map``,
        the same pure resolver ``patch_draft`` uses, and the resulting body is
        then persisted onto the SAME row exactly the way ``autosave_page``
        persists an in-place editing-row body change (never a new version) --
        the only difference from ``update_context_edit`` is how the new body
        is computed, not how it is written. The whole ownership/CAS/apply/
        persist sequence runs inside this method's own idempotency-ledger
        gate so a byte-identical retry replays the stored receipt instead of
        re-validating against a revision its own prior success already moved
        past.

        P8.5: ``idempotency_key`` is required.
        """

        with self.session_factory.begin() as session:

            def _execute() -> PagePayload:
                acquire_write_intent(session)
                self._lock_context_media_usage_intent_for_page(session, page_id)
                page = context_page_repo.get_page_by_id_for_update(session, page_id)
                if page is None:
                    raise PageNotFoundError("page not found")
                version = self._require_page_version(session, page, version_id)
                # A DIFFERENT author's version -- editing or not -- must be
                # opaque not-found here (P2.2/P2.6), matching
                # submit_context_edit_for_review/discard_context_edit exactly.
                if (
                    version.created_by_actor_kind != actor_kind
                    or version.created_by_actor_id != actor_id
                ):
                    raise PageNotFoundError("page version not found")
                if version.version_status != "editing":
                    disclosed_id, disclosed_hash = self._disclosable_version_fields(
                        version, actor_kind, actor_id
                    )
                    raise PageConflictError(
                        server_version_id=disclosed_id,
                        server_content_hash=disclosed_hash,
                    )
                if version.editing_revision != expected_editing_revision:
                    disclosed_id, disclosed_hash = self._disclosable_version_fields(
                        version, actor_kind, actor_id
                    )
                    raise PageConflictError(
                        server_version_id=disclosed_id,
                        server_content_hash=disclosed_hash,
                        server_editing_revision=(
                            version.editing_revision
                            if disclosed_id is not None
                            else None
                        ),
                    )

                current_body = version.body_md
                # The author's own open edit: a map left describing an older
                # body is rebuilt here (and persisted below) rather than
                # refused -- read_structure_map derives the same ids.
                block_map, _map_repaired = heal_block_map(
                    current_body, version.block_map, identity_seed=str(version.id)
                )
                outline = (
                    None
                    if target_ids_for_operations(operations) is not None
                    else parse_outline(current_body)
                )
                try:
                    applied = apply_operations_with_block_map(
                        current_body,
                        outline,
                        operations,
                        block_map=block_map,
                        identity_seed=str(version_id),
                    )
                except ContextEditError as exc:
                    # Relay the recovery facts, matching patch_draft's own
                    # ContextEditError -> ReadValidationError projection
                    # (context_write_service.py) -- the caller never holds the
                    # document, so a bare failure would force a full re-read.
                    raise ReadValidationError(
                        str(exc),
                        error_code=exc.error_code,
                        warnings=list(exc.warnings),
                        details=list(exc.details),
                    ) from exc

                prepared = self._prepare_body(
                    BodyInput(applied.body),
                    WriteContext(
                        workspace=self._workspace_for(session, page.workspace_id),
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        actor_provenance=actor_provenance,
                        page=page,
                        version=version,
                        session=session,
                    ),
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
                self._rewrite_body_in_place(
                    version,
                    prepared,
                    base_block_map=block_map,
                    prepared_block_map=next_block_map,
                )
                version.actor_type = actor_kind
                version.actor_id = parse_uuid_or_none(actor_id)
                version.actor_display_name = resolve_actor_display_name(
                    session,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
                version.created_by_actor_kind = actor_kind
                version.created_by_actor_id = actor_id
                version.editing_revision += 1
                session.add(version)
                self._sync_version_usage(
                    session,
                    page=page,
                    version=version,
                    body_md=new_body,
                )
                page.updated_at = utc_now()
                self._set_current_version(session, page, version)
                refresh_page_links(session, page=page, version=version)
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
                        "operation_count": len(operations),
                    },
                )
                payload = self._payload_from_page(
                    session,
                    page,
                    version,
                    current_version_id_override=version.id,
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
                retry_only=retry_only,
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
                    workspace=workspace,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                    page=page,
                    version=None,
                    session=session,
                ),
            )
            body_md = prepared.body
            requires_human_review = self._stages_as_draft(
                workspace=workspace,
                actor_provenance=actor_provenance,
                body_md=body_md,
            )
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
                                page_type=page.page_type,
                                tags=list(page.tags or []),
                                aliases=list(page.aliases or []),
                            ),
                        ),
                        WriteContext(
                            workspace=workspace,
                            actor_kind=actor_kind,
                            actor_id=actor_id,
                            actor_provenance=actor_provenance,
                            page=page,
                            version=None,
                            session=session,
                        ),
                    )
                    body_md = prepared.body

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
                    publish=not create_as_draft,
                )
                refresh_page_links(session, page=page, version=version)
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
                return self._payload_from_page(session, page, version)

            # Not actor-scoped: `write_page`'s upsert-by-slug write always
            # targets the page's own shared head, same as before P8.1 -- it
            # has no "resume my private editing row" contract of its own
            # (that is `begin_context_edit`/`autosave_page`'s job).
            current = self._require_page_version(
                session, page, editable_context_version_id(page)
            )
            self._validate_base(
                session,
                page,
                current,
                base_version_id,
                base_content_hash,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )

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
            if same_authored_markdown(body_md, current.body_md) and (
                (
                    current.version_status not in ("editing", "draft")
                    and not requires_human_review
                )
                or (
                    current.created_by_actor_kind == actor_kind
                    and current.created_by_actor_id == actor_id
                    and current.version_status in ("editing", "draft")
                )
            ):
                if title_changed or metadata_changed:
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
                    refresh_page_links(session, page=page, version=current)
                return self._payload_from_page(session, page, current)

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
                publish=not requires_human_review,
            )
            refresh_page_links(session, page=page, version=version)
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
        with self.session_factory.begin() as session:
            # SQLite: FOR UPDATE is a no-op, so take write intent before the
            # guarded read (see db_write_intent). PostgreSQL keeps the row lock.
            acquire_write_intent(session)
            self._lock_context_media_usage_intent_for_page(session, page_id)
            page = context_page_repo.get_page_by_id_for_update(session, page_id)
            if page is None:
                raise PageNotFoundError("page not found")
            next_title = title or page.title
            prepared = self._prepare_body(
                BodyInput(
                    body_md=body_md,
                    metadata=PageMetadataInput(
                        title=next_title,
                        slug=page.slug,
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
                    workspace=self._workspace_for(session, page.workspace_id),
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                    page=page,
                    version=None,
                    session=session,
                ),
            )
            body_md = prepared.body
            requires_human_review = self._stages_as_draft(
                workspace=self._workspace_for(session, page.workspace_id),
                actor_provenance=actor_provenance,
                body_md=body_md,
            )
            # Not actor-scoped: `update_page` always targets the page's own
            # shared head, same as before P8.1 -- unlike `autosave_page`, it
            # inserts a fresh "draft" and may publish it immediately
            # (`publish=not requires_human_review` below), which is only a
            # safe/expected outcome for the shared head, never for another
            # author's still-private, unlinked "editing" row.
            current = self._require_page_version(
                session, page, editable_context_version_id(page)
            )
            try:
                self._validate_base(
                    session,
                    page,
                    current,
                    base_version_id,
                    base_content_hash,
                    expected_editing_revision,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
            except PageConflictError:
                if on_stale != "preserve_as_alternative":
                    raise
                return self._preserve_alternative(
                    session=session,
                    page=page,
                    current=current,
                    base_version=self._require_alternative_base(
                        session=session,
                        page=page,
                        current=current,
                        base_version_id=base_version_id,
                        base_content_hash=base_content_hash,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                    ),
                    title=title,
                    prepared=prepared,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                )
            placement_changed = (
                destination_node_id is not None
                and destination_node_id != page.placement_parent_node_id
            )
            if not requires_human_review:
                placement_changed = self._move_context_tree_placement(
                    session=session,
                    workspace_id=page.workspace_id,
                    page=page,
                    destination_node_id=destination_node_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
            stage_metadata = should_stage_page_metadata(page, current)
            metadata_patch = page_metadata_delta(
                page,
                title=next_title,
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
            canonical_title_changed = next_title != page.title
            if placement_changed:
                metadata_patch["placement_parent_node_id"] = str(destination_node_id)

            # Preserve NFC/newline no-ops without ignoring Markdown hard breaks.
            if same_authored_markdown(body_md, current.body_md) and (
                (
                    current.version_status not in ("editing", "draft")
                    and not requires_human_review
                )
                or (
                    current.created_by_actor_kind == actor_kind
                    and current.created_by_actor_id == actor_id
                    and current.version_status in ("editing", "draft")
                )
            ):
                current.editing_revision += 1
                if title_changed or metadata_changed or placement_changed:
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
                        next_title,
                    )
                    if title_changed and not stage_metadata:
                        # Keep the sidebar tree node in sync with the new title.
                        # write_page calls this on every commit; the
                        # metadata-only branch of update_page used to skip it,
                        # producing a stale tree label until the next slug-keyed
                        # write. Review §2.2 P-C2.
                        self._ensure_context_tree_placement(
                            session=session,
                            workspace_id=page.workspace_id,
                            page=page,
                            parent_id=(
                                destination_node_id if placement_changed else None
                            ),
                            actor_kind=actor_kind,
                            actor_id=actor_id,
                            move_existing=placement_changed,
                        )
                    result = self.change_audit_recorder.record_context_page_write(
                        session=session,
                        workspace_id=page.workspace_id,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        action="page.update",
                        page_id=page.id,
                        version_id=current.id,
                        payload={
                            "before_content_hash": current.content_hash,
                            "after_content_hash": current.content_hash,
                            "metadata_only": (
                                metadata_changed
                                and not title_changed
                                and not placement_changed
                            ),
                            "placement_only": (
                                placement_changed
                                and not title_changed
                                and not metadata_changed
                            ),
                            "title_only": (
                                title_changed
                                and not metadata_changed
                                and not placement_changed
                            ),
                            "staged_metadata": stage_metadata and bool(metadata_patch),
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
                    if title_changed or metadata_changed:
                        refresh_page_links(session, page=page, version=current)
                return self._payload_from_page(session, page, current)

            was_archived = page.lifecycle_status == "archived"
            version = self._insert_version(
                session,
                page_id=page.id,
                parent_version_id=current.id,
                body=prepared,
                actor_kind=actor_kind,
                actor_id=actor_id,
                version_status="draft",
            )
            # The row is inserted as a draft so the canonical lifecycle can
            # validate and promote it, but a direct browser write is not a
            # staged draft. Keep canonical metadata in the same publish
            # transaction; AI/background writes retain the metadata patch.
            version_stages_metadata = (
                requires_human_review and should_stage_page_metadata(page, version)
            )
            if version_stages_metadata and placement_changed:
                metadata_patch["placement_parent_node_id"] = str(destination_node_id)
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
                next_title,
                publish=not requires_human_review,
            )
            if was_archived and not version_stages_metadata:
                # resurrecting an archived page must revive its tree node
                # too, or it becomes a searchable ghost the tree never shows
                # (B-8: page lifecycle and placement move together)
                self._ensure_context_tree_placement(
                    session=session,
                    workspace_id=page.workspace_id,
                    page=page,
                    parent_id=(destination_node_id if placement_changed else None),
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    move_existing=placement_changed,
                )
            elif canonical_title_changed and not version_stages_metadata:
                # New-version branch: same rationale as the no-op branch above —
                # the tree node label is keyed on page.title, so title changes
                # via PATCH must propagate or the sidebar shows a stale label.
                self._ensure_context_tree_placement(
                    session=session,
                    workspace_id=page.workspace_id,
                    page=page,
                    parent_id=(destination_node_id if placement_changed else None),
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    move_existing=placement_changed,
                )
            refresh_page_links(session, page=page, version=version)
            result = self.change_audit_recorder.record_context_page_write(
                session=session,
                workspace_id=page.workspace_id,
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
            return context_page_repo.insert_version(
                session,
                body=body,
                context_media_repository=repository,
                **kwargs,
            )
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
            return context_page_repo.insert_version_copy(
                session,
                source_version=source_version,
                context_media_repository=repository,
                **kwargs,
            )
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

    def _require_context_media_repository(
        self, session: Session
    ) -> ContextMediaRepository:
        if self.context_media_repository_factory is None:
            raise PageValidationError("Context media is not configured")
        return self.context_media_repository_factory(session)

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
    ) -> PagePayload:
        """Save a whole body for this actor (Browser autosave and MCP
        update_context_edit).

        ``editing_version_id`` (MCP's ``version_id``) binds the save to the
        open editing row the caller last read: when given, it must BE this
        actor's open editing row, or the save is a conflict naming the row
        that is open now (or saying none is).
        """

        # Empty on an idempotency replay, where no write callback executes.
        receipt_base: dict[str, object] = {}
        with self.session_factory.begin() as session:

            def _execute() -> PagePayload:
                page = self._autosave_lock_and_load(
                    session=session,
                    page_id=page_id,
                    editing_version_id=editing_version_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
                current = self._autosave_resolve_target(
                    session=session,
                    page=page,
                    session_version_id=session_version_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
                same_actor_draft = (
                    current.version_status in ("editing", "draft")
                    and current.graph_status == "linear"
                    and current.created_by_actor_kind == actor_kind
                    and current.created_by_actor_id == actor_id
                )
                receipt_base["body"] = current.body_md
                receipt_base["revision"] = current.editing_revision
                alternative_base = self._autosave_check_base(
                    session=session,
                    page=page,
                    current=current,
                    base_version_id=base_version_id,
                    base_content_hash=base_content_hash,
                    expected_editing_revision=expected_editing_revision,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    on_stale=on_stale,
                )
                next_title = title or page.title
                prepared = self._autosave_prepare(
                    session=session,
                    page=page,
                    current=current,
                    title=next_title,
                    body_md=body_md,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    page_type=page_type,
                    tags=tags,
                    aliases=aliases,
                    actor_provenance=actor_provenance,
                )
                if alternative_base is not None:
                    return self._preserve_alternative(
                        session=session,
                        page=page,
                        current=current,
                        base_version=alternative_base,
                        title=title,
                        prepared=prepared,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        actor_provenance=actor_provenance,
                    )
                baseline_patch = (
                    pending_page_metadata_patch(
                        session,
                        page=page,
                        version_id=current.id,
                    )
                    if same_actor_draft
                    else None
                )
                metadata_patch = page_metadata_delta(
                    page,
                    title=next_title,
                    page_type=prepared.metadata.page_type,
                    tags=prepared.metadata.tags,
                    aliases=prepared.metadata.aliases,
                    baseline_patch=baseline_patch,
                )
                effective_parent_id = (
                    parse_uuid_or_none(baseline_patch.get("placement_parent_node_id"))
                    if baseline_patch and "placement_parent_node_id" in baseline_patch
                    else page.placement_parent_node_id
                )
                if (
                    destination_node_id is not None
                    and destination_node_id != effective_parent_id
                ):
                    metadata_patch["placement_parent_node_id"] = str(
                        destination_node_id
                    )

                # Bytes, not content_hash: the hash ignores trailing spaces, so
                # a save that only added or removed a hard break (two trailing
                # spaces) was answered as succeeded and silently not stored.
                if prepared.body == current.body_md and not metadata_patch:
                    # P2.2/P2.6: `current` is the page-global pointer -- returning
                    # it unchanged is only safe when it is published (anyone with
                    # read access already sees it) or genuinely this actor's own
                    # editing/draft row. A different author's still-open editing
                    # row must not be handed back just because a caller's claimed
                    # base happened to already match its hash/id.
                    if same_actor_draft or current.version_status not in (
                        "editing",
                        "draft",
                    ):
                        return self._payload_from_page(session, page, current)

                # An AI client saving its OWN open "editing" row is the P8
                # editing state machine: private working state, reviewed once
                # at submit_context_edit_for_review. It takes the same
                # one-ledger-per-session path a browser editor does. Routing it
                # through the reviewed-draft path recorded a ReviewItem per
                # save, and the next save saw that review and forked a new
                # draft -- every second update_context_edit broke the session.
                own_open_edit = (
                    current.version_status == "editing"
                    and current.graph_status == "linear"
                    and current.created_by_actor_kind == actor_kind
                    and current.created_by_actor_id == actor_id
                )
                if actor_provenance in AI_PROVENANCES and not own_open_edit:
                    return self._autosave_persist_reviewed_draft(
                        session=session,
                        page=page,
                        current=current,
                        prepared=prepared,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        actor_provenance=actor_provenance,
                        session_version_id=session_version_id,
                        metadata_patch=metadata_patch,
                    )

                return self._autosave_persist_editing_row(
                    session=session,
                    page=page,
                    current=current,
                    prepared=prepared,
                    next_title=next_title,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    session_version_id=session_version_id,
                    same_actor_draft=same_actor_draft,
                    own_open_edit=own_open_edit,
                    baseline_patch=baseline_patch,
                    metadata_patch=metadata_patch,
                )

            return execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                tool_name="autosave_page",
                idempotency_key=idempotency_key,
                fingerprint=request_fingerprint(
                    {
                        "page_id": str(page_id),
                        "title": title,
                        "body_md": body_md,
                        "base_version_id": str(base_version_id),
                        "base_content_hash": base_content_hash,
                        "expected_editing_revision": expected_editing_revision,
                        "session_version_id": (
                            str(session_version_id)
                            if session_version_id is not None
                            else None
                        ),
                        "on_stale": on_stale,
                        "page_type": page_type,
                        "tags": tags,
                        "aliases": aliases,
                        "destination_node_id": (
                            str(destination_node_id)
                            if destination_node_id is not None
                            else None
                        ),
                        # Only when sent, so a receipt stored for a request
                        # without it still matches the same request.
                        **(
                            {"editing_version_id": str(editing_version_id)}
                            if editing_version_id is not None
                            else {}
                        ),
                    }
                ),
                result_model=PagePayload,
                execute=lambda: self._autosave_receipt(
                    result=_execute(),
                    receipt_base=receipt_base,
                    with_edit_receipt=with_edit_receipt,
                ),
                retry_only=retry_only,
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

    def _autosave_resolve_target(
        self,
        *,
        session: Session,
        page: ContextPage,
        session_version_id: UUID | None,
        actor_kind: str,
        actor_id: str,
    ) -> ContextPageVersion:
        current = self._require_page_version(
            session,
            page,
            self._editable_version_id_for_actor(session, page, actor_kind, actor_id),
        )
        if session_version_id is not None:
            session_version = self._require_page_version(
                session,
                page,
                session_version_id,
            )
            if current.id != session_version.id:
                session_reviews = self._context_reviews_for_version(
                    session=session,
                    page=page,
                    version=session_version,
                )
                can_resume_rejected_session = (
                    session_version.version_status == "draft"
                    and session_version.graph_status == "linear"
                    and session_version.created_by_actor_kind == actor_kind
                    and session_version.created_by_actor_id == actor_id
                    and any(review.status == "rejected" for review in session_reviews)
                )
                if not can_resume_rejected_session:
                    disclosed_version_id, disclosed_content_hash = (
                        self._disclosable_version_fields(current, actor_kind, actor_id)
                    )
                    raise PageConflictError(
                        server_version_id=disclosed_version_id,
                        server_content_hash=disclosed_content_hash,
                    )
                # Rejection deliberately restores the visible page head to
                # the published snapshot. The submitting session may still
                # continue from its exact rejected bytes, but only by
                # forking a successor below; the resolved review and its
                # immutable version are never rewritten.
                current = session_version
        return current

    def _autosave_check_base(
        self,
        *,
        session: Session,
        page: ContextPage,
        current: ContextPageVersion,
        base_version_id: UUID | None,
        base_content_hash: str | None,
        expected_editing_revision: int | None,
        actor_kind: str,
        actor_id: str,
        on_stale: str,
    ) -> ContextPageVersion | None:
        try:
            self._validate_base(
                session,
                page,
                current,
                base_version_id,
                base_content_hash,
                expected_editing_revision,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
        except PageConflictError:
            if on_stale != "preserve_as_alternative":
                raise
            # A stale ID must still identify the exact historical bytes the
            # caller read before preparation can validate the proposed body.
            return self._require_alternative_base(
                session=session,
                page=page,
                current=current,
                base_version_id=base_version_id,
                base_content_hash=base_content_hash,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
        return None

    def _autosave_prepare(
        self,
        *,
        session: Session,
        page: ContextPage,
        current: ContextPageVersion,
        title: str,
        body_md: str,
        page_type: str | None,
        tags: list[str] | None,
        aliases: list[str] | None,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
    ) -> PreparedBody:
        workspace = self._workspace_for(session, page.workspace_id)
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
                    page_type=page.page_type,
                    tags=list(page.tags or []),
                    aliases=list(page.aliases or []),
                ),
            ),
            WriteContext(
                workspace=workspace,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                page=page,
                version=current,
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
        session_version_id: UUID | None,
        same_actor_draft: bool,
        own_open_edit: bool,
        baseline_patch: dict[str, object] | None,
        metadata_patch: dict[str, object],
    ) -> PagePayload:
        body_md = prepared.body
        staged_patch = dict(baseline_patch or {})
        staged_patch.update(metadata_patch)

        # A ReviewItem speaks for the exact bytes stored in its referenced
        # version.  Browser and bearer authentication intentionally resolve
        # to the same human actor, so actor ownership alone is not enough to
        # decide that this mutable row still belongs to the browser session.
        # Fork before changing a reviewed row; later browser autosaves keep
        # mutating the one successor draft because no review targets it.
        targeting_reviews = self._context_reviews_for_version(
            session=session,
            page=page,
            version=current,
        )
        if own_open_edit:
            targeting_reviews = self._settle_autosave_reviews_of_open_edit(
                session=session,
                page=page,
                version=current,
                reviews=targeting_reviews,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
        replaced_reviews = [
            review
            for review in targeting_reviews
            if review.status in {"pending", "changes_requested"}
        ]
        created_session_draft = not same_actor_draft or bool(targeting_reviews)
        if created_session_draft:
            version = self._insert_version(
                session,
                page_id=page.id,
                parent_version_id=current.id,
                body=prepared,
                actor_kind=actor_kind,
                actor_id=actor_id,
                version_status="draft",
            )
        else:
            version = current
            version.editing_revision += 1
        session_operation = (
            None
            if created_session_draft
            else self._autosave_session_operation(
                session, page, version, actor_kind, actor_id
            )
        )
        # An adopted draft (a restore, an accepted proposal, this session
        # after a reload) has no ledger row of ours until the session
        # actually stages something. Opening one then keeps the budget at
        # one change set per session while leaving the other change set's
        # record of its own work untouched.
        if session_operation is None:
            change_set = ChangeSet(
                workspace_id=page.workspace_id,
                reason="page.autosave.draft",
                trigger_type="manual_edit",
                actor_type=actor_kind,
                actor_id=parse_uuid_or_none(actor_id),
                status="applied",
                risk_level="low",
            )
            session.add(change_set)
            session.flush()
            operation = ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="context_page",
                target_id=page.id,
                operation="update",
                diff_summary="page.autosave.draft",
                diff_ref=f"context_page_version:{version.id}",
            )
            session.add(operation)
            self.change_audit_recorder.audit_service.emit(
                session=session,
                workspace_id=page.workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="page.autosave.draft",
                target_kind="context_page",
                target_id=page.id,
                payload={
                    "before_content_hash": current.content_hash,
                    "after_content_hash": version.content_hash,
                    "parent_version_id": str(current.id),
                    "session_version_id": (
                        str(session_version_id)
                        if session_version_id is not None
                        else None
                    ),
                },
            )
            session.flush()
            if version.change_set_id is None:
                version.change_set_id = change_set.id
            session.add(version)
            session_operation = operation
            opened_session_ledger = True
        else:
            opened_session_ledger = False

        self._rewrite_body_in_place(version, prepared)
        version.actor_type = actor_kind
        version.actor_id = parse_uuid_or_none(actor_id)
        version.actor_display_name = resolve_actor_display_name(
            session,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        version.created_by_actor_kind = actor_kind
        version.created_by_actor_id = actor_id
        session.add(version)
        if not created_session_draft:
            self._sync_version_usage(
                session,
                page=page,
                version=version,
                body_md=body_md,
            )
        page.updated_at = utc_now()
        self._set_current_version(session, page, version, next_title)
        self._replace_autosave_metadata_patch(
            session=session,
            patch=staged_patch,
            operation=session_operation,
        )
        superseded_review_count = self._supersede_content_replaced_reviews(
            session=session,
            page=page,
            replaced_version=current,
            successor_version=version,
            reviews=replaced_reviews,
            new_change_set_id=session_operation.change_set_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        refresh_page_links(session, page=page, version=version)
        return self._payload_from_page(
            session,
            page,
            version,
            change_set_id=(
                session_operation.change_set_id if opened_session_ledger else None
            ),
            superseded_review_count=superseded_review_count,
        )

    @staticmethod
    def _autosave_receipt(
        *,
        result: PagePayload,
        receipt_base: dict[str, object],
        with_edit_receipt: bool,
    ) -> PagePayload:
        if with_edit_receipt and receipt_base:
            result._edit_receipt = build_edit_receipt(
                edit_mode="whole_body",
                before_body=str(receipt_base["body"]),
                after_body=result.body_md,
                editing_revision_before=int(receipt_base["revision"]),
                editing_revision_after=result.editing_revision,
            )
        return result

    def _autosave_persist_reviewed_draft(
        self,
        *,
        session: Session,
        page: ContextPage,
        current: ContextPageVersion,
        prepared: PreparedBody,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        session_version_id: UUID | None,
        metadata_patch: dict[str, object],
    ) -> PagePayload:
        """Stage an AI autosave without mutating any published projection."""
        body_md = prepared.body

        targeting_reviews = self._context_reviews_for_version(
            session=session,
            page=page,
            version=current,
        )
        unresolved_reviews = [
            review
            for review in targeting_reviews
            if review.status in {"pending", "changes_requested"}
        ]
        same_actor_unreviewed_draft = (
            current.version_status in ("editing", "draft")
            and current.graph_status == "linear"
            and current.created_by_actor_kind == actor_kind
            and current.created_by_actor_id == actor_id
            and not targeting_reviews
        )
        before_hash = current.content_hash
        if same_actor_unreviewed_draft:
            version = current
            self._rewrite_body_in_place(version, prepared)
            version.actor_type = actor_kind
            version.actor_id = parse_uuid_or_none(actor_id)
            version.actor_display_name = resolve_actor_display_name(
                session,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
            version.editing_revision += 1
            session.add(version)
            self._sync_version_usage(
                session,
                page=page,
                version=version,
                body_md=body_md,
            )
            action = "page.autosave.overwrite"
        else:
            version = self._insert_version(
                session,
                page_id=page.id,
                parent_version_id=current.id,
                body=prepared,
                actor_kind=actor_kind,
                actor_id=actor_id,
                version_status="draft",
            )
            action = "page.autosave.draft"

        self._set_current_version(session, page, version)
        result = self.change_audit_recorder.record_context_page_write(
            session=session,
            workspace_id=page.workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action=action,
            page_id=page.id,
            version_id=version.id,
            payload={
                "before_content_hash": before_hash,
                "after_content_hash": version.content_hash,
                "parent_version_id": (
                    str(version.parent_version_id)
                    if version.parent_version_id is not None
                    else None
                ),
                "session_version_id": (
                    str(session_version_id) if session_version_id is not None else None
                ),
                "staged_metadata": bool(metadata_patch),
            },
            actor_provenance=actor_provenance,
            body=body_md,
        )
        stage_page_metadata_patch(
            session,
            change_set_id=result.change_set_id,
            page=page,
            version=version,
            patch=metadata_patch,
        )
        refresh_page_links(session, page=page, version=version)
        for unresolved_review in unresolved_reviews:
            supersede_review_with_lineage(
                session=session,
                audit_service=self.change_audit_recorder.audit_service,
                workspace_id=page.workspace_id,
                review=unresolved_review,
                new_change_set_id=result.change_set_id,
                new_review_item_id=result.review_item_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                payload={
                    "replaced_version_id": str(current.id),
                    "successor_version_id": str(version.id),
                },
            )
        superseded_review_count = len(unresolved_reviews)
        return self._payload_from_page(
            session,
            page,
            version,
            change_set_id=result.change_set_id,
            review_item_id=result.review_item_id,
            superseded_review_count=superseded_review_count,
        )

    @staticmethod
    def _autosave_session_operation(
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
        actor_kind: str,
        actor_id: str,
    ) -> ChangeOperation | None:
        """The ledger row THIS editing session owns for ``version``, if any.

        A session rewrites its own row as the author keeps typing, which is how
        a whole session costs one change set. It must find that row by
        ownership, never by "the newest row on this version": an editing
        session can adopt a draft somebody else's change set created -- a
        restore, an accepted proposal, an agent's staged draft -- and rewriting
        that row would erase the record of what that change set did.

        Reason and actor alone do not establish ownership. ``reason`` is
        caller-supplied text on the MCP write lanes, so an agent can send
        `page.autosave.draft` verbatim, and the AI staging path records that
        same reason by design -- while a bearer token resolves to the very same
        actor as the person's browser. What no browser session ever produces is
        a ReviewItem: this ledger row is written directly precisely so a
        keystroke does not ask anyone for a decision. So a change set carrying a
        review belongs to a write that wanted one, and is not ours to reuse.
        """

        return session.scalars(
            select(ChangeOperation)
            .join(ChangeSet, ChangeSet.id == ChangeOperation.change_set_id)
            .where(
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page.id,
                ChangeOperation.diff_ref == f"context_page_version:{version.id}",
                ChangeSet.reason == "page.autosave.draft",
                ChangeSet.trigger_type == "manual_edit",
                ChangeSet.actor_type == actor_kind,
                ChangeSet.actor_id == parse_uuid_or_none(actor_id),
                ~select(ReviewItem.id)
                .where(ReviewItem.change_set_id == ChangeSet.id)
                .exists(),
            )
            .order_by(ChangeOperation.created_at.desc(), ChangeOperation.id.desc())
        ).first()

    @staticmethod
    def _replace_autosave_metadata_patch(
        *,
        session: Session,
        patch: dict[str, object],
        operation: ChangeOperation,
    ) -> None:
        """Keep staged browser metadata in the session's own ledger row."""

        if patch:
            operation.operation = "metadata_patch"
            operation.diff_summary = json.dumps(
                patch,
                ensure_ascii=False,
                sort_keys=True,
            )
        else:
            operation.operation = "update"
            operation.diff_summary = "page.autosave.draft"
        session.add(operation)

    def _require_alternative_base(
        self,
        *,
        session: Session,
        page: ContextPage,
        current: ContextPageVersion,
        actor_kind: str,
        actor_id: str,
        base_version_id: UUID | None,
        base_content_hash: str | None,
    ) -> ContextPageVersion:
        base_version = self._require_page_version(session, page, base_version_id)
        # A base that is the current head itself means the caller's own tab
        # overwrote the draft row in place (page.updated_at, current session
        # write model). That is not an unrelated conflict -- it is exactly the
        # case "preserve as alternative" exists to rescue -- so branch off
        # head instead of refusing. A base naming any other version is still a
        # real conflict and stays refused.
        if (
            base_version.content_hash != base_content_hash
            and base_version.id != current.id
        ):
            disclosed_version_id, disclosed_content_hash = (
                self._disclosable_version_fields(current, actor_kind, actor_id)
            )
            raise PageConflictError(
                server_version_id=disclosed_version_id,
                server_content_hash=disclosed_content_hash,
            )
        return base_version

    def _preserve_alternative(
        self,
        session: Session,
        page: ContextPage,
        current: ContextPageVersion,
        base_version: ContextPageVersion,
        title: str | None,
        prepared: PreparedBody,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
    ) -> PagePayload:
        version = self._insert_version(
            session,
            page_id=page.id,
            parent_version_id=base_version.id,
            body=prepared,
            actor_kind=actor_kind,
            actor_id=actor_id,
            version_status="draft",
            graph_status="branched",
        )
        active_alternatives = session.scalars(
            select(ContextPageVersion)
            .where(
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.graph_status == "branched",
                ContextPageVersion.id != version.id,
                # Durable MCP reconciliation branches are correctness state,
                # not browser alternative overflow. They may terminate only by
                # patch_draft(reconciles=...) or explicit discard, never because
                # another stale browser save happened to cross this UI cap.
                ContextPageVersion.reconcile_manifest.is_(None),
                ~select(ChangeSet.id)
                .where(
                    ChangeSet.id == ContextPageVersion.change_set_id,
                    ChangeSet.workspace_id == page.workspace_id,
                    ChangeSet.trigger_type == "mcp_patch_draft_diverged",
                )
                .exists(),
                ~select(ReviewItem.id)
                .where(
                    ReviewItem.change_set_id == ContextPageVersion.change_set_id,
                    ReviewItem.workspace_id == page.workspace_id,
                    ReviewItem.review_type == "context_reconciliation",
                )
                .exists(),
            )
            .order_by(ContextPageVersion.version_no.desc())
        ).all()
        for overflow in active_alternatives[MAX_ACTIVE_ALTERNATIVE_DRAFTS - 1 :]:
            overflow.graph_status = "discarded"
            session.add(overflow)
        result = self.change_audit_recorder.record_context_page_alternative(
            session=session,
            workspace_id=page.workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            page_id=page.id,
            version_id=version.id,
            payload={
                "base_version_id": str(base_version.id),
                "server_version_id": str(current.id),
                "server_content_hash": current.content_hash,
                "proposed_title": title,
                "proposed_page_type": prepared.metadata.page_type,
                "proposed_tags": prepared.metadata.tags,
                "proposed_aliases": prepared.metadata.aliases,
            },
            actor_provenance=actor_provenance,
        )
        # `current` is the page-global head at conflict time -- it must not
        # name a DIFFERENT author's private `editing` row even as a bare
        # pointer (P2.2/P2.6: no opaque-id disclosure either), and that
        # applies to BOTH `visible_version_id` and `_payload_from_page`'s own
        # `current_version_id` field (which otherwise defaults to the raw
        # page-global pointer -- safe only for a true self-view response).
        safe_current_id = _hidden_editing_id_replaced_with_published(
            page, current.id, actor_kind, actor_id
        )
        return self._payload_from_page(
            session,
            page,
            version,
            alternative_draft_id=version.id,
            change_set_id=result.change_set_id,
            review_item_id=result.review_item_id,
            preserved_as_alternative=True,
            visible_version_id=safe_current_id,
            current_version_id_override=safe_current_id,
        )

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
                audit_service=self.change_audit_recorder.audit_service,
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

    def _settle_autosave_reviews_of_open_edit(
        self,
        *,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
        reviews: list[ReviewItem],
        actor_kind: str,
        actor_id: str,
    ) -> list[ReviewItem]:
        """Retire review items that earlier AI autosaves attached to an open edit.

        Before own-editing-row AI saves took the session path, each one
        recorded a ReviewItem against the row it was still editing. Such an
        item never described a submitted state: the row stayed private and
        kept changing. Left in place they made every later save fork a draft
        and left a phantom entry in the review queue. They are settled here,
        audited; the real review is the one submit_context_edit_for_review
        records.
        """

        remaining: list[ReviewItem] = []
        settled = False
        for review in reviews:
            change_set = get_in_workspace(
                session,
                ChangeSet,
                workspace_id=page.workspace_id,
                resource_id=review.change_set_id,
            )
            from_autosave = change_set is not None and (
                change_set.reason or ""
            ).startswith("page.autosave")
            if not from_autosave:
                remaining.append(review)
                continue
            if review.status in {"pending", "changes_requested"}:
                review.status = "superseded"
                review.claimed_by = None
                review.claimed_at = None
                change_set.status = "superseded"
                self.change_audit_recorder.audit_service.emit(
                    session=session,
                    workspace_id=page.workspace_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    action="review.superseded_autosave_of_open_edit",
                    target_kind="review_item",
                    target_id=review.id,
                    payload={"version_id": str(version.id)},
                )
                settled = True
        if settled:
            session.flush()
        return remaining

    def _supersede_content_replaced_reviews(
        self,
        *,
        session: Session,
        page: ContextPage,
        replaced_version: ContextPageVersion,
        successor_version: ContextPageVersion,
        reviews: list[ReviewItem],
        new_change_set_id: UUID,
        actor_kind: str,
        actor_id: str,
    ) -> int:
        """Settle only reviews whose immutable target the browser replaced."""

        if successor_version.id == replaced_version.id:
            if reviews:
                raise RuntimeError("reviewed Context bytes must not be overwritten")
            return 0
        for review in reviews:
            review.status = "superseded"
            review.claimed_by = None
            review.claimed_at = None
            stale_change_set = session.get(ChangeSet, review.change_set_id)
            if stale_change_set is not None:
                stale_change_set.status = "superseded"
            self.change_audit_recorder.audit_service.emit(
                session=session,
                workspace_id=page.workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="review.superseded_by_content_replacement",
                target_kind="review_item",
                target_id=review.id,
                payload={
                    "original_review_item_id": str(review.id),
                    "replaced_version_id": str(replaced_version.id),
                    "successor_version_id": str(successor_version.id),
                    "new_change_set_id": str(new_change_set_id),
                },
            )
        session.flush()
        return len(reviews)

    @staticmethod
    def _set_current_version(
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
        title: str | None = None,
        *,
        publish: bool | None = None,
    ) -> ContextPage:
        """Point ``current_version_id`` at ``version`` (optionally retitling).

        Only a non-draft snapshot may move the published head: an unpublished
        draft (e.g. a staged restore) must never be promoted to what
        read_public_page serves by a metadata/title-only save or an in-place
        draft autosave — publishing a draft is an explicit step.
        """
        should_publish = (
            version.version_status not in ("editing", "draft")
            if publish is None
            else publish
        )
        if not should_publish:
            # P8.1: an "editing" row stays "editing" through any number of
            # autosaves -- it is the author-private working state, and
            # `backend/repositories/context_visibility.py` only hides a row
            # from non-authors while it is exactly "editing". Only an
            # explicit submit (`submit_context_edit_for_review`) may flip it
            # to "draft"; this pointer-update helper must never do so as a
            # side effect of an ordinary save, or the privacy window closes
            # itself on the very first autosave.
            page.current_version_id = version.id
            page.latest_draft_version_id = version.id
            if page.lifecycle_status != "archived":
                page.lifecycle_status = (
                    "published" if page.current_published_version_id else "draft"
                )
            # P8.4: `page.current_published_version_id is None` ("this page
            # has never published") is NOT the same thing as "nothing else
            # depends on page.title staying put" -- a never-published page
            # can still carry an already-submitted draft D awaiting review
            # while THIS save is a separate, still-private "editing"/"draft"
            # row. Writing straight to `page.title` here bypassed the exact
            # staging `should_stage_page_metadata` exists to enforce (the
            # same bug class its own docstring documents), just through a
            # second, redundant title-write path autosave_page also calls
            # into. Gate on the version's own staging need instead.
            if title is not None and not should_stage_page_metadata(page, version):
                page.title = title
            session.add(page)
            session.flush()
            return page
        try:
            apply_context_publication(
                session,
                page=page,
                version=version,
                require_latest_draft=False,
            )
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
                "Call guide and choose a node_id from placement_parent_candidates; "
                "the page write was rolled back.",
                cause_error_code=exc.error_code,
            ) from exc

    def _move_context_tree_placement(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        page: ContextPage,
        destination_node_id: UUID | None,
        actor_kind: str,
        actor_id: str,
    ) -> bool:
        if destination_node_id is None:
            return False
        previous_parent_id = page.placement_parent_node_id
        self._ensure_context_tree_placement(
            session=session,
            workspace_id=workspace_id,
            page=page,
            parent_id=destination_node_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            move_existing=True,
        )
        if previous_parent_id == destination_node_id:
            return False
        page.placement_parent_node_id = destination_node_id
        session.add(page)
        return True

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

    def _editable_version_id_for_actor(
        self,
        session: Session,
        page: ContextPage,
        actor_kind: str,
        actor_id: str,
    ) -> UUID | None:
        """The version an ordinary save by THIS actor should land on (P8.1/P8.4).

        ``editable_context_version_id(page)`` is a page-global pointer -- it
        cannot tell two authors' own open "editing" rows apart, so it would
        collide them onto the same version, exactly the ambiguity P2.2 was
        meant to rule out. Prefer this actor's own open "editing" row when
        one exists; otherwise fall back to the page-global pointer unchanged
        (ordinary single-author save, or a submitted draft this actor is
        continuing to amend before review).
        """

        own_editing_id = session.scalar(
            select(ContextPageVersion.id).where(
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.version_status == "editing",
                ContextPageVersion.created_by_actor_kind == actor_kind,
                ContextPageVersion.created_by_actor_id == actor_id,
            )
        )
        return own_editing_id or editable_context_version_id(page)

    @staticmethod
    def _highest_editing_revision(
        session: Session,
        page: ContextPage,
        actor_kind: str,
        actor_id: str,
        *,
        excluding: UUID,
    ) -> int | None:
        """The highest editing_revision any other row of this author on this
        page ever reached -- discarded, submitted and published rows included.

        editing_revision alone guards update_context_edit when no base is
        sent. Restarting every new editing row at 1 let a revision read from
        an EARLIER row of the same author match the new one, so a stale
        whole-body save replaced work the caller had never seen. A new row
        starts above this value instead. Per author, so the starting value
        says nothing about anyone else's private edits.
        """

        return session.scalar(
            select(func.max(ContextPageVersion.editing_revision)).where(
                ContextPageVersion.workspace_id == page.workspace_id,
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.created_by_actor_kind == actor_kind,
                ContextPageVersion.created_by_actor_id == actor_id,
                ContextPageVersion.id != excluding,
            )
        )

    @staticmethod
    def _require_named_open_edit(
        session: Session,
        page: ContextPage,
        version_id: UUID,
        actor_kind: str,
        actor_id: str,
    ) -> None:
        """Refuse a save that names an editing row other than the open one.

        Only this actor's own open row is ever named back (its id and
        revision, as every own-edit conflict does), so a caller learns
        nothing about anyone else's work.
        """

        open_edit = context_page_repo.get_open_edit_for_actor(
            session,
            workspace_id=page.workspace_id,
            page_id=page.id,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if open_edit is None:
            raise PageConflictError(
                server_version_id=None,
                server_content_hash=None,
                no_open_edit=True,
            )
        if open_edit.id != version_id:
            raise PageConflictError(
                server_version_id=open_edit.id,
                server_content_hash=open_edit.content_hash,
                server_editing_revision=open_edit.editing_revision,
            )

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

    def _disclosable_version_fields(
        self,
        current: ContextPageVersion,
        actor_kind: str | None,
        actor_id: str | None,
    ) -> tuple[UUID | None, str | None]:
        """P2.2/P2.6: a conflict response must never name another author's
        private "editing" row's id/content_hash -- that would itself be the
        disclosure those sections exist to prevent. Any caller that raises a
        ``PageConflictError`` naming ``current`` (page-global pointers can
        durably resolve to another author's open editing row, see
        ``editable_context_version_id``) must route the disclosed fields
        through this check first, not just ``_validate_base``'s own callers.
        """

        if current.version_status == "editing" and (
            current.created_by_actor_kind != actor_kind
            or current.created_by_actor_id != actor_id
        ):
            return None, None
        return current.id, current.content_hash

    def _validate_base(
        self,
        session: Session,
        page: ContextPage,
        current: ContextPageVersion,
        base_version_id: UUID | None,
        base_content_hash: str | None,
        expected_editing_revision: int | None = None,
        *,
        actor_kind: str | None = None,
        actor_id: str | None = None,
    ) -> None:
        if base_version_id is None and base_content_hash is None:
            own_open_edit = (
                current.version_status == "editing"
                and current.created_by_actor_kind == actor_kind
                and current.created_by_actor_id == actor_id
            )
            # The author's own editing row: editing_revision moves on every
            # save of it, and a new row starts above every earlier row's
            # revision (begin_context_edit), so it alone proves the caller saw
            # the latest state of THIS row.
            if own_open_edit and expected_editing_revision is not None:
                if expected_editing_revision != current.editing_revision:
                    raise PageConflictError(
                        server_version_id=current.id,
                        server_content_hash=current.content_hash,
                        server_editing_revision=current.editing_revision,
                    )
                return
            raise PageValidationError(
                "base_version_id and base_content_hash are required"
            )
        if base_version_id is None or base_content_hash is None:
            raise PageValidationError(
                "base_version_id and base_content_hash are required"
            )
        base_version = context_page_repo.get_version(session, base_version_id)
        if base_version is None or base_version.page_id != page.id:
            raise PageNotFoundError("page version not found")
        # `current` can be `page.latest_draft_version_id`/`current_version_id`,
        # a page-global pointer that -- since P8.1 -- can durably name another
        # author's private "editing" row (see `editable_context_version_id`).
        # A conflict response naming that row's id/content_hash to a caller
        # who is not its author would itself be the disclosure P2.2/P2.6
        # exists to prevent, even though the write these callers perform is
        # not itself actor-scoped (their own doc comments explain why not).
        disclosed_version_id, disclosed_content_hash = self._disclosable_version_fields(
            current, actor_kind, actor_id
        )
        disclosed_revision = (
            current.editing_revision
            if disclosed_version_id is not None
            and current.version_status == "editing"
            and current.created_by_actor_kind == actor_kind
            and current.created_by_actor_id == actor_id
            else None
        )
        if base_version_id != current.id or base_content_hash != current.content_hash:
            raise PageConflictError(
                server_version_id=disclosed_version_id,
                server_content_hash=disclosed_content_hash,
                server_editing_revision=disclosed_revision,
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
                server_version_id=disclosed_version_id,
                server_content_hash=disclosed_content_hash,
                server_editing_revision=disclosed_revision,
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
            current_version_id=_editable_head_for_actor(page, can_read_drafts, actor),
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
    ) -> PagePayload:
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
        # P8.4: project the parent whenever we have an actor to project it
        # for -- not just when `can_read_drafts` (a coarse role gate) is
        # False. `can_read_drafts=True` only means "may see some draft
        # content"; it says nothing about THIS actor being the author of a
        # private "editing" parent, which is an identity check
        # `context_parent_projection` -> `context_version_is_visible` makes
        # on its own. Skipping projection whenever a Viewer/Editor could
        # read drafts leaked another author's hidden editing-row id as this
        # version's raw `parent_version_id`.
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
        return PagePayload(
            id=page.id,
            workspace_id=page.workspace_id,
            slug=page.slug,
            title=title,
            page_type=page_type,
            tags=tags,
            aliases=aliases,
            visible_version_id=projected_visible_id,
            current_version_id=(
                _editable_head_for_actor(page, can_read_drafts, parent_visibility_actor)
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
            version_id=version.id,
            editing_revision=version.editing_revision,
            parent_version_id=parent_version_id,
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
        )


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


def _hidden_editing_id_replaced_with_published(
    page: ContextPage, candidate: UUID | None, actor_kind: str, actor_id: str
) -> UUID | None:
    """``candidate``, unless it might name someone else's private `editing` row.

    Shared by call sites that only have the raw ``actor_kind``/``actor_id``
    strings this codebase's write methods take (not a full ``ActorContext``)
    but still must not hand back a DIFFERENT author's private editing head,
    even as a bare id -- P2.2/P2.6, same rule as `_editable_head_for_actor`
    and `editing_row_hidden_from_actor`.

    There is no session here to look up an arbitrary ``candidate`` id's own
    row, so this can only PROVE safety for ids it can inspect without one:
    the published id (trivially safe), `latest_draft_version_id`, and
    `current_version_id` (both inspectable via their loaded relationships --
    a page that has never published can have `current_version_id` name an
    `editing` row directly, with `latest_draft_version_id` staying `None`
    the whole time, so both must be checked independently rather than
    assuming they always name the same row). A `candidate` naming neither
    pointer is not provably safe -- replaced rather than assumed so.
    """

    if candidate is None or candidate == page.current_published_version_id:
        return candidate

    def _is_hidden(editing: ContextPageVersion | None) -> bool:
        return (
            editing is not None
            and editing.version_status == "editing"
            and (
                editing.created_by_actor_kind != actor_kind
                or editing.created_by_actor_id != actor_id
            )
        )

    if candidate == page.latest_draft_version_id:
        return (
            page.current_published_version_id
            if _is_hidden(page.latest_draft_version)
            else candidate
        )
    if candidate == page.current_version_id:
        return (
            page.current_published_version_id
            if _is_hidden(page.current_version)
            else candidate
        )
    return page.current_published_version_id


def _editable_head_for_actor(
    page: ContextPage,
    can_read_drafts: bool,
    actor: ActorContext | None = None,
) -> UUID | None:
    """The head this actor may edit -- and may know about.

    ``current_version_id`` follows the draft once a session stages one, so
    handing it to everyone answers "does an unpublished draft exist?" for an
    actor who is not allowed to ask: the id differs from the published pointer
    exactly when a draft is open. An actor who cannot see drafts is told about
    the published head, which is the newest version that actually exists for
    them.

    ``actor`` is only given on a genuine cross-actor read (``list_pages``,
    ``read_page``'s viewer). Write-response payloads leave it ``None``: the
    version they return is what that same actor just wrote, so there is
    nothing to hide from them in their own response. When given, a
    page-global ``current_version_id`` that is actually someone else's
    private ``editing`` row -- or any other id this function cannot verify,
    e.g. after it has diverged from `latest_draft_version_id` -- is not
    handed out; see `_hidden_editing_id_replaced_with_published`'s docstring
    for why an unverifiable candidate is replaced too, not assumed safe.
    P2.2/P2.6.
    """

    if not can_read_drafts:
        return page.current_published_version_id
    if actor is None:
        return page.current_version_id
    return _hidden_editing_id_replaced_with_published(
        page, page.current_version_id, actor.actor_kind, actor.actor_id
    )


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
    session: Session,
    page: ContextPage,
    version: ContextPageVersion,
) -> tuple[str, str, list[str], list[str]]:
    """Overlay a draft's staged metadata without mutating canonical fields."""

    page_type, tags, aliases = metadata_for_payload(page, version)
    title = page.title
    if should_stage_page_metadata(page, version):
        patch = pending_page_metadata_patch(
            session,
            page=page,
            version_id=version.id,
        )
        patched_title = patch.get("title")
        if isinstance(patched_title, str):
            title = patched_title
        patched_page_type = patch.get("page_type")
        if isinstance(patched_page_type, str) and patched_page_type in PAGE_TYPES:
            page_type = patched_page_type
        patched_tags = patch.get("tags")
        if isinstance(patched_tags, list):
            tags = normalize_page_tags([str(tag) for tag in patched_tags])
        patched_aliases = patch.get("aliases")
        if isinstance(patched_aliases, list):
            aliases = normalize_page_aliases([str(alias) for alias in patched_aliases])
    return title, page_type, tags, aliases


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
        }

    before = (
        snapshot(from_version)
        if from_version is not None
        else {
            "title": None,
            "tags": (),
            "aliases": (),
            "page_type": None,
            "placement": None,
        }
    )
    after = snapshot(to_version)
    return [
        key
        for key in ("title", "tags", "aliases", "page_type", "placement")
        if before[key] != after[key]
    ]


def sync_page_projections(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion | None = None,
) -> None:
    """Synchronize write-through projections for a Context page.

    PageLink rows are version-scoped so draft and published link graphs can
    coexist. page_name_index is a rebuildable projection of the currently live
    published/draft heads.
    """

    _sync_page_name_index(session, page=page)
    if version is not None:
        # Re-derive so ``dtxwiki reindex`` doubles as the NULL backfill path
        # for legacy rows (nav_description is rebuildable by contract).
        version.nav_description = derive_nav_description(version.body_md)
        _sync_page_links_for_version(session, page=page, version=version)
    _sync_page_doc_source(session, page=page)
    _delete_stale_page_link_versions(session, page=page)

    # G1 write-through: keep the page's curated edge projection (wikilink/
    # citation, published-head grain) fresh with the link projection — the
    # neighbor block must not wait for a reindex.
    from backend.services.indexing.edge_registry import (
        refresh_page_edge_projection,
    )

    refresh_page_edge_projection(session, page.workspace_id, page.id)


def refresh_page_links(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
) -> None:
    """Compatibility wrapper for older call sites."""

    sync_page_projections(session, page=page, version=version)


def _sync_page_links_for_version(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
) -> None:
    # The table is pruned to the page's live pointers after every sync, so all
    # rows present here belonged to a live version immediately before this
    # write. Prefer the newest surviving pin for each target slug.
    preserved_pins: dict[str, UUID] = {}
    pin_rows = session.execute(
        select(PageLink.target_slug, PageLink.target_page_id)
        .join(
            ContextPageVersion,
            ContextPageVersion.id == PageLink.source_version_id,
        )
        .where(
            PageLink.source_page_id == page.id,
            PageLink.target_page_id.is_not(None),
        )
        .order_by(ContextPageVersion.version_no.desc())
    ).all()
    for target_slug, target_page_id in pin_rows:
        preserved_pins.setdefault(target_slug, target_page_id)
    session.execute(
        delete(PageLink).where(
            PageLink.source_page_id == page.id,
            PageLink.source_version_id == version.id,
        )
    )
    body = parse_page_frontmatter(version.body_md).body
    # Strip fenced code blocks and inline `code` so a documentation page that
    # demonstrates wikilink syntax (`[[other]]`, or a ```md fence) does not
    # create phantom backlink rows. Offsets are preserved (whitespace-filled)
    # so snippet_around_match still produces accurate previews. P-H1.
    scannable = _strip_code_regions(body)
    matches_by_target: dict[str, list[re.Match[str]]] = {}
    for match in WIKILINK_PATTERN.finditer(scannable):
        target_slug = slugify_wikilink_target(match.group("target"))
        if not target_slug:
            continue
        matches_by_target.setdefault(target_slug, []).append(match)

    target_page_ids = _target_page_ids_by_slug(
        session,
        workspace_id=page.workspace_id,
        target_slugs=matches_by_target.keys(),
    )
    for target_slug, matches in matches_by_target.items():
        session.add(
            PageLink(
                workspace_id=page.workspace_id,
                source_page_id=page.id,
                source_version_id=version.id,
                target_slug=target_slug,
                target_page_id=preserved_pins.get(
                    target_slug, target_page_ids.get(target_slug)
                ),
                occurrence_count=len(matches),
                first_snippet=snippet_around_match(body, matches[0]),
            )
        )


def _delete_stale_page_link_versions(session: Session, *, page: ContextPage) -> None:
    live_version_ids = {
        version_id
        for version_id in (
            page.current_published_version_id,
            page.latest_draft_version_id,
            page.current_version_id,
        )
        if version_id is not None
    }
    statement = delete(PageLink).where(PageLink.source_page_id == page.id)
    if live_version_ids:
        statement = statement.where(PageLink.source_version_id.notin_(live_version_ids))
    session.execute(statement)


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
        metadata_patch = (
            pending_page_metadata_patch(session, page=page, version_id=version.id)
            if scope == "draft"
            else None
        )
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
    """Which (version_id, "published"/"draft") pairs the name index reflects.

    The index has no per-author scope -- search_service.py grants "draft"
    visibility_scope rows to any actor with the draft-read role, full stop.
    "current_version_id != current_published_version_id" used to be a safe
    stand-in for "this is a submitted draft, correctly role-visible" because
    nothing else could occupy that gap. Page-first creation can put a private
    "editing" row directly behind `current_version_id` (with
    `latest_draft_version_id` staying `None` the whole time), so the
    id-comparison alone would misclassify a still-private autosave as "draft"
    -- confirmed by reproducing it: an autosave indexed both the placeholder
    slug and an in-progress title under visibility_scope="draft" before this
    fixed it. Checking the
    resolved version's own `version_status` closes it: an "editing" row is
    excluded from the "draft" candidate regardless of which of the two
    pointers (`latest_draft_version_id` or, absent that, `current_version_id`)
    currently names it. The "published" branch below is unconditional and
    separate -- `current_published_version_id` is only ever set at actual
    publish time, so it cannot itself name an "editing" row.

    Resolved via ``session.get()`` on the raw id, not the ``latest_draft_
    version``/``current_version`` relationship attributes: several callers
    (autosave_page, create_page_first) set the ``*_version_id``
    columns directly on an already-loaded ``page``, and a relationship
    already accessed earlier in the same session does not invalidate just
    because the underlying FK column was reassigned out from under it --
    confirmed by reproducing it, a stale cached ``None`` from an earlier
    access silently dropped the draft row in exactly that pattern.
    """

    versions: list[tuple[UUID, str]] = []
    if page.current_published_version_id is not None:
        versions.append((page.current_published_version_id, "published"))
    draft_id = page.latest_draft_version_id
    if (
        draft_id is None
        and page.current_version_id != page.current_published_version_id
    ):
        draft_id = page.current_version_id
    if draft_id is not None and draft_id != page.current_published_version_id:
        draft = session.get(ContextPageVersion, draft_id)
        if draft is not None and draft.version_status == "draft":
            versions.append((draft.id, "draft"))
    return versions


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
        (page.title, "title"),
        *[(alias, "alias") for alias in page.aliases or []],
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
    """Bind a metadata delta to its version for staging and historical reads."""

    if not patch:
        return
    session.add(
        ChangeOperation(
            workspace_id=page.workspace_id,
            change_set_id=change_set_id,
            target_type="context_page",
            target_id=page.id,
            operation="metadata_patch",
            diff_summary=json.dumps(patch, ensure_ascii=False, sort_keys=True),
            diff_ref=f"context_page_version:{version.id}",
        )
    )


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
    # version, so applying them immediately during a private `editing`
    # session would leak that author's unpublished metadata change to public
    # readers (who still see the old body via current_published_version_id)
    # the moment it's saved -- not just to a reviewer, which is why this
    # cannot stay "draft"-only now that "editing" is unreviewed and private.
    #
    # This used to also require `page.current_published_version_id is not
    # None`, on the assumption that a page which has never published has
    # nothing to protect yet. A real Codex review found that reachable and
    # wrong: a never-published page can still carry an already-SUBMITTED
    # draft D awaiting review while a second editing session E is opened on
    # the same page. `should_stage_page_metadata(page, D)` used the same
    # page-global condition, so it would ALSO skip overlaying D's own staged
    # patch -- E's private, unsubmitted title then reads back as D's, to any
    # draft-reader, through the shared canonical columns both versions read.
    # Confirmed by reproducing it end to end (submit D, begin+autosave E,
    # read D as a different actor) before this fix, and that the same
    # contaminated title also lands in D's own PageNameIndex entry.
    # Publication history is irrelevant to whether staging is needed --
    # only this version's own status is.
    return version.version_status in ("editing", "draft")


def _target_page_ids_by_slug(
    session: Session,
    *,
    workspace_id: UUID,
    target_slugs: Iterable[str],
) -> dict[str, UUID]:
    slugs = tuple(target_slugs)
    if not slugs:
        return {}
    rows = session.execute(
        select(ContextPage.slug, ContextPage.id)
        .where(ContextPage.workspace_id == workspace_id)
        .where(ContextPage.slug.in_(slugs))
    )
    return {slug: page_id for slug, page_id in rows}


def slugify_wikilink_target(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value.strip()).lower()
    slug = re.sub(r"[\W_]+", "-", normalized).strip("-")
    return slug[:80]


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


def snippet_around_match(body_md: str, match: re.Match[str], radius: int = 90) -> str:
    start = max(0, match.start() - radius)
    end = min(len(body_md), match.end() + radius)
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
