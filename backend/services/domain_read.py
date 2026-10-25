import base64
import binascii
import json
import re
from collections.abc import Collection
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.orm import Session, aliased

from backend.library_media.ports import LibraryBlobRepositoryFactory
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
    PageLink,
)
from backend.models.feedback import FeedbackItem
from backend.models.library import (
    LibraryArtifact,
    LibraryAttempt,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
)
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.mcp_scopes import AGENT_PAGE_BODY_SCOPES, has_required_scope
from backend.policy.trust_boundaries import (
    actor_can_read_drafts,
    actor_can_read_sensitive_agent_pages,
)
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.read import (
    AgentCategoryEntry,
    AgentPageDetail,
    AgentPageListPage,
    AgentPageSummary,
    ChangeSetEntry,
    ContextPageDetail,
    ContextPageListPage,
    ContextPageSummary,
    LibraryArtifactPayload,
    LibraryConversionStatePayload,
    LibraryItemDetail,
    LibraryItemListPage,
    LibraryItemSummary,
    LibraryItemVersionPayload,
    ListChangesPayload,
    PendingReviewInfo,
    PublishedVersionInfo,
    ReviewDecisionEntry,
    VersionEntry,
    VersionInfo,
)
from backend.services.actor_visible_read import (
    PUBLISHED_CONTEXT_VERSION_STATUSES,
    VISIBLE_AGENT_SENSITIVITIES,
    actor_can_read_agent_page,
    actor_can_read_audit_records,
    actor_can_read_feedback,
    actor_can_read_library_trust_level,
    agent_page_visible,
    agent_version_is_visible,
    change_set_visible,
    context_page_visible,
    context_parent_projection,
    context_version_is_visible,
    library_item_visible,
)
from backend.services.context_lifecycle import project_context_version
from backend.services.context_review_disposition import (
    context_version_review_disposition,
    context_version_review_metadata,
)
from backend.services.context_visibility import (
    editing_row_hidden_from_actor,
    visible_context_version_expression,
    visible_context_version_id,
)
from backend.services.conversion_failure_policy import (
    is_retryable_conversion_failure,
)
from backend.services.library_canonical import (
    canonical_library_artifact_belongs_to_item,
    canonical_library_artifact_lookup,
    get_canonical_library_version,
    library_original_content_url,
    library_original_download_available,
    list_canonical_library_artifacts,
)
from backend.services.lint import LintRegistry, page_format_warnings_for_body
from backend.services.list_pagination import (
    _decode_keyset_cursor,
    _encode_keyset_cursor,
)
from backend.services.mcp_read_projection import record_served_read
from backend.services.okf import (
    EXCLUDED_LIBRARY_LIFECYCLE_STATUSES,
    context_concept_path,
    library_concept_path,
)
from backend.services.read_errors import (
    ReadNotFoundError,
    ReadValidationError,
    RejectedByPolicyError,
    ScopeDeniedError,
)
from backend.services.tree_read import TreeReadService
from backend.services.workspace_read import AGENT_PAGE_TAXONOMY
from backend.utils.actor_display import (
    resolve_actor_display_name,
    resolve_actor_display_names,
)

_RECONCILIATION_SUMMARY_SCAN_LIMIT = 20


def _encode_version_history_cursor(
    *,
    workspace_id: UUID,
    target_type: str,
    target_id: UUID,
    before_version_no: int,
) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "workspace_id": str(workspace_id),
            "target_type": target_type,
            "target_id": str(target_id),
            "before_version_no": before_version_no,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_version_history_cursor(
    cursor: str | None,
    *,
    workspace_id: UUID,
    target_type: str,
    target_id: UUID,
) -> int | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(f"{cursor}{padding}").decode("utf-8")
        )
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReadValidationError(
            "cursor must be an opaque version pagination token"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("workspace_id") != str(workspace_id)
        or payload.get("target_type") != target_type
        or payload.get("target_id") != str(target_id)
        or not isinstance(payload.get("before_version_no"), int)
        or payload["before_version_no"] < 1
    ):
        raise ReadValidationError("cursor must be an opaque version pagination token")
    return payload["before_version_no"]


_SAFE_EXTRACTION_SUMMARY = re.compile(
    r"extracted (?:0|[1-9][0-9]{0,17}) characters",
    flags=re.ASCII,
)


def _public_extraction_summary(version: LibraryItemVersion) -> str | None:
    """Expose only the bounded legacy success fact, never stored diagnostics."""

    summary = version.extraction_summary
    if version.processing_status != "completed" or summary is None:
        return None
    if _SAFE_EXTRACTION_SUMMARY.fullmatch(summary) is None:
        return None
    return summary


def _library_conversion_state_payload(
    version: LibraryItemVersion,
    *,
    attempt: LibraryAttempt | None,
) -> LibraryConversionStatePayload:
    status = version.processing_status
    reason: (
        Literal[
            "document_conversion_failed",
            "document_conversion_timed_out",
            "document_conversion_unavailable",
        ]
        | None
    ) = None
    if status == "failed":
        reason = "document_conversion_failed"
    elif status == "timed_out":
        reason = "document_conversion_timed_out"
    elif status == "stored_unconverted":
        reason = "document_conversion_unavailable"
    error_code = attempt.error_code if attempt is not None else None
    detail = attempt.conversion_detail if attempt is not None else None
    retryable = is_retryable_conversion_failure(
        status=status,
        error_code=error_code,
        manual_retry_disposition=(
            detail.manual_retry_disposition if detail is not None else None
        ),
        disposition_reason=(detail.disposition_reason if detail is not None else None),
    )
    return LibraryConversionStatePayload(
        status=status,
        reason=reason,
        retryable=retryable,
    )


