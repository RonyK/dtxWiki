import difflib
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.domain.input_policy import InputPolicyError, validate_text, validate_title
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.base import utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.agent import (
    SENSITIVE_AGENT_PAGE_CATEGORIES,
    AgentPageActivateResult,
    AgentPageDraftRequest,
    AgentPageDraftResult,
)
from backend.schemas.input_policy import validate_request_inputs
from backend.schemas.page import PageVersionDiff
from backend.services.actor_visible_read import agent_version_is_visible
from backend.services.audit_service import AuditService
from backend.services.context_links import (
    LinkContext,
    editor_link_actor,
    rewrite_links_with_report,
)
from backend.services.db_write_intent import acquire_write_intent
from backend.services.read_errors import (
    ReadNotFoundError,
    ReadValidationError,
    RejectedByPolicyError,
    ScopeDeniedError,
    StaleBaseVersionError,
)
from backend.services.review_risk_policy import (
    ReviewRiskResolver,
    review_risk_snapshot,
)
from backend.services.tree_service import ensure_primary_tree_placement
from backend.spi.types import AI_PROVENANCES
from backend.utils.actor_display import resolve_actor_display_name
from backend.utils.content_recency import normalize_content_recency
from backend.utils.page_frontmatter import validate_agent_markdown_input
from backend.utils.uuid import parse_uuid_or_none

CATEGORY_PRIORITIES = {
    "agent_guide": 1,
    "persona": 2,
    "skill": 3,
    "memory": 4,
}
AGENT_PAGE_DIFF_MAX_INPUT_CHARS = 200_000
PROPOSED_PAGE_META_KEY = "__proposed_page_meta__"


