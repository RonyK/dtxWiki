"""Actor-filtered link projections for authenticated and public page readers."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage
from backend.models.ask import AskSession
from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.models.library import LibraryItem
from backend.models.mcp_chat import McpChatSession
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.repositories.workspace_scoped import get_in_workspace, select_in_workspace
from backend.schemas.read import ContextLinkTarget, WikilinkTarget
from backend.services.actor_visible_read import agent_page_visible, library_item_visible
from backend.services.context_links import (
    LinkContext,
    classify,
    configured_self_origins,
    extract_links,
    resolve,
)
from backend.services.library_canonical import canonical_library_artifact_lookup
from backend.services.public_page_exposure import valid_public_exposed_version_id
from backend.services.read_errors import ReadServiceError
from backend.services.tree_read import TreeReadService
from backend.services.workspace_urls import (
    agent_page_url,
    library_artifact_url,
    library_item_url,
    review_item_url,
    workspace_path,
)

if TYPE_CHECKING:
    from backend.policy.auth import ActorContext


def page_link_target_condition(page: ContextPage):
    """Only unresolved slug locators may follow a target created afterwards."""
    slug_locator = or_(
        *(
            PageLink.locator_key.startswith(kind + ":")
            for kind in ("wikilink", "concept_path", "slug", "resource_uri")
        )
    )
    return (PageLink.target_page_id == page.id) | (
        PageLink.target_page_id.is_(None)
        & (PageLink.target_slug == page.slug)
        & slug_locator
    )


def context_link_targets(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
    actor: ActorContext | None = None,
    public: bool = False,
    workspace: Workspace | None = None,
    self_origins: frozenset[str] | None = None,
) -> list[ContextLinkTarget]:
    context = LinkContext(
        session=session,
        workspace_id=page.workspace_id,
        actor=None if public else actor,
        source_page_id=page.id,
        source_version_id=version.id,
        source_is_published=version.id == page.current_published_version_id,
        self_origins=(
            configured_self_origins() if self_origins is None else self_origins
        ),
    )
    targets: list[ContextLinkTarget] = []
    seen: set[str] = set()
    for occurrence in extract_links(version.body_md):
        if occurrence.href in seen:
            continue
        seen.add(occurrence.href)
        locator = classify(occurrence.href, context.self_origins)
        handled, href = _server_navigation_target(
            session,
            occurrence.href,
            workspace_id=page.workspace_id,
            actor=None if public else actor,
            public=public,
            self_origins=context.self_origins,
        )
        if handled:
            targets.append(
                ContextLinkTarget(
                    locator=occurrence.href,
                    href=href,
                    state="ok" if href is not None else "unavailable",
                )
            )
            continue
        resolution = resolve(locator, context)
        if resolution.state == "ignored":
            continue
        # Authenticated foreign full links navigate directly without probing
        # another Workspace. Public readers cannot inherit that authority.
        if (
            resolution.state == "foreign"
            and not public
            and locator.kind in {"stable_full", "canonical_route"}
        ):
            continue
        target_id = resolution.page_id if resolution.state == "ok" else None
        if locator.fragment and locator.fragment.lower().startswith(
            ("claim-", "artifact-")
        ):
            locator = replace(locator, fragment=None)
            resolution = resolve(locator, context)
            target_id = resolution.page_id if resolution.state == "ok" else None
        href = resolution.href if target_id is not None else None
        if public and target_id is not None:
            target = get_in_workspace(
                session,
                ContextPage,
                workspace_id=page.workspace_id,
                resource_id=target_id,
            )
            exposed_id = (
                valid_public_exposed_version_id(target, workspace)
                if target is not None and workspace is not None
                else None
            )
            exposed = (
                session.scalar(
                    select_in_workspace(
                        ContextPageVersion,
                        workspace_id=page.workspace_id,
                        conditions=(
                            ContextPageVersion.page_id == target_id,
                            ContextPageVersion.id == exposed_id,
                            ContextPageVersion.version_status == "published_snapshot",
                        ),
                    )
                )
                if exposed_id is not None
                else None
            )
            href = (
                f"/w/{quote(workspace.slug, safe='')}/pages/"
                f"{quote(target.slug, safe='')}"
                + (f"#{locator.fragment}" if locator.fragment else "")
                if exposed is not None
                else None
            )
        targets.append(
            ContextLinkTarget(
                locator=occurrence.href,
                state="ok" if href is not None else "unavailable",
                href=href,
                target_page_id=target_id if href is not None and not public else None,
            )
        )
    return targets


def ask_context_link_targets(
    session: Session,
    *,
    body: str,
    workspace_id: UUID,
    actor: ActorContext | None,
) -> list[ContextLinkTarget]:
    """Project only server-confirmed internal links from an untrusted answer."""
    context = LinkContext(session=session, workspace_id=workspace_id, actor=actor)
    targets: list[ContextLinkTarget] = []
    seen: set[str] = set()
    target_kinds = {
        "page_id", "stable_full", "stable_short", "canonical_route",
        "nav_target", "concept_path", "slug", "wikilink", "resource_uri",
    }
    for occurrence in extract_links(body):
        if occurrence.href in seen:
            continue
        seen.add(occurrence.href)
        locator = classify(occurrence.href)
        handled, href = _server_navigation_target(
            session,
            occurrence.href,
            workspace_id=workspace_id,
            actor=actor,
            self_origins=configured_self_origins(),
        )
        if handled:
            targets.append(
                ContextLinkTarget(
                    locator=occurrence.href,
                    href=href,
                    state="ok" if href else "unavailable",
                )
            )
            continue
        if locator.kind not in target_kinds:
            continue
        resolution = resolve(locator, context)
        available = resolution.state == "ok" and resolution.href is not None
        targets.append(
            ContextLinkTarget(
                locator=occurrence.href,
                href=resolution.href if available else None,
                target_page_id=resolution.page_id if available else None,
                state="ok" if available else "unavailable",
            )
        )
    return targets


def _server_navigation_target(
    session: Session,
    href: str,
    *,
    workspace_id: UUID,
    actor: ActorContext | None,
    public: bool = False,
    self_origins: frozenset[str] = frozenset(),
) -> tuple[bool, str | None]:
    """Confirm non-Context browser destinations in their proven source workspace."""
    reference = _parse_workspace_navigation_reference(href, self_origins=self_origins)
    if reference is None:
        if _looks_like_workspace_navigation(href, self_origins=self_origins):
            return True, None
        return False, None
    section, kind, target_id, explicit_workspace_id = reference
    if explicit_workspace_id is not None and explicit_workspace_id != workspace_id:
        return True, None
    if public:
        if section == "context" and kind == "legacy_target":
            # Context's resolver must still apply pins and public exposure to
            # legacy page/placement links. Authenticated folder destinations
            # remain unavailable to the anonymous reader.
            return False, None
        # The public reader never inherits authenticated SPA authority.
        return True, None

    if section in {"review", "ask", "lint", "chats"}:
        if section == "chats" and not _actor_can_edit_workspace(actor):
            return True, None
        if section == "review":
            from backend.services.review_service import ReviewService

            try:
                ReviewService().get_review(
                    session,
                    workspace_id,
                    target_id,
                    actor_user_id=actor.user_id if actor else None,
                    actor_role=(
                        (actor.role or actor.workspace_role) if actor else "viewer"
                    ),
                    actor=actor,
                )
            except ReadServiceError:
                return True, None
            return True, review_item_url(workspace_id, target_id)
        if section == "ask":
            if (
                get_in_workspace(
                    session,
                    AskSession,
                    workspace_id=workspace_id,
                    resource_id=target_id,
                )
                is None
            ):
                return True, None
            return True, f"{workspace_path(workspace_id)}/ask/{target_id}"
        if section == "chats":
            if (
                get_in_workspace(
                    session,
                    McpChatSession,
                    workspace_id=workspace_id,
                    resource_id=target_id,
                )
                is None
            ):
                return True, None
            return True, f"{workspace_path(workspace_id)}/chats/{target_id}"
        try:
            from backend.services.lint_service import LintService

            result = LintService().list_findings(
                session, workspace_id, finding_id=target_id, actor=actor, limit=1
            )
        except (ReadServiceError, ValueError):
            return True, None
        if not result.findings:
            return True, None
        return True, f"{workspace_path(workspace_id)}/lint/findings/{target_id}"

    if kind == "folder":
        node = get_in_workspace(
            session,
            WorkspaceTreeNode,
            workspace_id=workspace_id,
            resource_id=target_id,
        )
        if (
            node is None
            or node.section != section
            or node.node_kind != "folder"
            or actor is None
            or not TreeReadService.node_visible_to_actor(session, node, actor)
        ):
            return True, None
        return True, f"{workspace_path(workspace_id)}/{section}/folders/{node.id}"

    if kind == "legacy_target":
        if section == "context":
            # Let the Context link resolver retain locator pin precedence for
            # real page and page-placement targets. A plain folder node has no
            # page pin to honor and gets its typed destination here.
            if get_in_workspace(
                session, ContextPage, workspace_id=workspace_id, resource_id=target_id
            ) is not None:
                return False, None
            node = get_in_workspace(
                session,
                WorkspaceTreeNode,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
            if node is None or node.section != "context":
                return False, None
            if node.node_kind != "folder":
                return False, None
            if (
                public
                or actor is None
                or not TreeReadService.node_visible_to_actor(session, node, actor)
            ):
                return True, None
            return True, f"{workspace_path(workspace_id)}/context/folders/{node.id}"
        # Legacy target ids were usually domain ids; only if no domain record
        # matches do we inspect a same-workspace tree node and honor its kind.
        if section == "library":
            item = get_in_workspace(
                session, LibraryItem, workspace_id=workspace_id, resource_id=target_id
            )
            if item is not None:
                return True, (
                    library_item_url(workspace_id, item.id)
                    if library_item_visible(item, actor, workspace_id=workspace_id)
                    else None
                )
            artifact_ref = canonical_library_artifact_lookup(
                session, workspace_id=workspace_id, artifact_id=target_id
            )
            if artifact_ref is not None:
                item, _version, _artifact = artifact_ref
                return True, (
                    library_artifact_url(workspace_id, target_id)
                    if library_item_visible(item, actor, workspace_id=workspace_id)
                    else None
                )
        else:
            page = get_in_workspace(
                session, AgentPage, workspace_id=workspace_id, resource_id=target_id
            )
            if page is not None:
                return True, (
                    agent_page_url(workspace_id, page.id)
                    if agent_page_visible(page, actor, workspace_id=workspace_id)
                    else None
                )
        node = get_in_workspace(
            session,
            WorkspaceTreeNode,
            workspace_id=workspace_id,
            resource_id=target_id,
        )
        if (
            node is None
            or node.section != section
            or actor is None
            or not TreeReadService.node_visible_to_actor(session, node, actor)
        ):
            return True, None
        if node.node_kind == "folder":
            return True, f"{workspace_path(workspace_id)}/{section}/folders/{node.id}"
        if (
            section == "library"
            and node.target_kind == "library_item"
            and node.target_id
        ):
            item = get_in_workspace(
                session,
                LibraryItem,
                workspace_id=workspace_id,
                resource_id=node.target_id,
            )
            return True, (
                library_item_url(workspace_id, item.id)
                if library_item_visible(item, actor, workspace_id=workspace_id)
                else None
            )
        if (
            section == "library"
            and node.target_kind == "library_artifact"
            and node.target_id
        ):
            artifact_ref = canonical_library_artifact_lookup(
                session, workspace_id=workspace_id, artifact_id=node.target_id
            )
            if artifact_ref is None:
                return True, None
            item, _version, _artifact = artifact_ref
            return True, (
                library_artifact_url(workspace_id, node.target_id)
                if library_item_visible(item, actor, workspace_id=workspace_id)
                else None
            )
        if section == "agent" and node.target_kind == "agent_page" and node.target_id:
            page = get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=node.target_id,
            )
            return True, (
                agent_page_url(workspace_id, page.id)
                if agent_page_visible(page, actor, workspace_id=workspace_id)
                else None
            )
        return True, None

    if section == "library" and kind == "item":
        item = get_in_workspace(
            session, LibraryItem, workspace_id=workspace_id, resource_id=target_id
        )
        return True, (
            library_item_url(workspace_id, item.id)
            if library_item_visible(item, actor, workspace_id=workspace_id)
            else None
        )
    if section == "library" and kind == "artifact":
        artifact_ref = canonical_library_artifact_lookup(
            session, workspace_id=workspace_id, artifact_id=target_id
        )
        if artifact_ref is None:
            return True, None
        item, _version, _artifact = artifact_ref
        return True, (
            library_artifact_url(workspace_id, target_id)
            if library_item_visible(item, actor, workspace_id=workspace_id)
            else None
        )
    if section == "agent" and kind == "page":
        page = get_in_workspace(
            session, AgentPage, workspace_id=workspace_id, resource_id=target_id
        )
        return True, (
            agent_page_url(workspace_id, page.id)
            if agent_page_visible(page, actor, workspace_id=workspace_id)
            else None
        )
    return False, None


def _actor_can_edit_workspace(actor: ActorContext | None) -> bool:
    if actor is None:
        return False
    from backend.policy.rbac import role_allows

    try:
        return role_allows(actor.role or actor.workspace_role, "editor")
    except ValueError:
        return False


def _parse_workspace_navigation_reference(
    href: str,
    *,
    self_origins: frozenset[str] = frozenset(),
) -> tuple[str, str, UUID, UUID | None] | None:
    try:
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            locator = classify(href, self_origins)
            if not locator.absolute:
                return None
            href = f"{parts.path}?{parts.query}" if parts.query else parts.path
            parts = urlsplit(href)
        if parts.scheme or parts.netloc or parts.username or parts.password:
            return None
        legacy_sections = {
            "/context": "context",
            "/library": "library",
            "/agent": "agent",
            "/review": "review",
            "/ask": "ask",
            "/lint": "lint",
        }
        if parts.path in legacy_sections:
            query = parse_qs(parts.query, strict_parsing=True)
            expected_query = {
                "context": "target", "library": "target", "agent": "target",
                "review": "review", "ask": "target", "lint": "finding",
            }[legacy_sections[parts.path]]
            if set(query) != {expected_query} or len(query[expected_query]) != 1:
                return None
            return (
                legacy_sections[parts.path],
                "legacy_target",
                UUID(query[expected_query][0]),
                None,
            )
        chat_match = re.fullmatch(r"/chats/([0-9a-fA-F-]{36})", parts.path)
        if chat_match is not None and not parts.query:
            return "chats", "legacy_target", UUID(chat_match[1]), None
        if parts.query:
            return None
        match = re.fullmatch(
            r"/workspaces/([0-9a-fA-F-]{36})/(context|library|agent)/(items|artifacts|pages|folders)/([0-9a-fA-F-]{36})",
            parts.path,
        )
        if match is not None:
            explicit_workspace_id = UUID(match[1])
            section = match[2]
            route_kind = match[3]
            kind = {
                "items": "item",
                "artifacts": "artifact",
                "pages": "page",
                "folders": "folder",
            }[route_kind]
            if (section == "library" and kind == "page") or (
                section == "agent" and kind in {"item", "artifact"}
            ) or (section == "context" and kind != "folder"):
                return None
            return section, kind, UUID(match[4]), explicit_workspace_id
        match = re.fullmatch(
            r"/workspaces/([0-9a-fA-F-]{36})/(review|ask|chats)/([0-9a-fA-F-]{36})|/workspaces/([0-9a-fA-F-]{36})/lint/findings/([0-9a-fA-F-]{36})",
            parts.path,
        )
        if match is not None:
            if match[1] is not None:
                return match[2], "item", UUID(match[3]), UUID(match[1])
            return "lint", "item", UUID(match[5]), UUID(match[4])
    except (ValueError, UnicodeError):
        return None


def _looks_like_workspace_navigation(
    href: str, *, self_origins: frozenset[str]
) -> bool:
    """Recognize malformed internal route references without probing targets."""
    try:
        locator = classify(href, self_origins)
        if locator.kind in {"canonical_route", "stable_full", "stable_short"}:
            # Valid Context page links belong to the established Context
            # resolver, which preserves pin, publication, and foreign-link
            # handling. This guard only catches malformed product routes.
            return False
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            if not classify(href, self_origins).absolute:
                return False
            path = parts.path
        else:
            path = parts.path
        return (
            path in {"/context", "/library", "/agent", "/review", "/ask", "/lint"}
            or path == "/chats"
            or path.startswith("/chats/")
            or path.startswith("/workspaces/")
        )
    except ValueError:
        return False


def legacy_wikilink_targets(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
    actor: ActorContext | None,
) -> list[WikilinkTarget]:
    """Keep the legacy reader projection without bypassing target visibility."""
    context = LinkContext(
        session=session,
        workspace_id=page.workspace_id,
        actor=actor,
        source_page_id=page.id,
        source_version_id=version.id,
    )
    links = session.scalars(
        select_in_workspace(
            PageLink,
            workspace_id=page.workspace_id,
            conditions=(
                PageLink.source_page_id == page.id,
                PageLink.source_version_id == version.id,
                PageLink.locator_key.startswith("wikilink:"),
                PageLink.target_slug.is_not(None),
            ),
        ).order_by(PageLink.target_slug)
    )
    result = []
    for link in links:
        resolution = resolve(classify(f"[[{link.target_slug}]]"), context)
        visible = resolution.state == "ok"
        result.append(
            WikilinkTarget(
                target_slug=link.target_slug,
                target_page_id=resolution.page_id if visible else None,
                current_slug=resolution.target_slug if visible else None,
            )
        )
    return result