class DomainReadService:
    def __init__(
        self,
        tree_read_service: TreeReadService | None = None,
        *,
        library_blob_repository_factory: LibraryBlobRepositoryFactory | None = None,
        lint_registry: LintRegistry | None = None,
    ) -> None:
        self.lint_registry = (
            lint_registry if lint_registry is not None else LintRegistry.builtin()
        )
        self.tree_read_service = tree_read_service or TreeReadService()
        # Doc §3-D: History's per-version download button needs a
        # server-authoritative "is the original still retrievable" answer,
        # not just a `source_blob_id` presence check -- optional so existing
        # callers that build this service without blob access keep working
        # (every LibraryItemVersionPayload then reports availability False).
        self._library_blob_repository_factory = library_blob_repository_factory

    def list_alternative_drafts(
        self,
        session: Session,
        page_id: UUID,
        *,
        actor: ActorContext | None,
        limit: int = 20,
    ) -> list[ContextPageVersion]:
        if not actor_can_read_drafts(actor):
            return []
        return list(
            session.scalars(
                select(ContextPageVersion)
                .where(
                    ContextPageVersion.page_id == page_id,
                    ContextPageVersion.graph_status == "branched",
                )
                .order_by(ContextPageVersion.version_no.desc())
                .limit(limit)
            )
        )

    def list_library_items(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        connector: str | None = None,
        trust: str | None = None,
        lifecycle: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        exclude_trust_levels: Collection[str] | None = None,
    ) -> LibraryItemListPage:
        base = select(LibraryItem).where(LibraryItem.workspace_id == workspace_id)
        if connector is not None:
            base = base.where(LibraryItem.connector_type == connector)
        if trust is not None:
            base = base.where(LibraryItem.trust_level == trust)
        if lifecycle is not None:
            base = base.where(LibraryItem.lifecycle_status == lifecycle)
        else:
            # The default listing hides archived/hidden items like every other
            # read lane; viewing them requires an explicit lifecycle filter
            # (Codex R12).
            base = base.where(
                LibraryItem.lifecycle_status.notin_(EXCLUDED_LIBRARY_LIFECYCLE_STATUSES)
            )
        # The total must reflect what the actor may see: for restricted actors
        # the caller passes the restricted trust levels so the count never leaks
        # hidden items (the filter is applied before the aggregate).
        if exclude_trust_levels:
            base = base.where(LibraryItem.trust_level.notin_(set(exclude_trust_levels)))
        total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
        current_version = aliased(LibraryItemVersion)
        stmt = (
            base.outerjoin(
                current_version,
                and_(
                    current_version.id == LibraryItem.current_version_id,
                    current_version.library_item_id == LibraryItem.id,
                ),
            )
            .add_columns(current_version.processing_status)
            .order_by(LibraryItem.updated_at.desc(), LibraryItem.id.desc())
        )
        decoded = _decode_keyset_cursor(cursor)
        if decoded is not None:
            cur_ts, cur_id = decoded
            stmt = stmt.where(
                tuple_(LibraryItem.updated_at, LibraryItem.id) < (cur_ts, cur_id)
            )
        rows = session.execute(stmt.limit(limit + 1)).all()
        page, has_more = rows[:limit], len(rows) > limit
        last_source = page[-1][0] if page else None
        next_cursor = (
            _encode_keyset_cursor(last_source.updated_at, last_source.id)
            if has_more and last_source is not None
            else None
        )
        return LibraryItemListPage(
            items=[
                self._source_summary(source, processing_status=processing_status)
                for source, processing_status in page
            ],
            next_cursor=next_cursor,
            total=total,
            has_more=has_more,
        )

    def get_library_item(
        self,
        session: Session,
        workspace_id: UUID,
        library_item_id: UUID,
        *,
        actor: ActorContext | None = None,
    ) -> LibraryItemDetail:
        source = self.require_visible_library_item(
            session,
            workspace_id,
            library_item_id,
            actor=actor,
        )
        versions = session.scalars(
            select(LibraryItemVersion)
            .where(LibraryItemVersion.library_item_id == source.id)
            .order_by(LibraryItemVersion.version_no)
        ).all()
        conversion_states = self._library_conversion_states(session, versions)
        canonical_version = get_canonical_library_version(session, source)
        artifacts = (
            list_canonical_library_artifacts(session, canonical_version)
            if canonical_version is not None
            else ()
        )
        current: LibraryItemVersion | None = None
        current_version = None
        if source.current_version_id is not None:
            current = get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=source.current_version_id,
            )
            current_version = (
                self._library_item_version_payload(
                    current,
                    workspace_id=workspace_id,
                    conversion=conversion_states.get(current.id),
                )
                if current is not None
                else None
            )
        current_ready_version = None
        if source.current_ready_version_id is not None:
            ready = get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=source.current_ready_version_id,
            )
            current_ready_version = (
                self._library_item_version_payload(
                    ready,
                    workspace_id=workspace_id,
                    conversion=conversion_states.get(ready.id),
                )
                if ready is not None
                else None
            )
        return LibraryItemDetail(
            **self._source_summary(
                source,
                processing_status=(
                    current.processing_status if current is not None else None
                ),
            ).model_dump(),
            metadata=source.metadata_json,
            current_version=current_version,
            current_ready_version=current_ready_version,
            versions=[
                self._library_item_version_payload(
                    version,
                    session=session,
                    workspace_id=workspace_id,
                    conversion=conversion_states.get(version.id),
                    compute_download_availability=True,
                )
                for version in versions
            ],
            artifacts=[
                self._library_artifact_payload(
                    artifact,
                    workspace_id=workspace_id,
                )
                for artifact in artifacts
            ],
        )

    def require_visible_library_item(
        self,
        session: Session,
        workspace_id: UUID,
        library_item_id: UUID,
        *,
        actor: ActorContext | None = None,
    ) -> LibraryItem:
        """Resolve one visible item without hydrating its detail projection."""

        source = get_in_workspace(
            session,
            LibraryItem,
            workspace_id=workspace_id,
            resource_id=library_item_id,
        )
        if source is None:
            raise ReadNotFoundError("source not found")
        self._require_visible_library_source(source, actor)
        return source

    def list_library_item_versions(
        self,
        session: Session,
        workspace_id: UUID,
        library_item_id: UUID,
        *,
        actor: ActorContext | None = None,
    ) -> list[LibraryItemVersionPayload]:
        """The explicit version collection for one item (P9.2)."""

        source = self.require_visible_library_item(
            session,
            workspace_id,
            library_item_id,
            actor=actor,
        )
        versions = session.scalars(
            select(LibraryItemVersion)
            .where(LibraryItemVersion.library_item_id == source.id)
            .order_by(LibraryItemVersion.version_no)
        ).all()
        conversion_states = self._library_conversion_states(session, versions)
        return [
            self._library_item_version_payload(
                version,
                session=session,
                workspace_id=workspace_id,
                conversion=conversion_states.get(version.id),
                compute_download_availability=True,
            )
            for version in versions
        ]

    def get_library_item_version(
        self,
        session: Session,
        workspace_id: UUID,
        version_id: UUID,
        *,
        actor: ActorContext | None = None,
    ) -> LibraryItemVersionPayload:
        version = get_in_workspace(
            session,
            LibraryItemVersion,
            workspace_id=workspace_id,
            resource_id=version_id,
        )
        if version is None:
            raise ReadNotFoundError("source version not found")
        source = get_in_workspace(
            session,
            LibraryItem,
            workspace_id=workspace_id,
            resource_id=version.library_item_id,
        )
        if source is None:
            raise ReadNotFoundError("source version not found")
        self._require_visible_library_source(source, actor)
        record_served_read("library_item", source.id, version)
        return self._library_item_version_payload(
            version,
            workspace_id=workspace_id,
            conversion=self._library_conversion_states(session, (version,)).get(
                version.id
            ),
        )

    def get_library_artifact(
        self,
        session: Session,
        workspace_id: UUID,
        artifact_id: UUID,
        *,
        actor: ActorContext | None = None,
    ) -> LibraryArtifactPayload:
        # P9.2: conversion and the item's currently selected preview-generation
        # artifact share this one flat metadata surface -- see
        # ``library_canonical.canonical_library_artifact_lookup``.
        lookup = canonical_library_artifact_lookup(
            session,
            workspace_id=workspace_id,
            artifact_id=artifact_id,
        )
        if lookup is None:
            raise ReadNotFoundError("source artifact not found")
        source, _version, artifact = lookup
        self._require_visible_library_source(source, actor)
        record_served_read("library_item", source.id, _version)
        return self._library_artifact_payload(
            artifact,
            workspace_id=workspace_id,
        )

    @staticmethod
    def _require_visible_library_source(
        source: LibraryItem,
        actor: ActorContext | None,
    ) -> None:
        # An archived/hidden item is hidden from every read lane (search, tree,
        # graph, OKF, citation); a direct GET must 404 too rather than re-serve
        # its versions/artifacts (Codex round-11/12 — same class as bundle/ask).
        if source.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
            raise ReadNotFoundError("source not found")
        if not actor_can_read_library_trust_level(source.trust_level, actor):
            raise RejectedByPolicyError(
                "restricted source requires elevated access",
                error_code="restricted_library_denied",
            )

    def list_agent_categories(
        self,
        session: Session,
        workspace_id: UUID,
    ) -> list[AgentCategoryEntry]:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        category_policy = workspace.agent_injection_policy.get(
            "category_inclusion_policy",
            {},
        )
        return [
            AgentCategoryEntry(
                category=category,
                description=description,
                default_injection_policy=category_policy.get(
                    category,
                    "task_matched",
                ),
            )
            for category, description in AGENT_PAGE_TAXONOMY
        ]

    def list_agent_pages(
        self,
        session: Session,
        workspace_id: UUID,
        category: str | None = None,
        lifecycle_status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        *,
        actor: ActorContext | None = None,
    ) -> AgentPageListPage:
        base = select(AgentPage).where(AgentPage.workspace_id == workspace_id)
        if not actor_can_read_drafts(actor):
            base = base.where(
                AgentPage.lifecycle_status == "active",
                AgentPage.current_active_version_id.is_not(None),
            )
        if not actor_can_read_sensitive_agent_pages(actor):
            from backend.schemas.agent import SENSITIVE_AGENT_PAGE_CATEGORIES

            base = base.where(
                AgentPage.category.notin_(SENSITIVE_AGENT_PAGE_CATEGORIES),
                AgentPage.sensitivity.in_(VISIBLE_AGENT_SENSITIVITIES),
            )
        if category is not None:
            base = base.where(AgentPage.category == category)
        if lifecycle_status is not None:
            base = base.where(AgentPage.lifecycle_status == lifecycle_status)
        else:
            # Draft-read capability must not resurrect archived pages in the
            # recency overview. Explicit archive inspection still uses the
            # lifecycle filter above.
            base = base.where(AgentPage.lifecycle_status != "archived")
        total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
        # Recency-first so the keyset cursor (updated_at,id) is monotonic and the
        # overview shows the newest agent pages, matching Context/Library.
        stmt = base.order_by(AgentPage.updated_at.desc(), AgentPage.id.desc())
        decoded = _decode_keyset_cursor(cursor)
        if decoded is not None:
            cur_ts, cur_id = decoded
            stmt = stmt.where(
                tuple_(AgentPage.updated_at, AgentPage.id) < (cur_ts, cur_id)
            )
        rows = session.scalars(stmt.limit(limit + 1)).all()
        page, has_more = rows[:limit], len(rows) > limit
        next_cursor = (
            _encode_keyset_cursor(page[-1].updated_at, page[-1].id)
            if has_more and page
            else None
        )
        return AgentPageListPage(
            items=[self._agent_page_summary(item, actor=actor) for item in page],
            next_cursor=next_cursor,
            total=total,
            has_more=has_more,
        )

    def agent_page_category(
        self,
        session: Session,
        workspace_id: UUID,
        agent_page_id: UUID,
    ) -> str | None:
        """Cheap category lookup for the sensitive-body owner gate (keeps the
        REST layer out of the models package)."""
        return session.scalar(
            select(AgentPage.category)
            .where(AgentPage.id == agent_page_id)
            .where(AgentPage.workspace_id == workspace_id)
        )

    def agent_page_requires_sensitive_access(
        self,
        session: Session,
        workspace_id: UUID,
        agent_page_id: UUID | None = None,
        slug: str | None = None,
    ) -> bool:
        page = self._resolve_agent_page(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=agent_page_id,
            slug=slug,
        )
        from backend.services.actor_visible_read import (
            agent_page_requires_sensitive_access,
        )

        return agent_page_requires_sensitive_access(page)

    def read_agent_page(
        self,
        session: Session,
        workspace_id: UUID,
        agent_page_id: UUID | None = None,
        slug: str | None = None,
        version_selector: str | int = "active",
        *,
        actor: ActorContext | None = None,
    ) -> AgentPageDetail:
        page = self._resolve_agent_page(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=agent_page_id,
            slug=slug,
        )
        if not agent_page_visible(page, actor, workspace_id=workspace_id):
            raise ReadNotFoundError("agent page not found")
        version = self._select_agent_version(
            session=session,
            page=page,
            version_selector=version_selector,
        )
        if version is not None and not agent_version_is_visible(page, version, actor):
            raise ReadNotFoundError("agent page version not found")
        record_served_read("agent_page", page.id, version)
        tree_path = self.tree_read_service.path_for_target(
            session=session,
            workspace_id=workspace_id,
            target_kind="agent_page",
            target_id=page.id,
        )
        tree_node = self.tree_read_service.primary_node_for_target(
            session=session,
            workspace_id=workspace_id,
            target_kind="agent_page",
            target_id=page.id,
        )
        parent_node_id = tree_node.parent_id if tree_node is not None else None
        children_count = (
            self.tree_read_service.children_count(
                session=session,
                workspace_id=workspace_id,
                parent_id=tree_node.id,
            )
            if tree_node is not None
            else 0
        )
        return AgentPageDetail(
            **self._agent_page_summary(page, actor=actor).model_dump(),
            body_markdown=version.body_markdown if version is not None else None,
            frontmatter=version.frontmatter if version is not None else {},
            version_info=(
                self._agent_version_info(version) if version is not None else None
            ),
            tree_path=tree_path,
            parent_node_id=parent_node_id,
            children_count=children_count,
        )

    def list_context_pages(
        self,
        session: Session,
        workspace_id: UUID,
        page_type: str | None = None,
        status: str | None = None,
        has_draft: bool | None = None,
        limit: int = 50,
        cursor: str | None = None,
        *,
        actor: ActorContext | None = None,
    ) -> ContextPageListPage:
        if has_draft is not None and not actor_can_read_drafts(actor):
            raise ScopeDeniedError(
                "filtering Context pages by draft state requires draft-read capability"
            )
        visible_version = visible_context_version_expression(actor)
        published_version = aliased(ContextPageVersion)
        draft_version = aliased(ContextPageVersion)
        modified_at = func.coalesce(
            published_version.created_at,
            draft_version.created_at,
            ContextPage.updated_at,
        )
        base = (
            select(ContextPage, modified_at.label("modified_at"))
            .outerjoin(
                published_version,
                published_version.id == ContextPage.current_published_version_id,
            )
            .outerjoin(
                draft_version,
                draft_version.id == ContextPage.latest_draft_version_id,
            )
            .where(
                ContextPage.workspace_id == workspace_id,
                visible_version.is_not(None),
            )
        )
        if page_type is not None:
            base = base.where(ContextPage.page_type == page_type)
        if status is not None:
            base = base.where(ContextPage.lifecycle_status == status)
        else:
            # The overview is a navigation list of current pages, not an
            # activity log. Archived pages have no live tree destination and
            # must follow the default-hidden lifecycle policy used by Library.
            base = base.where(ContextPage.lifecycle_status != "archived")
        if has_draft is not None:
            has_unpublished_head = or_(
                ContextPage.latest_draft_version_id.is_not(None),
                ContextPage.current_version_id.is_not(None)
                & (
                    ContextPage.current_published_version_id.is_(None)
                    | (
                        ContextPage.current_version_id
                        != ContextPage.current_published_version_id
                    )
                ),
            )
            base = base.where(
                has_unpublished_head if has_draft else ~has_unpublished_head
            )
        total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
        stmt = base.order_by(modified_at.desc(), ContextPage.id.desc())
        decoded = _decode_keyset_cursor(cursor)
        if decoded is not None:
            cur_ts, cur_id = decoded
            stmt = stmt.where(tuple_(modified_at, ContextPage.id) < (cur_ts, cur_id))
        rows = session.execute(stmt.limit(limit + 1)).all()
        page_rows, has_more = rows[:limit], len(rows) > limit
        next_cursor = (
            _encode_keyset_cursor(
                page_rows[-1].modified_at,
                page_rows[-1].ContextPage.id,
            )
            if has_more and page_rows
            else None
        )
        return ContextPageListPage(
            items=[
                self._context_page_summary(
                    row.ContextPage,
                    actor=actor,
                    modified_at=row.modified_at,
                )
                for row in page_rows
            ],
            next_cursor=next_cursor,
            total=total,
            has_more=has_more,
        )

    def read_context_page(
        self,
        session: Session,
        workspace_id: UUID,
        page_id: UUID,
        version_id: UUID | None = None,
        *,
        actor: ActorContext | None = None,
        include_warnings: bool = True,
    ) -> ContextPageDetail:
        page = get_in_workspace(
            session,
            ContextPage,
            workspace_id=workspace_id,
            resource_id=page_id,
        )
        if page is None:
            raise ReadNotFoundError("context page not found")
        if not context_page_visible(page, actor, workspace_id=workspace_id):
            raise ReadNotFoundError("context page not found")
        visible_head_id, _visible_is_draft = visible_context_version_id(page, actor)
        selected_version_id = version_id or visible_head_id
        if selected_version_id is None:
            raise ReadNotFoundError("context page not found")
        version = None
        if selected_version_id is not None:
            version = get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=selected_version_id,
            )
        if version_id is not None and version is None:
            raise ReadNotFoundError("context page version not found")
        if version is not None and version.page_id != page.id:
            raise ReadNotFoundError("context page version not found")
        if version is not None and not context_version_is_visible(version, actor):
            raise ReadNotFoundError("context page version not found")
        record_served_read("context_page", page.id, version)
        enrichment = self.context_page_read_enrichment(
            session,
            workspace_id,
            page,
            version,
            actor,
            include_warnings=include_warnings,
        )
        return ContextPageDetail(
            **self._context_page_summary(page, actor=actor).model_dump(),
            body_markdown=version.body_md if version is not None else None,
            version_info=(
                self._context_version_info(session, page, version, actor=actor)
                if version is not None
                else None
            ),
            **enrichment,
        )

    def context_page_read_enrichment(
        self,
        session: Session,
        workspace_id: UUID,
        page: ContextPage,
        version: ContextPageVersion | None,
        actor: ActorContext | None,
        *,
        include_warnings: bool = True,
    ) -> dict[str, Any]:
        """Read-only projection fields for a Context page's canonical detail
        response: tree placement, backlinks/wikilinks, lint warnings, the
        open pending-review summary, and reconciliation availability.

        Extracted out of ``read_context_page`` (P9.1) so the canonical REST
        detail route (``backend.api.context_pages``) and the MCP-facing
        ``read_context_page``/``read_target`` share exactly one computation
        instead of a second copy of this projection.
        """
        published_version = (
            get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=page.current_published_version_id,
            )
            if page.current_published_version_id is not None
            else None
        )
        if published_version is not None and published_version.page_id != page.id:
            # Do not project a cross-page corrupt pointer as trusted metadata.
            published_version = None
        tree_path = self.tree_read_service.path_for_target(
            session=session,
            workspace_id=workspace_id,
            target_kind="context_page",
            target_id=page.id,
        )
        tree_node = self.tree_read_service.primary_node_for_target(
            session=session,
            workspace_id=workspace_id,
            target_kind="context_page",
            target_id=page.id,
        )
        parent_node_id = tree_node.parent_id if tree_node is not None else None
        backlink_count = (
            session.scalar(
                select(func.count())
                .select_from(PageLink)
                .join(ContextPage, ContextPage.id == PageLink.source_page_id)
                .where(PageLink.workspace_id == workspace_id)
                .where(
                    (PageLink.target_page_id == page.id)
                    | (
                        PageLink.target_page_id.is_(None)
                        & (PageLink.target_slug == page.slug)
                    )
                )
                .where(PageLink.source_page_id != page.id)
                .where(ContextPage.lifecycle_status != "archived")
                .where(
                    PageLink.source_version_id
                    == visible_context_version_expression(actor)
                )
            )
            or 0
        )
        wikilink_targets = []
        if version is not None:
            target_page = aliased(ContextPage)
            # A link pinned at write time to a page that did not exist yet
            # leaves `target_page_id` NULL forever -- nothing re-resolves it
            # when the target is created later, so the link renders broken
            # even after the page shows up. Fall back to a live slug lookup
            # for exactly those unresolved rows, mirroring the same
            # `target_page_id IS NULL AND target_slug matches` fallback the
            # backlink_count query above already uses. Archived pages resolve
            # nowhere (see B-8 in _sync_page_name_index), so they don't count.
            target_page_by_slug = aliased(ContextPage)
            wikilink_targets = [
                {
                    "target_slug": link.target_slug,
                    "target_page_id": link.target_page_id or fallback_page_id,
                    "current_slug": current_slug or fallback_slug,
                }
                for (
                    link,
                    current_slug,
                    fallback_page_id,
                    fallback_slug,
                ) in session.execute(
                    select(
                        PageLink,
                        target_page.slug,
                        target_page_by_slug.id,
                        target_page_by_slug.slug,
                    )
                    .outerjoin(target_page, target_page.id == PageLink.target_page_id)
                    .outerjoin(
                        target_page_by_slug,
                        (target_page_by_slug.workspace_id == PageLink.workspace_id)
                        & (target_page_by_slug.slug == PageLink.target_slug)
                        & (target_page_by_slug.lifecycle_status != "archived")
                        & PageLink.target_page_id.is_(None),
                    )
                    .where(
                        PageLink.source_page_id == page.id,
                        PageLink.source_version_id == version.id,
                    )
                    .order_by(PageLink.target_slug.asc())
                )
            ]
        # M1: lint the EXACT version being displayed (version-aware), so warnings
        # match the body in the response. Skipped on internal existence-check
        # reads (e.g. citation validation) via include_warnings=False.
        warnings: list[str] = []
        pending_review: PendingReviewInfo | None = None
        pending_draft_version_id: UUID | None = None
        pending_draft_warnings: list[str] = []
        reconciliation_versions: list[ContextPageVersion] = []
        reconciliation_pending_count_truncated = False
        if actor_can_read_drafts(actor):
            from backend.services.context_reconciliation import (
                verified_reconciliation_branch_is_available,
            )

            reconciliation_candidates = session.scalars(
                select(ContextPageVersion)
                .where(
                    ContextPageVersion.workspace_id == workspace_id,
                    ContextPageVersion.page_id == page.id,
                    ContextPageVersion.graph_status == "branched",
                    ContextPageVersion.version_status == "draft",
                    ContextPageVersion.reconcile_manifest.is_not(None),
                )
                .order_by(
                    ContextPageVersion.created_at.desc(),
                    ContextPageVersion.id.desc(),
                )
                .limit(_RECONCILIATION_SUMMARY_SCAN_LIMIT + 1)
            ).all()
            reconciliation_pending_count_truncated = (
                len(reconciliation_candidates) > _RECONCILIATION_SUMMARY_SCAN_LIMIT
            )
            reconciliation_versions = [
                candidate
                for candidate in reconciliation_candidates[
                    :_RECONCILIATION_SUMMARY_SCAN_LIMIT
                ]
                if verified_reconciliation_branch_is_available(
                    session,
                    page=page,
                    version=candidate,
                    actor_kind=actor.actor_kind if actor is not None else None,
                    actor_id=actor.actor_id if actor is not None else None,
                )
            ]
        if include_warnings:
            workspace = session.get(Workspace, workspace_id)
            lint_policy = (
                workspace.lint_policy
                if workspace is not None and workspace.lint_policy
                else None
            )
            warnings = page_format_warnings_for_body(
                session,
                workspace_id,
                page,
                version.body_md if version is not None else None,
                registry=self.lint_registry,
                policy=lint_policy,
            )
            # F1: surface the open review for this page's CURRENT pending draft so
            # it can be acted on inline. Scoped to latest_draft_version_id via the
            # operation's diff_ref so a superseded/already-published older draft's
            # review never resurfaces here (P1): no current draft -> no pending
            # review; a newer draft -> only that draft's review.
            review_item = None
            if (
                actor_can_read_drafts(actor)
                and page.latest_draft_version_id is not None
            ):
                draft_ref = f"context_page_version:{page.latest_draft_version_id}"
                review_item = session.scalars(
                    select(ReviewItem)
                    .join(ChangeSet, ReviewItem.change_set_id == ChangeSet.id)
                    .join(
                        ChangeOperation,
                        ChangeOperation.change_set_id == ChangeSet.id,
                    )
                    .where(
                        ChangeSet.workspace_id == workspace_id,
                        ChangeOperation.target_type == "context_page",
                        ChangeOperation.target_id == page.id,
                        ChangeOperation.diff_ref == draft_ref,
                        ReviewItem.status.in_(("pending", "changes_requested")),
                    )
                    .order_by(ReviewItem.created_at.desc(), ReviewItem.id.desc())
                ).first()
            if review_item is not None:
                pending_review = PendingReviewInfo(
                    review_item_id=review_item.id,
                    status=review_item.status,
                    risk_level=review_item.risk_level,
                )
                pending_draft_version_id = page.latest_draft_version_id
                # Lint the DRAFT body (not the displayed/published one) so the
                # reviewer sees the draft's format issues before approving even
                # when the published head is clean (Sweep-2).
                draft_version = get_in_workspace(
                    session,
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    resource_id=page.latest_draft_version_id,
                )
                if draft_version is not None:
                    pending_draft_warnings = page_format_warnings_for_body(
                        session,
                        workspace_id,
                        page,
                        draft_version.body_md,
                        registry=self.lint_registry,
                        policy=lint_policy,
                    )
        return {
            "published_version_info": (
                PublishedVersionInfo(
                    version_id=published_version.id,
                    version_no=published_version.version_no,
                    published_at=published_version.created_at,
                )
                if published_version is not None
                else None
            ),
            "tree_path": tree_path,
            "parent_node_id": parent_node_id,
            "children_count": (
                self.tree_read_service.children_count(
                    session=session,
                    workspace_id=workspace_id,
                    parent_id=tree_node.id,
                )
                if tree_node is not None
                else 0
            ),
            "backlink_count": backlink_count,
            "wikilink_targets": wikilink_targets,
            "warnings": warnings,
            "pending_draft_version_id": pending_draft_version_id,
            "pending_draft_warnings": pending_draft_warnings,
            "pending_review": pending_review,
            "reconciliation_pending_count": len(reconciliation_versions),
            "reconciliation_pending_count_truncated": (
                reconciliation_pending_count_truncated
            ),
            "next_reconciliation_version_id": (
                reconciliation_versions[0].id if reconciliation_versions else None
            ),
            "reconciliation_available": bool(reconciliation_versions),
        }

    def read_target(
        self,
        session: Session,
        workspace_id: UUID,
        target_type: str,
        target_id: UUID,
        *,
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        if target_type == "context_page":
            return self.read_context_page(
                session=session,
                workspace_id=workspace_id,
                page_id=target_id,
                actor=actor,
            ).model_dump(mode="json")
        if target_type == "context_page_version":
            return self._read_context_page_version(
                session=session,
                workspace_id=workspace_id,
                version_id=target_id,
                actor=actor,
            )
        if target_type == "library_item_version":
            return self.get_library_item_version(
                session=session,
                workspace_id=workspace_id,
                version_id=target_id,
                actor=actor,
            ).model_dump(mode="json")
        if target_type == "library_artifact":
            return self.get_library_artifact(
                session=session,
                workspace_id=workspace_id,
                artifact_id=target_id,
                actor=actor,
            ).model_dump(mode="json")
        if target_type == "context_claim":
            return self._read_claim(
                session=session,
                workspace_id=workspace_id,
                claim_id=target_id,
                actor=actor,
            )
        if target_type == "context_citation":
            return self._read_citation(
                session=session,
                workspace_id=workspace_id,
                citation_id=target_id,
                actor=actor,
            )
        if target_type == "feedback":
            if not actor_can_read_feedback(actor):
                raise ReadNotFoundError("feedback not found")
            return self._read_feedback(
                session=session,
                workspace_id=workspace_id,
                feedback_id=target_id,
                actor=actor,
            )
        if target_type == "change_set":
            if not actor_can_read_audit_records(actor):
                raise ReadNotFoundError("change set not found")
            return self._read_change_set(
                session=session,
                workspace_id=workspace_id,
                change_set_id=target_id,
                actor=actor,
            )
        raise ReadNotFoundError("target not found")

    def fetch_target(
        self,
        session: Session,
        workspace_id: UUID,
        entity_id: UUID,
        entity_type: str,
        *,
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        result = self._fetch_visible_target(
            session=session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            entity_type=entity_type,
            actor=actor,
        )
        if result is not None:
            return result

        # A pair naming an actor-visible entity in the wrong namespace is a
        # malformed search identifier. Hidden entities are deliberately not
        # considered here, so this validation distinction cannot reveal them.
        for candidate_type in (
            "context_page",
            "context_claim",
            "library_item",
            "library_artifact",
            "agent_page",
            "feedback",
            "change_set",
        ):
            if candidate_type == entity_type:
                continue
            if (
                self._fetch_visible_target(
                    session=session,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    entity_type=candidate_type,
                    actor=actor,
                )
                is not None
            ):
                raise ReadValidationError("id does not match type")
        raise ReadNotFoundError("fetch target not found")

    def _fetch_visible_target(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        entity_id: UUID,
        entity_type: str,
        actor: ActorContext | None,
    ) -> dict[str, Any] | None:
        if entity_type == "context_page":
            page = get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=entity_id,
            )
            if not context_page_visible(page, actor, workspace_id=workspace_id):
                return None
            version_id, _is_draft = visible_context_version_id(page, actor)
            version = get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=version_id,
            )
            if version is None:
                return None
            concept_path = context_concept_path(page.slug)
            record_served_read("context_page", page.id, version)
            return {
                "id": page.id,
                "title": page.title,
                "text": version.body_md,
                "url": f"/context?target={page.id}",
                "metadata": {
                    "type": entity_type,
                    "version_id": str(version.id),
                    "concept_path": concept_path,
                    "page_type": page.page_type,
                },
            }

        if entity_type == "context_claim":
            claim = get_in_workspace(
                session,
                ContextClaim,
                workspace_id=workspace_id,
                resource_id=entity_id,
            )
            version = (
                get_in_workspace(
                    session,
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    resource_id=claim.page_version_id,
                )
                if claim is not None
                else None
            )
            page = (
                get_in_workspace(
                    session,
                    ContextPage,
                    workspace_id=workspace_id,
                    resource_id=version.page_id,
                )
                if version is not None
                else None
            )
            if (
                page is None
                or not context_page_visible(page, actor, workspace_id=workspace_id)
                or not context_version_is_visible(version, actor)
            ):
                return None
            concept_path = context_concept_path(page.slug)
            record_served_read("context_page", page.id, version)
            return {
                "id": claim.id,
                "title": f"Claim in {page.title}",
                "text": claim.claim_text,
                "url": f"/context?target={page.id}#claim-{claim.id}",
                "metadata": {
                    "type": entity_type,
                    "version_id": str(version.id),
                    "page_id": str(page.id),
                    "concept_path": concept_path,
                    "claim_type": claim.claim_type,
                    "status": claim.status,
                    "location_in_page": claim.location_in_page,
                },
            }

        if entity_type == "library_item":
            item = get_in_workspace(
                session,
                LibraryItem,
                workspace_id=workspace_id,
                resource_id=entity_id,
            )
            if not library_item_visible(item, actor, workspace_id=workspace_id):
                return None
            version = get_canonical_library_version(session, item)
            artifacts = (
                list_canonical_library_artifacts(session, version)
                if version is not None
                else ()
            )
            served_text = "\n\n".join(
                artifact.conversion_detail.index_text
                for artifact in artifacts
                if artifact.conversion_detail.index_text
            )
            record_served_read("library_item", item.id, version, content=served_text)
            return {
                "id": item.id,
                "title": item.title or item.uri,
                "text": served_text,
                "url": f"/library?target={item.id}",
                "metadata": {
                    "type": entity_type,
                    "version_id": str(version.id) if version is not None else None,
                    "concept_path": library_concept_path(item.id),
                    "uri": item.uri,
                    "connector_type": item.connector_type,
                },
            }

        if entity_type == "library_artifact":
            artifact = get_in_workspace(
                session,
                LibraryArtifact,
                workspace_id=workspace_id,
                resource_id=entity_id,
            )
            version = (
                get_in_workspace(
                    session,
                    LibraryItemVersion,
                    workspace_id=workspace_id,
                    resource_id=artifact.library_item_version_id,
                )
                if artifact is not None
                else None
            )
            item = (
                get_in_workspace(
                    session,
                    LibraryItem,
                    workspace_id=workspace_id,
                    resource_id=version.library_item_id,
                )
                if version is not None
                else None
            )
            if not library_item_visible(
                item,
                actor,
                workspace_id=workspace_id,
                version=version,
            ):
                return None
            if not canonical_library_artifact_belongs_to_item(
                session,
                item=item,
                artifact_id=artifact.id,
            ):
                return None
            record_served_read(
                "library_item",
                item.id,
                version,
                content=artifact.conversion_detail.index_text or "",
            )
            return {
                "id": artifact.id,
                "title": item.title or artifact.kind.replace("_", " ").title(),
                "text": artifact.conversion_detail.index_text or "",
                "url": f"/library?target={item.id}#artifact-{artifact.id}",
                "metadata": {
                    "type": entity_type,
                    "version_id": str(version.id),
                    "library_item_id": str(item.id),
                    "concept_path": library_concept_path(item.id),
                    "artifact_type": artifact.kind,
                },
            }

        if entity_type == "agent_page":
            page = get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=entity_id,
            )
            if not agent_page_visible(page, actor, workspace_id=workspace_id):
                return None
            if not has_required_scope(actor, AGENT_PAGE_BODY_SCOPES):
                # MCP-02: fetch's own TOOL_REQUIRED_SCOPES entry only demands
                # "read", but AgentPage bodies are gated behind
                # agent-page-read everywhere else (read_agent_page, the
                # prompts/resources lane) -- widening "fetch"'s own required
                # scopes would be wrong (has_required_scope is any-of, so
                # that loosens the check instead of tightening it). Same
                # "invisible entity" convention as the other branches below.
                return None
            version_id = (
                page.latest_draft_version_id
                if actor_can_read_drafts(actor)
                and page.latest_draft_version_id is not None
                else page.current_active_version_id
            )
            if version_id is None:
                return None
            version = get_in_workspace(
                session,
                AgentPageVersion,
                workspace_id=workspace_id,
                resource_id=version_id,
            )
            if version is None:
                return None
            record_served_read("agent_page", page.id, version)
            return {
                "id": page.id,
                "title": page.title,
                "text": version.body_markdown,
                "url": f"/agent?target={page.id}",
                "metadata": {
                    "type": entity_type,
                    "version_id": str(version.id),
                    "slug": page.slug,
                    "category": page.category,
                },
            }

        if entity_type == "feedback":
            if not actor_can_read_feedback(actor):
                return None
            feedback = get_in_workspace(
                session,
                FeedbackItem,
                workspace_id=workspace_id,
                resource_id=entity_id,
            )
            if feedback is None:
                return None
            from backend.services.feedback_service import FeedbackService

            if not FeedbackService._feedback_target_visible(session, feedback, actor):
                return None
            return {
                "id": feedback.id,
                "title": f"Feedback: {feedback.severity}",
                "text": feedback.body,
                "url": f"dtxwiki://workspaces/{workspace_id}/feedback/{feedback.id}",
                "metadata": {
                    "type": entity_type,
                    "target_type": feedback.target_type,
                    "target_id": str(feedback.target_id),
                    "severity": feedback.severity,
                    "status": feedback.status,
                },
            }

        if entity_type == "change_set":
            if not actor_can_read_audit_records(actor):
                return None
            change_set = get_in_workspace(
                session,
                ChangeSet,
                workspace_id=workspace_id,
                resource_id=entity_id,
            )
            if change_set is None:
                return None
            # LEAK-02: same target-visibility gate as _read_change_set — the
            # capability check above says nothing about the operations' targets.
            if not change_set_visible(session, change_set, actor):
                return None
            operations = session.scalars(
                select(ChangeOperation)
                .where(ChangeOperation.change_set_id == change_set.id)
                .order_by(ChangeOperation.created_at, ChangeOperation.id)
            ).all()
            operation_text = [
                " ".join(
                    part
                    for part in (
                        operation.operation,
                        operation.diff_summary,
                    )
                    if part
                )
                for operation in operations
            ]
            text = "\n".join(
                part
                for part in (
                    change_set.reason,
                    f"Status: {change_set.status}",
                    *operation_text,
                )
                if part
            )
            return {
                "id": change_set.id,
                "title": change_set.reason or "Change set",
                "text": text,
                "url": f"dtxwiki://workspaces/{workspace_id}/changes/{change_set.id}",
                "metadata": {
                    "type": entity_type,
                    "trigger_type": change_set.trigger_type,
                    "status": change_set.status,
                    "risk_level": change_set.risk_level,
                },
            }

        return None

    def list_changes(
        self,
        session: Session,
        workspace_id: UUID,
        target_type: str,
        target_id: UUID,
        include_diff: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        *,
        actor: ActorContext | None = None,
    ) -> ListChangesPayload:
        if not actor_can_read_audit_records(actor):
            raise ReadNotFoundError("target not found")
        self._require_visible_history_target(
            session=session,
            workspace_id=workspace_id,
            target_type=target_type,
            target_id=target_id,
            actor=actor,
        )
        changeset_stmt = select(ChangeSet).where(
            ChangeSet.workspace_id == workspace_id,
            ChangeSet.operations.any(
                and_(
                    ChangeOperation.target_type == target_type,
                    ChangeOperation.target_id == target_id,
                )
            ),
        )
        if not actor_can_read_drafts(actor):
            changeset_stmt = changeset_stmt.where(ChangeSet.status == "applied")
        changesets = session.scalars(
            changeset_stmt.order_by(ChangeSet.created_at.desc()).limit(limit)
        ).all()
        change_ids = [change_set.id for change_set in changesets]
        decisions = []
        if change_ids:
            decisions = session.scalars(
                select(ReviewDecision)
                .join(ReviewItem, ReviewDecision.review_item_id == ReviewItem.id)
                .where(ReviewItem.change_set_id.in_(change_ids))
                .order_by(ReviewDecision.created_at.desc())
            ).all()
        before_version_no = _decode_version_history_cursor(
            cursor,
            workspace_id=workspace_id,
            target_type=target_type,
            target_id=target_id,
        )
        version_page = self._version_entries(
            session=session,
            workspace_id=workspace_id,
            target_type=target_type,
            target_id=target_id,
            limit=limit + 1,
            actor=actor,
            before_version_no=before_version_no,
        )
        has_more_versions = len(version_page) > limit
        versions = version_page[:limit]
        next_cursor = (
            _encode_version_history_cursor(
                workspace_id=workspace_id,
                target_type=target_type,
                target_id=target_id,
                before_version_no=versions[-1].version_no,
            )
            if has_more_versions and versions
            else None
        )
        return ListChangesPayload(
            versions=versions,
            changesets=[
                self._change_set_entry(change_set) for change_set in changesets
            ],
            review_decisions=[
                self._review_decision_entry(decision) for decision in decisions
            ],
            diffs=self._diff_entries(session, changesets) if include_diff else None,
            next_cursor=next_cursor,
        )

    def _resolve_agent_page(
        self,
        session: Session,
        workspace_id: UUID,
        agent_page_id: UUID | None,
        slug: str | None,
    ) -> AgentPage:
        if agent_page_id is not None:
            page = get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=agent_page_id,
            )
        elif slug is not None:
            page = session.scalar(
                select(AgentPage).where(
                    AgentPage.workspace_id == workspace_id,
                    AgentPage.slug == slug,
                )
            )
        else:
            raise ReadNotFoundError("agent page not found")
        if page is None:
            raise ReadNotFoundError("agent page not found")
        return page

    def _read_context_page_version(
        self,
        session: Session,
        workspace_id: UUID,
        version_id: UUID,
        *,
        actor: ActorContext | None = None,
        include_warnings: bool = True,
    ) -> dict[str, Any]:
        version = get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=workspace_id,
            resource_id=version_id,
        )
        if version is None:
            raise ReadNotFoundError("context page version not found")
        page = get_in_workspace(
            session,
            ContextPage,
            workspace_id=workspace_id,
            resource_id=version.page_id,
        )
        if page is None:
            raise ReadNotFoundError("context page version not found")
        return self.read_context_page(
            session=session,
            workspace_id=workspace_id,
            page_id=page.id,
            version_id=version.id,
            actor=actor,
            include_warnings=include_warnings,
        ).model_dump(mode="json")

    def _read_claim(
        self,
        session: Session,
        workspace_id: UUID,
        claim_id: UUID,
        *,
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        claim = get_in_workspace(
            session,
            ContextClaim,
            workspace_id=workspace_id,
            resource_id=claim_id,
        )
        if claim is None:
            raise ReadNotFoundError("context claim not found")
        version = get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=workspace_id,
            resource_id=claim.page_version_id,
        )
        page = (
            get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=version.page_id,
            )
            if version is not None
            else None
        )
        if page is None:
            raise ReadNotFoundError("context claim not found")
        if version is None or not context_version_is_visible(version, actor):
            raise ReadNotFoundError("context claim not found")
        record_served_read("context_page", page.id, version)
        return {
            "id": str(claim.id),
            "page_version_id": str(claim.page_version_id),
            "claim_text": claim.claim_text,
            "location_in_page": claim.location_in_page,
            "claim_type": claim.claim_type,
            "confidence": claim.confidence,
            "status": claim.status,
            "is_core": claim.is_core,
        }

    def _read_citation(
        self,
        session: Session,
        workspace_id: UUID,
        citation_id: UUID,
        *,
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        citation = get_in_workspace(
            session,
            ContextCitation,
            workspace_id=workspace_id,
            resource_id=citation_id,
        )
        if citation is None or not self._citation_is_visible(
            session=session,
            workspace_id=workspace_id,
            citation=citation,
            actor=actor,
        ):
            raise ReadNotFoundError("context citation not found")
        return {
            "id": str(citation.id),
            "target_kind": citation.target_kind,
            "target_id": str(citation.target_id),
            "citation_type": citation.citation_type,
            "library_item_version_id": str(citation.library_item_version_id)
            if citation.library_item_version_id
            else None,
            "library_artifact_id": str(citation.library_artifact_id)
            if citation.library_artifact_id
            else None,
            "cited_context_page_version_id": str(citation.cited_context_page_version_id)
            if citation.cited_context_page_version_id
            else None,
            "snippet": citation.snippet,
            "verification_status": citation.verification_status,
        }

    @staticmethod
    def _read_feedback(
        session: Session,
        workspace_id: UUID,
        feedback_id: UUID,
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        from backend.services.feedback_service import FeedbackService

        feedback = get_in_workspace(
            session,
            FeedbackItem,
            workspace_id=workspace_id,
            resource_id=feedback_id,
        )
        if feedback is None:
            raise ReadNotFoundError("feedback not found")
        # A feedback about a target the reader cannot see must 404 like the list
        # lane, not re-serve the target via its id/body (Codex R13).
        if not FeedbackService._feedback_target_visible(session, feedback, actor):
            raise ReadNotFoundError("feedback not found")
        return {
            "id": str(feedback.id),
            "target_type": feedback.target_type,
            "target_id": str(feedback.target_id),
            "body": feedback.body,
            "severity": feedback.severity,
            "status": feedback.status,
        }

    @staticmethod
    def _read_change_set(
        session: Session,
        workspace_id: UUID,
        change_set_id: UUID,
        *,
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        change_set = get_in_workspace(
            session,
            ChangeSet,
            workspace_id=workspace_id,
            resource_id=change_set_id,
        )
        if change_set is None:
            raise ReadNotFoundError("change set not found")
        # LEAK-02: actor_can_read_audit_records (checked by the caller) is a
        # capability gate, not a target filter — a change set whose operations
        # touch a draft-only page/agent page must still 404, not reveal its
        # reason/diff_summary. 404, not 403, so existence is not disclosed.
        if not change_set_visible(session, change_set, actor):
            raise ReadNotFoundError("change set not found")
        operations = session.scalars(
            select(ChangeOperation).where(
                ChangeOperation.change_set_id == change_set.id
            )
        ).all()
        return {
            "id": str(change_set.id),
            "reason": change_set.reason,
            "trigger_type": change_set.trigger_type,
            "actor_type": change_set.actor_type,
            "status": change_set.status,
            "risk_level": change_set.risk_level,
            "operations": [
                {
                    "id": str(operation.id),
                    "target_type": operation.target_type,
                    "target_id": str(operation.target_id),
                    "operation": operation.operation,
                    "diff_summary": operation.diff_summary,
                    "diff_ref": operation.diff_ref,
                }
                for operation in operations
            ],
        }

    def _citation_is_visible(
        self,
        session: Session,
        workspace_id: UUID,
        citation: ContextCitation,
        actor: ActorContext | None,
    ) -> bool:
        if citation.target_kind == "context_claim":
            claim = get_in_workspace(
                session,
                ContextClaim,
                workspace_id=workspace_id,
                resource_id=citation.target_id,
            )
            target_version = (
                get_in_workspace(
                    session,
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    resource_id=claim.page_version_id,
                )
                if claim is not None
                else None
            )
        else:
            # ContextCitation stores the owning ContextPageVersion id for all
            # page/section/whole target variants; only context_claim points to
            # a claim row.
            target_version = get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=citation.target_id,
            )
        target_page = (
            get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=target_version.page_id,
            )
            if target_version is not None
            else None
        )
        if target_page is None or not context_version_is_visible(target_version, actor):
            return False
        if citation.library_item_version_id is not None:
            try:
                self.get_library_item_version(
                    session=session,
                    workspace_id=workspace_id,
                    version_id=citation.library_item_version_id,
                    actor=actor,
                )
            except (ReadNotFoundError, RejectedByPolicyError):
                return False
            return True
        if citation.library_artifact_id is not None:
            try:
                self.get_library_artifact(
                    session=session,
                    workspace_id=workspace_id,
                    artifact_id=citation.library_artifact_id,
                    actor=actor,
                )
            except (ReadNotFoundError, RejectedByPolicyError):
                return False
            return True
        if citation.cited_context_page_version_id is not None:
            try:
                self._read_context_page_version(
                    session=session,
                    workspace_id=workspace_id,
                    version_id=citation.cited_context_page_version_id,
                    actor=actor,
                    include_warnings=False,  # existence check only; skip lint work
                )
            except ReadNotFoundError:
                return False
            return True
        return False

    @staticmethod
    def _select_agent_version(
        session: Session,
        page: AgentPage,
        version_selector: str | int,
    ) -> AgentPageVersion | None:
        if version_selector == "active":
            return (
                get_in_workspace(
                    session,
                    AgentPageVersion,
                    workspace_id=page.workspace_id,
                    resource_id=page.current_active_version_id,
                )
                if page.current_active_version_id is not None
                else None
            )
        if version_selector == "draft":
            return (
                get_in_workspace(
                    session,
                    AgentPageVersion,
                    workspace_id=page.workspace_id,
                    resource_id=page.latest_draft_version_id,
                )
                if page.latest_draft_version_id is not None
                else None
            )
        return session.scalar(
            select(AgentPageVersion).where(
                AgentPageVersion.agent_page_id == page.id,
                AgentPageVersion.version_no == int(version_selector),
            )
        )

    @staticmethod
    def _source_summary(
        source: LibraryItem,
        *,
        processing_status: str | None,
    ) -> LibraryItemSummary:
        return LibraryItemSummary(
            id=source.id,
            uri=source.uri,
            title=source.title,
            connector_type=source.connector_type,
            format_hint=source.format_hint,
            trust_level=source.trust_level,
            lifecycle_status=source.lifecycle_status,
            current_version_id=source.current_version_id,
            current_ready_version_id=source.current_ready_version_id,
            current_preview_version_id=source.current_preview_version_id,
            processing_status=processing_status,
            updated_at=source.updated_at,
        )

    def _library_item_version_payload(
        self,
        version: LibraryItemVersion,
        *,
        session: Session | None = None,
        workspace_id: UUID | None = None,
        conversion: LibraryConversionStatePayload | None = None,
        compute_download_availability: bool = False,
    ) -> LibraryItemVersionPayload:
        # Doc §3-D: the stricter, blob-verifying check (and the "hide the URL
        # if it lied" behavior below) is scoped to History rows only --
        # `compute_download_availability=True` at that call site. The
        # current/ready-version payloads keep their existing cheap
        # presence-only `original_url` unconditionally: they're on the hot
        # item-detail read path, already covered by the P4.4 shared
        # projection with the Preview manifest, and out of this doc's scope.
        original_download_available = (
            library_original_download_available(
                session,
                workspace_id=workspace_id,
                version=version,
                blob_repository_factory=self._library_blob_repository_factory,
            )
            if compute_download_availability
            and session is not None
            and workspace_id is not None
            else False
        )
        original_url = (
            library_original_content_url(
                workspace_id=workspace_id,
                version_id=version.id,
                source_blob_id=version.source_blob_id,
            )
            if workspace_id is not None
            and (not compute_download_availability or original_download_available)
            else None
        )
        return LibraryItemVersionPayload(
            id=version.id,
            library_item_id=version.library_item_id,
            version_no=version.version_no,
            content_hash=version.content_hash,
            processing_status=version.processing_status,
            conversion=conversion
            or _library_conversion_state_payload(version, attempt=None),
            original_filename=version.original_filename,
            declared_media_type=version.declared_media_type,
            detected_media_type=version.detected_media_type,
            format_id=version.format_id,
            byte_size=version.byte_size,
            detection=version.detection_json,
            selected_conversion_attempt_id=version.selected_conversion_attempt_id,
            selected_preview_attempt_id=version.selected_preview_attempt_id,
            preview_status=version.preview_status,
            original_url=original_url,
            original_download_available=original_download_available,
            poll_url=(
                f"/api/v1/workspaces/{workspace_id}/library/"
                f"{version.library_item_id}/versions/{version.id}"
                if workspace_id is not None
                else None
            ),
            extraction_summary=_public_extraction_summary(version),
            previous_version_id=version.previous_version_id,
            created_at=version.created_at,
            fetched_at=version.fetched_at,
        )

    @staticmethod
    def _library_conversion_states(
        session: Session,
        versions: Collection[LibraryItemVersion],
    ) -> dict[UUID, LibraryConversionStatePayload]:
        version_by_id = {version.id: version for version in versions}
        if not version_by_id:
            return {}
        latest_runs: dict[UUID, LibraryRun] = {}
        for run in session.scalars(
            select(LibraryRun)
            .where(
                LibraryRun.library_item_version_id.in_(version_by_id),
                LibraryRun.stage == "conversion",
            )
            .order_by(
                LibraryRun.library_item_version_id,
                LibraryRun.run_seq.desc(),
            )
        ):
            latest_runs.setdefault(run.library_item_version_id, run)
        latest_attempts: dict[UUID, LibraryAttempt] = {}
        run_ids = {run.id for run in latest_runs.values()}
        if run_ids:
            for attempt in session.scalars(
                select(LibraryAttempt)
                .where(
                    LibraryAttempt.run_id.in_(run_ids),
                    LibraryAttempt.stage == "conversion",
                )
                .order_by(
                    LibraryAttempt.run_id,
                    LibraryAttempt.chain_index.desc(),
                )
            ):
                latest_attempts.setdefault(attempt.run_id, attempt)
        return {
            version_id: _library_conversion_state_payload(
                version,
                attempt=(
                    latest_attempts.get(run.id)
                    if (run := latest_runs.get(version_id)) is not None
                    else None
                ),
            )
            for version_id, version in version_by_id.items()
        }

    @staticmethod
    def _library_artifact_payload(
        artifact: LibraryArtifact,
        *,
        workspace_id: UUID | None = None,
    ) -> LibraryArtifactPayload:
        # P9.2: this payload now also describes preview-generation artifacts,
        # which carry a ``preview_detail`` row instead of ``conversion_detail``
        # -- the conversion-only extraction fields stay their existing
        # defaults for those.
        detail = artifact.conversion_detail
        if detail is not None:
            is_complete = detail.is_complete
            sanitizer_version = detail.sanitizer_version
            extraction_method = detail.extraction_method
            index_text = detail.index_text
            location_model = detail.location_model
            quality_score = (
                float(detail.quality_score)
                if detail.quality_score is not None
                else None
            )
        else:
            is_complete = True
            sanitizer_version = None
            extraction_method = None
            index_text = None
            location_model = {}
            quality_score = None
        return LibraryArtifactPayload(
            id=artifact.id,
            library_item_version_id=artifact.library_item_version_id,
            stage=artifact.stage,
            artifact_type=artifact.kind,
            role=artifact.role,
            media_type=artifact.media_type,
            byte_size=artifact.byte_size,
            ordinal=artifact.ordinal,
            is_complete=is_complete,
            sanitizer_version=sanitizer_version,
            extraction_method=extraction_method,
            index_text=index_text,
            location_model=location_model,
            quality_score=quality_score,
            content_url=(
                f"/api/v1/workspaces/{workspace_id}/library/artifacts/{artifact.id}/content"
                if workspace_id is not None and artifact.blob_id is not None
                else None
            ),
            created_at=artifact.created_at,
        )

    @staticmethod
    def _agent_page_summary(
        page: AgentPage,
        *,
        actor: ActorContext | None = None,
    ) -> AgentPageSummary:
        can_read_drafts = actor_can_read_drafts(actor)
        return AgentPageSummary(
            id=page.id,
            category=page.category,
            title=page.title,
            slug=page.slug,
            lifecycle_status=page.lifecycle_status,
            injection_policy=page.injection_policy,
            priority=page.priority,
            sensitivity=page.sensitivity,
            current_active_version_id=page.current_active_version_id,
            latest_draft_version_id=(
                page.latest_draft_version_id if can_read_drafts else None
            ),
            updated_at=page.updated_at,
        )

    @staticmethod
    def _agent_version_info(version: AgentPageVersion) -> VersionInfo:
        return VersionInfo(
            version_id=version.id,
            version_no=version.version_no,
            version_status=version.version_status,
            graph_status=None,
            parent_version_id=None,
            content_hash=None,
            created_at=version.created_at,
        )

    @staticmethod
    def _context_page_summary(
        page: ContextPage,
        *,
        actor: ActorContext | None = None,
        modified_at=None,
    ) -> ContextPageSummary:
        visible_id, _is_draft = visible_context_version_id(page, actor)
        can_read_drafts = actor_can_read_drafts(actor)
        # P2.2/P2.6: role-based `can_read_drafts` answers "may this actor see
        # SOME unpublished content", not "may this actor see THIS author's
        # unshared editing" -- a page-global pointer that currently names a
        # private `editing` row must fall back to the published id instead,
        # same rule as page_service.py's `_editable_head_for_actor`.
        # `current_version_id` and `latest_draft_version_id` are checked
        # independently via their own loaded relationships (not by comparing
        # one id to the other) because a page that has never published can
        # have `current_version_id` name an `editing` row directly, with
        # `latest_draft_version_id` staying `None` the whole time
        # (page-first creation, before any Save draft).
        latest_is_hidden_editing = editing_row_hidden_from_actor(
            page.latest_draft_version, actor
        )
        current_is_hidden_editing = editing_row_hidden_from_actor(
            page.current_version, actor
        )
        return ContextPageSummary(
            id=page.id,
            slug=page.slug,
            title=page.title,
            page_type=page.page_type,
            lifecycle_status=page.lifecycle_status,
            aliases=list(page.aliases or []),
            visible_version_id=visible_id,
            # Redacting only latest_draft_version_id would still answer the
            # question: current_version_id follows the draft, so it differs from
            # the published pointer exactly when one is open.
            current_version_id=(
                page.current_version_id
                if can_read_drafts and not current_is_hidden_editing
                else page.current_published_version_id
            ),
            current_published_version_id=page.current_published_version_id,
            latest_draft_version_id=(
                page.latest_draft_version_id
                if can_read_drafts and not latest_is_hidden_editing
                else None
            ),
            is_public=page.is_public,
            updated_at=modified_at
            or (
                page.current_published_version.created_at
                if page.current_published_version is not None
                else (
                    page.latest_draft_version.created_at
                    if page.latest_draft_version is not None
                    else page.updated_at
                )
            ),
        )

    @staticmethod
    def _context_version_info(
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
        *,
        actor: ActorContext | None,
    ) -> VersionInfo:
        projection = project_context_version(
            page,
            version,
            visible_version_id=visible_context_version_id(page, actor)[0],
            expose_draft_state=actor_can_read_drafts(actor),
        )
        parent = (
            get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=page.workspace_id,
                resource_id=version.parent_version_id,
            )
            if version.parent_version_id is not None
            else None
        )
        _prior_relation, visible_parent_id = context_parent_projection(
            version, parent, actor
        )
        return VersionInfo(
            version_id=version.id,
            version_no=version.version_no,
            version_status=version.version_status,
            review_disposition=context_version_review_disposition(
                session, version, workspace_id=page.workspace_id
            ),
            history_status=projection.history_status,
            graph_status=projection.graph_status,
            parent_version_id=visible_parent_id,
            content_hash=version.content_hash,
            created_at=version.created_at,
            is_visible_head=projection.is_visible_head,
            is_published_head=projection.is_published_head,
            is_draft_head=projection.is_draft_head,
            is_alternative=projection.is_alternative,
            is_discarded=projection.is_discarded,
        )

    def _require_visible_history_target(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        target_type: str,
        target_id: UUID,
        actor: ActorContext | None,
    ) -> None:
        if target_type == "context_page":
            page = get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
            if page is None or visible_context_version_id(page, actor)[0] is None:
                raise ReadNotFoundError("target not found")
            return
        if target_type == "context_page_version":
            self._read_context_page_version(
                session,
                workspace_id,
                target_id,
                actor=actor,
                include_warnings=False,
            )
            return
        if target_type == "agent_page":
            page = get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
            if (
                page is None
                or not actor_can_read_agent_page(page, actor)
                or (
                    page.current_active_version_id is None
                    and not actor_can_read_drafts(actor)
                )
            ):
                raise ReadNotFoundError("target not found")
            return
        if target_type == "library_item":
            self.get_library_item(
                session,
                workspace_id,
                target_id,
                actor=actor,
            )
            return
        raise ReadNotFoundError("target not found")

    def _version_entries(
        self,
        session: Session,
        workspace_id: UUID,
        target_type: str,
        target_id: UUID,
        limit: int,
        actor: ActorContext | None,
        before_version_no: int | None = None,
    ) -> list[VersionEntry]:
        if target_type == "context_page":
            page = get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
            if page is None:
                return []
            stmt = (
                select(ContextPageVersion)
                .join(ContextPage, ContextPageVersion.page_id == ContextPage.id)
                .where(ContextPageVersion.page_id == target_id)
                .where(ContextPage.workspace_id == workspace_id)
            )
            if not actor_can_read_drafts(actor):
                stmt = stmt.where(
                    ContextPageVersion.version_status.in_(
                        PUBLISHED_CONTEXT_VERSION_STATUSES
                    )
                )
            if before_version_no is not None:
                stmt = stmt.where(ContextPageVersion.version_no < before_version_no)
            versions = session.scalars(
                stmt.order_by(ContextPageVersion.version_no.desc()).limit(limit)
            ).all()
            versions = [
                version
                for version in versions
                if context_version_is_visible(version, actor)
            ]
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
                            ContextPageVersion.id.in_(parent_ids)
                        )
                    )
                }
                if parent_ids
                else {}
            )
            review_metadata = context_version_review_metadata(
                session, versions, workspace_id=workspace_id
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

            entries: list[VersionEntry] = []
            for version in versions:
                prior_relation, visible_parent_id = context_parent_projection(
                    version,
                    parents_by_id.get(version.parent_version_id),
                    actor,
                )
                projection = project_context_version(
                    page,
                    version,
                    visible_version_id=visible_head_id,
                    expose_draft_state=expose_draft_state,
                )
                entries.append(
                    VersionEntry(
                        version_id=version.id,
                        version_no=version.version_no,
                        version_status=version.version_status,
                        review_disposition=review_metadata[version.id].disposition,
                        history_status=projection.history_status,
                        graph_status=projection.graph_status,
                        actor_type=version.actor_type,
                        actor_display_name=version.actor_display_name
                        or actor_names[(version.actor_type, version.actor_id)],
                        created_at=version.created_at,
                        parent_version_id=visible_parent_id,
                        prior_version_id=visible_parent_id,
                        prior_relation=prior_relation,
                        actor_id=version.actor_id,
                        is_current=(
                            version.id == page.current_version_id
                            if expose_draft_state
                            else projection.is_visible_head
                        ),
                        is_current_published=(
                            version.id == page.current_published_version_id
                        ),
                        is_latest_draft=(
                            expose_draft_state
                            and version.id == page.latest_draft_version_id
                        ),
                        is_visible_head=projection.is_visible_head,
                        is_published_head=projection.is_published_head,
                        is_draft_head=projection.is_draft_head,
                        review_status=review_metadata[version.id].status,
                        review_item_id=review_metadata[version.id].review_item_id,
                        is_alternative=projection.is_alternative,
                        is_discarded=projection.is_discarded,
                        merged_into_version_id=(
                            version.merged_into_version_id
                            if expose_draft_state
                            else None
                        ),
                        reconciliation_available=(
                            expose_draft_state
                            and verified_reconciliation_branch_is_available(
                                session,
                                page=page,
                                version=version,
                                actor_kind=(
                                    actor.actor_kind if actor is not None else None
                                ),
                                actor_id=actor.actor_id if actor is not None else None,
                            )
                        ),
                    )
                )
            return entries
        if target_type == "agent_page":
            page = get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
            if page is None or not actor_can_read_agent_page(page, actor):
                return []
            current_version_id = (
                page.latest_draft_version_id or page.current_active_version_id
            )
            stmt = select(AgentPageVersion).where(
                AgentPageVersion.agent_page_id == target_id
            )
            if not actor_can_read_drafts(actor):
                stmt = stmt.where(AgentPageVersion.version_status != "draft")
            if before_version_no is not None:
                stmt = stmt.where(AgentPageVersion.version_no < before_version_no)
            versions = session.scalars(
                stmt.order_by(AgentPageVersion.version_no.desc()).limit(limit)
            ).all()
            versions = [
                version
                for version in versions
                if agent_version_is_visible(page, version, actor)
            ]
            return [
                VersionEntry(
                    version_id=version.id,
                    version_no=version.version_no,
                    version_status=version.version_status,
                    actor_type=version.actor_type,
                    actor_display_name=version.actor_display_name
                    or resolve_actor_display_name(
                        session,
                        actor_kind=version.actor_type,
                        actor_id=version.actor_id,
                    ),
                    created_at=version.created_at,
                    actor_id=version.actor_id,
                    is_current=version.id == current_version_id,
                )
                for version in versions
            ]
        if target_type == "library_item":
            versions = session.scalars(
                select(LibraryItemVersion)
                .join(LibraryItem, LibraryItemVersion.library_item_id == LibraryItem.id)
                .where(LibraryItemVersion.library_item_id == target_id)
                .where(LibraryItem.workspace_id == workspace_id)
                .order_by(LibraryItemVersion.version_no.desc())
                .limit(limit)
            ).all()
            return [
                VersionEntry(
                    version_id=version.id,
                    version_no=version.version_no,
                    version_status=version.processing_status,
                    actor_type="system",
                    created_at=version.created_at,
                    prior_version_id=version.previous_version_id,
                )
                for version in versions
            ]
        return []

    @staticmethod
    def _change_set_entry(change_set: ChangeSet) -> ChangeSetEntry:
        return ChangeSetEntry(
            change_set_id=change_set.id,
            actor_type=change_set.actor_type,
            actor_id=change_set.actor_id,
            reason=change_set.reason,
            status=change_set.status,
            risk_level=change_set.risk_level,
            created_at=change_set.created_at,
        )

    @staticmethod
    def _review_decision_entry(decision: ReviewDecision) -> ReviewDecisionEntry:
        return ReviewDecisionEntry(
            decision_id=decision.id,
            decision_type=decision.decision_type,
            actor_type=decision.actor_type,
            decision_reason=decision.decision_reason,
            created_at=decision.created_at,
        )

    @staticmethod
    def _diff_entries(
        session: Session,
        changesets: list[ChangeSet],
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for change_set in changesets:
            operations = session.scalars(
                select(ChangeOperation).where(
                    ChangeOperation.change_set_id == change_set.id
                )
            ).all()
            for operation in operations:
                entries.append(
                    {
                        "change_set_id": str(change_set.id),
                        "operation": operation.operation,
                        "target_type": operation.target_type,
                        "target_id": str(operation.target_id),
                        "diff_summary": operation.diff_summary,
                        "diff_ref": operation.diff_ref,
                    }
                )
        return entries
