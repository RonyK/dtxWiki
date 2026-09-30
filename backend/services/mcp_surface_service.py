"""Model access for the MCP prompts/resources surface.

The MCP protocol layer (``backend.mcp.protocol.prompts_resources``) must not
import ``backend.models`` directly (architecture boundary). This service owns
the AgentPage / ContextPage / Workspace queries and returns plain DTOs so the
protocol layer only builds MCP types and applies role/scope gates.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.actor_visible_read import (
    actor_can_read_agent_page,
    agent_page_requires_sensitive_access,
)
from backend.services.context_visibility import visible_context_version_id
from backend.utils.uuid import parse_uuid_or_none


class McpSurfaceNotFoundError(RuntimeError):
    """Raised when a requested AgentPage cannot be resolved."""

    def __init__(self, error_code: str = "agent_page_not_found") -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True)
class AgentPageRef:
    slug: str
    title: str
    category: str
    requires_sensitive_access: bool


@dataclass(frozen=True)
class AgentPageMeta:
    """AgentPage identity/category without the (possibly large) version body.

    Lets callers run scope/owner gates before loading the body (so a denied
    sensitive read never materializes it), and avoids a None body/version
    convention on the body-bearing AgentPageContent below.
    """

    page_id: UUID
    category: str
    title: str
    version_id: UUID | None
    requires_sensitive_access: bool


@dataclass(frozen=True)
class AgentPageContent:
    page_id: UUID
    category: str
    title: str
    version_id: UUID
    body: str
    requires_sensitive_access: bool


@dataclass(frozen=True)
class ContextPageContent:
    page_id: UUID
    page_type: str
    version_id: UUID
    body: str


def list_agent_page_refs(
    session,
    workspace_id: UUID,
    *,
    categories: Iterable[str] | None = None,
    actor: ActorContext | None = None,
    limit: int | None = None,
) -> list[AgentPageRef]:
    statement = (
        select(AgentPage)
        .where(AgentPage.workspace_id == workspace_id)
        .where(AgentPage.lifecycle_status == "active")
    )
    if categories is not None:
        category_tuple = tuple(categories)
        if not category_tuple:
            # An explicit empty category set matches nothing; return early
            # rather than emitting `IN ()` (a no-match + SAWarning) (B#1).
            return []
        statement = statement.where(AgentPage.category.in_(category_tuple))
    statement = statement.order_by(
        AgentPage.category, AgentPage.priority, AgentPage.slug
    )
    # M-05: the MCP resources/prompts LIST must advertise a BOUNDED number of
    # entry points, not the whole per-page catalog. `limit` caps the visible
    # rows for those surfaces; category rollups + the slug template keep the
    # untruncated set reachable. Actor visibility is applied per row, so
    # over-fetch a small margin before slicing so a page of visible refs is
    # still returned when the newest rows are sensitive.
    if limit is not None:
        statement = statement.limit(limit * 4)
    refs = [
        AgentPageRef(
            slug=page.slug,
            title=page.title,
            category=page.category,
            requires_sensitive_access=agent_page_requires_sensitive_access(page),
        )
        for page in session.scalars(statement).all()
        if actor_can_read_agent_page(page, actor)
    ]
    if limit is not None:
        return refs[:limit]
    return refs


def workspace_title(session, workspace_id: UUID) -> str | None:
    workspace = session.get(Workspace, workspace_id)
    return workspace.title if workspace is not None else None


def workspace_agent_injection_policy(session, workspace_id: UUID) -> dict | None:
    workspace = session.get(Workspace, workspace_id)
    return workspace.agent_injection_policy if workspace is not None else None


def resolve_agent_page_meta(
    session,
    workspace_id: UUID,
    *,
    agent_page_id: UUID | None,
    slug: str | None,
) -> AgentPageMeta:
    """Resolve an active AgentPage's identity/category WITHOUT its body.

    Callers gate on category (scope/owner) and only then call
    load_agent_page_body, so a denied sensitive read never loads the body (B#8).
    """
    if agent_page_id is not None:
        page = session.get(AgentPage, agent_page_id)
    elif slug:
        page = session.scalar(
            select(AgentPage).where(
                AgentPage.workspace_id == workspace_id,
                AgentPage.slug == slug,
            )
        )
    else:
        raise ValueError("agent_page_id or slug is required")
    if (
        page is None
        or page.workspace_id != workspace_id
        or page.lifecycle_status != "active"
    ):
        raise McpSurfaceNotFoundError("agent_page_not_found")
    return AgentPageMeta(
        page_id=page.id,
        category=page.category,
        title=page.title,
        version_id=page.current_active_version_id,
        requires_sensitive_access=agent_page_requires_sensitive_access(page),
    )


def load_agent_page_body(session, version_id: UUID | None) -> str | None:
    if version_id is None:
        return None
    version = session.get(AgentPageVersion, version_id)
    return version.body_markdown if version is not None else None


def read_agent_pages_by_category(
    session,
    workspace_id: UUID,
    category: str,
    actor: ActorContext | None = None,
) -> list[AgentPageContent]:
    pages = session.scalars(
        select(AgentPage)
        .where(AgentPage.workspace_id == workspace_id)
        .where(AgentPage.category == category)
        .where(AgentPage.lifecycle_status == "active")
        .order_by(AgentPage.priority, AgentPage.slug)
    ).all()
    # Batch-load active versions in one query instead of a per-page
    # session.get (N+1) (C5).
    version_ids = [
        page.current_active_version_id
        for page in pages
        if page.current_active_version_id is not None
    ]
    versions_by_id: dict[UUID, AgentPageVersion] = {}
    if version_ids:
        versions_by_id = {
            version.id: version
            for version in session.scalars(
                select(AgentPageVersion).where(AgentPageVersion.id.in_(version_ids))
            ).all()
        }
    contents: list[AgentPageContent] = []
    for page in pages:
        if not actor_can_read_agent_page(page, actor):
            continue
        version = (
            versions_by_id.get(page.current_active_version_id)
            if page.current_active_version_id is not None
            else None
        )
        if version is None:
            continue
        contents.append(
            AgentPageContent(
                page_id=page.id,
                category=page.category,
                title=page.title,
                version_id=version.id,
                body=version.body_markdown,
                requires_sensitive_access=agent_page_requires_sensitive_access(page),
            )
        )
    return contents


def read_context_page_content(
    session,
    workspace_id: UUID,
    identifier: str,
    actor: ActorContext | None,
) -> ContextPageContent | None:
    page = _resolve_context_page(session, workspace_id, identifier)
    if page is None or page.lifecycle_status == "archived":
        return None
    version_id, _is_draft = visible_context_version_id(page, actor)
    if version_id is None:
        return None
    version = session.get(ContextPageVersion, version_id)
    if version is None:
        return None
    return ContextPageContent(
        page_id=page.id,
        page_type=page.page_type,
        version_id=version.id,
        body=version.body_markdown,
    )


def _resolve_context_page(
    session,
    workspace_id: UUID,
    identifier: str,
) -> ContextPage | None:
    page_id = parse_uuid_or_none(identifier)
    if page_id is not None:
        page = session.get(ContextPage, page_id)
    else:
        page = session.scalar(
            select(ContextPage).where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.slug == identifier,
            )
        )
    if page is None or page.workspace_id != workspace_id:
        return None
    return page