def agent_page_by_id_for_update_statement(workspace_id: UUID, agent_page_id: UUID):
    return (
        select(AgentPage)
        .where(
            AgentPage.workspace_id == workspace_id,
            AgentPage.id == agent_page_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def agent_page_by_slug_for_update_statement(workspace_id: UUID, slug: str):
    return (
        select(AgentPage)
        .where(
            AgentPage.workspace_id == workspace_id,
            AgentPage.slug == slug,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


class AgentPageService:
    def __init__(self, audit_service: AuditService | None = None) -> None:
        self.audit_service = audit_service or AuditService()
        self.risk_resolver = ReviewRiskResolver()

    def page_exists(self, session: Session, workspace_id: UUID, slug: str) -> bool:
        return (
            session.scalar(
                select(AgentPage.id).where(
                    AgentPage.workspace_id == workspace_id,
                    AgentPage.slug == slug,
                )
            )
            is not None
        )

    def write_draft(
        self,
        session: Session,
        workspace_id: UUID,
        request: AgentPageDraftRequest,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        agent_page_id: UUID | None = None,
        origin_chat_session_id: UUID | None = None,
        actor: ActorContext | None = None,
        normalize_index_refs: bool = True,
    ) -> AgentPageDraftResult:
        # SQLite: FOR UPDATE is a no-op and pysqlite defers BEGIN until the
        # first DML, so take write intent before the page resolve and the
        # version_no allocation below. PostgreSQL keeps the narrower row lock.
        validate_request_inputs(request)
        acquire_write_intent(session)
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")

        page = self._resolve_or_create_page(
            session=session,
            workspace=workspace,
            request=request,
            actor_kind=actor_kind,
            actor_id=actor_id,
            agent_page_id=agent_page_id,
        )
        self._validate_base_version(
            session=session,
            page=page,
            base_version_id=request.base_version_id,
        )

        is_sensitive = (
            request.category in SENSITIVE_AGENT_PAGE_CATEGORIES
            or page.category in SENSITIVE_AGENT_PAGE_CATEGORIES
        )
        risk_resolution = self.risk_resolver.resolve(
            workspace=workspace,
            event=(
                "agent_sensitive_draft" if is_sensitive else "agent_non_sensitive_draft"
            ),
            signals={
                # Report who wrote it; the resolver decides what that costs.
                "ai_authored": actor_provenance in AI_PROVENANCES,
                "sensitive_agent_category": is_sensitive,
            },
            actor_provenance=actor_provenance,
        )
        operation_kind = (
            "create_draft" if page.latest_draft_version_id is None else ("update_draft")
        )
        change_set = ChangeSet(
            workspace_id=workspace_id,
            reason=request.reason,
            trigger_type="agent_page_draft",
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            # T15/F2: chat_owner review routing for agent-page drafts (lookup-
            # only; NULL on a conversation's first write, as for context drafts).
            origin_chat_session_id=origin_chat_session_id,
            status="draft_created",
            risk_level=risk_resolution.risk_level,
        )
        session.add(change_set)
        session.flush()

        # Index refs are system-internal; an Agent draft stores their
        # canonical links like a Context body does.
        body_markdown = request.body_markdown
        if normalize_index_refs:
            body_markdown, _, _ = rewrite_links_with_report(
                request.body_markdown,
                LinkContext(
                    session,
                    workspace_id,
                    editor_link_actor(
                        actor_kind,
                        actor_id,
                        workspace_id=workspace_id,
                        actor_context=actor,
                    ),
                ),
                index_refs_only=True,
            )
        # Canonical folder URLs can be longer than their authored index refs.
        validate_agent_markdown_input(body_markdown)
        version = AgentPageVersion(
            workspace_id=page.workspace_id,
            agent_page_id=page.id,
            version_no=self._next_version_no(session=session, page_id=page.id),
            body_markdown=body_markdown,
            frontmatter={
                **request.frontmatter,
                PROPOSED_PAGE_META_KEY: {
                    "category": request.category,
                    "title": request.title,
                    "sensitivity": request.sensitivity,
                },
            },
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            actor_display_name=resolve_actor_display_name(
                session,
                actor_kind=actor_kind,
                actor_id=actor_id,
            ),
            change_set_id=change_set.id,
            version_status="draft",
        )
        session.add(version)
        session.flush()

        page.latest_draft_version_id = version.id
        page.lifecycle_status = (
            "active" if page.current_active_version_id is not None else "draft"
        )
        ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace_id,
            target_kind="agent_page",
            target_id=page.id,
            title=page.title,
            slug=page.slug,
            section="agent",
            parent_id=request.parent_node_id,
            default_parent_slug=request.category,
            actor_kind=actor_kind,
            actor_id=actor_id,
            can_have_children=True,
        )
        from backend.services.indexing.fts5 import sync_agent_page_doc_sources

        sync_agent_page_doc_sources(session, page)
        session.flush()

        operation = ChangeOperation(
            workspace_id=change_set.workspace_id,
            change_set_id=change_set.id,
            target_type="agent_page",
            target_id=page.id,
            operation=operation_kind,
            diff_summary=request.reason,
            diff_ref=f"agent_page_version:{version.id}",
        )
        session.add(operation)

        review_item_id = None
        if risk_resolution.review_required:
            review = ReviewItem(
                workspace_id=workspace_id,
                change_set_id=change_set.id,
                review_type="agent_page_activation",
                status="pending",
                risk_level=risk_resolution.risk_level,
                **review_risk_snapshot(risk_resolution),
                decision_summary=(
                    "sensitive AgentPage draft requires owner activation review"
                ),
            )
            session.add(review)
            session.flush()
            review_item_id = review.id

        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action=f"agent_page.draft.{operation_kind.removesuffix('_draft')}",
            target_kind="agent_page",
            target_id=page.id,
            payload={
                "category": request.category,
                "slug": request.slug,
                "version_id": str(version.id),
                "change_set_id": str(change_set.id),
                "review_item_id": str(review_item_id) if review_item_id else None,
            },
            event_summary=request.reason,
            retention_class="sensitive" if is_sensitive else "standard",
        )
        change_set.status = (
            "needs_review" if risk_resolution.review_required else "draft_created"
        )
        session.flush()

        return AgentPageDraftResult(
            agent_page_id=page.id,
            draft_version_id=version.id,
            change_set_id=change_set.id,
            review_item_id=review_item_id,
            next_action=(
                "review_required"
                if risk_resolution.review_required
                else "owner_activation_required"
            ),
        )

    def activate(
        self,
        session: Session,
        workspace_id: UUID,
        agent_page_id: UUID,
        version_id: UUID,
        reason: str,
        actor_kind: str,
        actor_id: str,
        actor_role: str,
    ) -> AgentPageActivateResult:
        validate_text(reason, field="reason", max_length=2000)
        # SQLite: FOR UPDATE is a no-op, so without write intent two activations
        # of different drafts of this page both read the same
        # current_active_version_id, each supersede only that one, and both land
        # as active_snapshot -- no unique index catches it. Take the writer lock
        # before the read. PostgreSQL keeps the narrower row lock.
        acquire_write_intent(session)
        page = self._lock_page_by_id(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=agent_page_id,
        )
        if page is None:
            raise ReadNotFoundError("agent page not found")
        version = session.get(AgentPageVersion, version_id)
        if version is None or version.agent_page_id != page.id:
            raise ReadNotFoundError("agent page version not found")
        validate_agent_markdown_input(version.body_markdown)
        proposed_meta = dict(self._proposed_page_meta(version))
        if "title" in proposed_meta:
            proposed_meta["title"] = validate_title(proposed_meta["title"])
        proposed_category = proposed_meta.get("category", page.category)
        self._ensure_activation_allowed(
            page=page,
            proposed_category=proposed_category,
            actor_kind=actor_kind,
            actor_role=actor_role,
        )
        if version.version_status != "draft":
            raise RejectedByPolicyError("only draft AgentPage versions can activate")

        previous_active_version_id = page.current_active_version_id
        previous = None
        if previous_active_version_id is not None:
            previous = session.get(AgentPageVersion, previous_active_version_id)
            if previous is not None:
                previous.version_status = "superseded_snapshot"
        if previous is None or (
            normalize_content_recency(previous.body_markdown)
            != normalize_content_recency(version.body_markdown)
            or self._visible_frontmatter(previous) != self._visible_frontmatter(version)
        ):
            page.content_changed_at = utc_now()

        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        is_sensitive = (
            page.category in SENSITIVE_AGENT_PAGE_CATEGORIES
            or proposed_category in SENSITIVE_AGENT_PAGE_CATEGORIES
        )
        risk_resolution = self.risk_resolver.resolve(
            workspace=workspace,
            event=(
                "agent_sensitive_activation"
                if is_sensitive
                else "agent_non_sensitive_activation"
            ),
            signals={"sensitive_agent_category": is_sensitive},
        )
        change_set = ChangeSet(
            workspace_id=workspace_id,
            reason=reason,
            trigger_type="agent_page_activation",
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            status="applied",
            risk_level=risk_resolution.risk_level,
        )
        session.add(change_set)
        session.flush()

        version.version_status = "active_snapshot"
        version.activation_reason = reason
        if proposed_meta:
            page.category = proposed_meta.get("category", page.category)
            page.title = proposed_meta.get("title", page.title)
            page.sensitivity = proposed_meta.get("sensitivity", page.sensitivity)
            version.frontmatter = {
                key: value
                for key, value in version.frontmatter.items()
                if key != PROPOSED_PAGE_META_KEY
            }
            ensure_primary_tree_placement(
                session=session,
                workspace_id=workspace_id,
                target_kind="agent_page",
                target_id=page.id,
                title=page.title,
                slug=page.slug,
                section="agent",
                parent_id=None,
                default_parent_slug=page.category,
                actor_kind=actor_kind,
                actor_id=actor_id,
                can_have_children=True,
            )
        page.current_active_version_id = version.id
        if page.latest_draft_version_id == version.id:
            page.latest_draft_version_id = None
        page.lifecycle_status = "active"
        from backend.services.indexing.fts5 import sync_agent_page_doc_sources

        sync_agent_page_doc_sources(session, page)
        session.flush()

        operation = ChangeOperation(
            workspace_id=change_set.workspace_id,
            change_set_id=change_set.id,
            target_type="agent_page",
            target_id=page.id,
            operation="activate",
            diff_summary=reason,
            diff_ref=f"agent_page_version:{version.id}",
        )
        session.add(operation)
        audit_event = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="agent_page.activated",
            target_kind="agent_page",
            target_id=page.id,
            payload={
                "version_id": str(version.id),
                "previous_active_version_id": (
                    str(previous_active_version_id)
                    if previous_active_version_id
                    else None
                ),
                "change_set_id": str(change_set.id),
            },
            event_summary=reason,
            retention_class="sensitive" if is_sensitive else "standard",
        )
        session.flush()

        return AgentPageActivateResult(
            agent_page_id=page.id,
            active_version_id=version.id,
            previous_active_version_id=previous_active_version_id,
            change_set_id=change_set.id,
            audit_event_id=audit_event.id,
        )

    def diff_versions(
        self,
        session: Session,
        workspace_id: UUID,
        agent_page_id: UUID,
        from_version_id: UUID,
        to_version_id: UUID,
        actor: ActorContext | None = None,
    ) -> PageVersionDiff:
        page = self._require_page_in_workspace(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=agent_page_id,
        )
        from_version = self._require_version(
            session=session,
            page=page,
            version_id=from_version_id,
        )
        to_version = self._require_version(
            session=session,
            page=page,
            version_id=to_version_id,
        )
        # A diff serves both versions' full body. _require_version only checks
        # page membership, so gate each version with the canonical predicate
        # (sensitivity + draft) and reject archived pages — otherwise a viewer
        # could read an unreviewed draft body via the diff (Codex R14; parity
        # with the Context-page diff which gates via context_version_is_visible).
        if actor is not None and (
            page.lifecycle_status == "archived"
            or not agent_version_is_visible(page, from_version, actor)
            or not agent_version_is_visible(page, to_version, actor)
        ):
            raise ReadNotFoundError("agent page version not found")
        from_body = from_version.body_markdown
        to_body = to_version.body_markdown
        if len(from_body) + len(to_body) > AGENT_PAGE_DIFF_MAX_INPUT_CHARS:
            raise ReadValidationError("version bodies are too large to diff")
        lines = list(
            difflib.unified_diff(
                from_body.splitlines(),
                to_body.splitlines(),
                fromfile=f"v{from_version.version_no}",
                tofile=f"v{to_version.version_no}",
                lineterm="",
            )
        )
        return PageVersionDiff(
            from_version_id=from_version.id,
            to_version_id=to_version.id,
            from_version_no=from_version.version_no,
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
        )

    def restore_version(
        self,
        session: Session,
        workspace_id: UUID,
        agent_page_id: UUID,
        version_id: UUID,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
    ) -> AgentPageDraftResult:
        page = self._require_page_in_workspace(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=agent_page_id,
        )
        target = self._require_version(
            session=session,
            page=page,
            version_id=version_id,
        )
        if target.body_markdown is None:
            raise ReadValidationError("source version has no content")
        validate_agent_markdown_input(target.body_markdown)
        edit_base_version_id = (
            page.latest_draft_version_id or page.current_active_version_id
        )
        try:
            request = AgentPageDraftRequest(
                base_version_id=edit_base_version_id,
                body_markdown=target.body_markdown,
                category=page.category,
                frontmatter=dict(target.frontmatter or {}),
                reason=f"Restore Agent page v{target.version_no} as draft",
                sensitivity=page.sensitivity,
                slug=page.slug,
                title=page.title,
            )
        except ValidationError as exc:
            raise InputPolicyError(
                "stored Agent page violates current input policy"
            ) from exc
        return self.write_draft(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=page.id,
            request=request,
            actor_kind=actor_kind,
            actor_id=actor_id,
            actor_provenance=actor_provenance,
            normalize_index_refs=False,
        )

    def _resolve_or_create_page(
        self,
        session: Session,
        workspace: Workspace,
        request: AgentPageDraftRequest,
        actor_kind: str,
        actor_id: str,
        agent_page_id: UUID | None = None,
    ) -> AgentPage:
        if agent_page_id is not None:
            page = self._lock_page_by_id(
                session=session,
                workspace_id=workspace.id,
                agent_page_id=agent_page_id,
            )
            if page is None:
                raise ReadNotFoundError("agent page not found")
            if page.slug != request.slug:
                raise StaleBaseVersionError("request slug does not match AgentPage")
            return page
        page = self._lock_page_by_slug(
            session=session,
            workspace_id=workspace.id,
            slug=request.slug,
        )
        if page is not None:
            return page
        candidate = AgentPage(
            workspace_id=workspace.id,
            category=request.category,
            title=request.title,
            slug=request.slug,
            lifecycle_status="draft",
            injection_policy=self._default_injection_policy(
                workspace,
                request.category,
            ),
            priority=CATEGORY_PRIORITIES[request.category],
            sensitivity=request.sensitivity,
            created_by_actor_type=actor_kind,
            created_by_actor_id=parse_uuid_or_none(actor_id),
        )
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush()
            return candidate
        except IntegrityError:
            page = self._lock_page_by_slug(
                session=session,
                workspace_id=workspace.id,
                slug=request.slug,
            )
            if page is None:
                raise
            return page

    @staticmethod
    def _lock_page_by_id(
        session: Session,
        workspace_id: UUID,
        agent_page_id: UUID,
    ) -> AgentPage | None:
        return session.scalar(
            agent_page_by_id_for_update_statement(workspace_id, agent_page_id)
        )

    @staticmethod
    def _lock_page_by_slug(
        session: Session,
        workspace_id: UUID,
        slug: str,
    ) -> AgentPage | None:
        return session.scalar(
            agent_page_by_slug_for_update_statement(workspace_id, slug)
        )

    @staticmethod
    def _require_page_in_workspace(
        session: Session,
        workspace_id: UUID,
        agent_page_id: UUID,
    ) -> AgentPage:
        page = session.get(AgentPage, agent_page_id)
        if page is None or page.workspace_id != workspace_id:
            raise ReadNotFoundError("agent page not found")
        return page

    @staticmethod
    def _require_version(
        session: Session,
        page: AgentPage,
        version_id: UUID,
    ) -> AgentPageVersion:
        version = session.get(AgentPageVersion, version_id)
        if version is None or version.agent_page_id != page.id:
            raise ReadNotFoundError("agent page version not found")
        return version

    @staticmethod
    def _visible_frontmatter(version: AgentPageVersion) -> dict[str, Any]:
        return {
            key: value
            for key, value in (version.frontmatter or {}).items()
            if key not in {PROPOSED_PAGE_META_KEY, "title", "slug"}
        }

    @staticmethod
    def _proposed_page_meta(version: AgentPageVersion) -> dict[str, Any]:
        meta = (version.frontmatter or {}).get(PROPOSED_PAGE_META_KEY)
        return meta if isinstance(meta, dict) else {}

    @staticmethod
    def _validate_base_version(
        session: Session,
        page: AgentPage,
        base_version_id: UUID | None,
    ) -> None:
        if base_version_id is None:
            return
        base_version = session.get(AgentPageVersion, base_version_id)
        if base_version is None or base_version.agent_page_id != page.id:
            raise StaleBaseVersionError("base version does not belong to AgentPage")
        current_edit_base = (
            page.latest_draft_version_id or page.current_active_version_id
        )
        if current_edit_base is not None and base_version_id != current_edit_base:
            raise StaleBaseVersionError("base version is stale")

    @staticmethod
    def _next_version_no(session: Session, page_id: UUID) -> int:
        return (
            session.scalar(
                select(
                    func.coalesce(func.max(AgentPageVersion.version_no), 0) + 1
                ).where(AgentPageVersion.agent_page_id == page_id)
            )
            or 1
        )

    @staticmethod
    def _default_injection_policy(workspace: Workspace, category: str) -> str:
        return workspace.agent_injection_policy.get(
            "category_inclusion_policy",
            {},
        ).get(category, "task_matched")

    @staticmethod
    def _ensure_activation_allowed(
        page: AgentPage,
        proposed_category: str,
        actor_kind: str,
        actor_role: str,
    ) -> None:
        if actor_kind in {"mcp_client", "agent"}:
            raise ScopeDeniedError(
                "MCP clients and agent actors cannot activate AgentPages"
            )
        if (
            page.category in SENSITIVE_AGENT_PAGE_CATEGORIES
            or proposed_category in SENSITIVE_AGENT_PAGE_CATEGORIES
        ):
            if actor_role != "owner":
                raise ScopeDeniedError("sensitive AgentPages require owner activation")
            return
        if actor_role not in {"owner", "admin"}:
            raise ScopeDeniedError("AgentPage activation requires owner/admin role")
