"""Actor-filtered link projections for authenticated and public page readers."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage
from backend.models.ask import AskSession
from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.mcp_chat import McpChatSession
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.repositories.workspace_scoped import get_in_workspace, select_in_workspace
from backend.schemas.read import ContextLinkTarget, WikilinkTarget
from backend.services.actor_visible_read import agent_page_visible, library_item_visible
from backend.services.context_links import (
    LinkContext,
    LinkResolutionBatch,
    classify,
    configured_self_origins,
    extract_links,
    resolve,
)
from backend.services.library_canonical import (
    canonical_library_artifact_lookup,
    canonical_library_artifact_lookup_many,
)
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


@dataclass(frozen=True)
class _VisibleTreeNodes:
    nodes_by_id: dict[UUID, WorkspaceTreeNode]
    visible_ids: set[UUID]

    def node(self, node_id: UUID) -> WorkspaceTreeNode | None:
        return self.nodes_by_id.get(node_id)

    def visible(self, node_id: UUID) -> bool:
        return node_id in self.visible_ids


@dataclass(frozen=True)
class _NavigationBatch:
    library_items: dict[UUID, LibraryItem]
    library_artifacts: dict[
        UUID, tuple[LibraryItem, LibraryItemVersion, LibraryArtifact] | None
    ]
    agent_pages: dict[UUID, AgentPage]
    ask_sessions: set[UUID]
    chat_sessions: set[UUID]
    review_ids: set[UUID]
    finding_ids: set[UUID]
    tree: _VisibleTreeNodes


def _chunks(values: list[UUID], size: int = 400):
    for start in range(0, len(values), size):
        yield values[start : start + size]


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
    occurrences = extract_links(version.body_md)
    visible_node_ids = _visible_tree_node_ids_for_links(
        session,
        [occurrence.href for occurrence in occurrences],
        workspace_id=page.workspace_id,
        actor=None if public else actor,
        self_origins=context.self_origins,
    )
    locators = {
        href: _classify_context_navigation(href, context.self_origins)
        for href in (occurrence.href for occurrence in occurrences)
    }
    resolution_batch = _resolution_batch(session, list(locators.values()), context)
    navigation_batch = _navigation_batch(
        session,
        [occurrence.href for occurrence in occurrences],
        workspace_id=page.workspace_id,
        actor=None if public else actor,
        public=public,
        self_origins=context.self_origins,
        tree=visible_node_ids,
    )
    for occurrence in occurrences:
        if occurrence.href in seen:
            continue
        seen.add(occurrence.href)
        locator = locators[occurrence.href]
        handled, href = _server_navigation_target(
            session,
            occurrence.href,
            workspace_id=page.workspace_id,
            actor=None if public else actor,
            public=public,
            self_origins=context.self_origins,
            allow_foreign_explicit=not public and actor is not None,
            visible_node_ids=visible_node_ids,
            resolution_batch=resolution_batch,
            navigation_batch=navigation_batch,
        )
        if handled:
            href = _preserve_workspace_view_query(href, occurrence.href)
            targets.append(
                ContextLinkTarget(
                    locator=occurrence.href,
                    href=href,
                    state="ok" if href is not None else "unavailable",
                )
            )
            continue
        # A locator outside the supported URL grammar is opaque authored text,
        # not a failed internal destination. In particular, do not probe the
        # source workspace for unknown/custom paths or protocols.
        if locator.kind == "unknown":
            continue
        resolution = _resolve_context_locator(locator, context, resolution_batch)
        if resolution.state == "ignored":
            continue
        # Authenticated foreign full links navigate directly without probing
        # another Workspace. Public readers cannot inherit that authority.
        if (
            resolution.state == "foreign"
            and not public
            and locator.kind in {"stable_full", "canonical_route"}
        ):
            if urlsplit(occurrence.href).query:
                targets.append(
                    ContextLinkTarget(
                        locator=occurrence.href,
                        href=occurrence.href,
                        state="ok",
                    )
                )
            continue
        target_id = resolution.page_id if resolution.state == "ok" else None
        if locator.fragment and locator.fragment.lower().startswith(
            ("claim-", "artifact-")
        ):
            locator = replace(locator, fragment=None)
            resolution = _resolve_context_locator(locator, context, resolution_batch)
            target_id = resolution.page_id if resolution.state == "ok" else None
        href = resolution.href if target_id is not None else None
        if (
            target_id is not None
            and locator.kind == "canonical_route"
            and urlsplit(occurrence.href).path.endswith("/edit")
            and not public
            and _actor_can_edit_workspace(actor)
        ):
            href = f"{workspace_path(page.workspace_id)}/context/pages/{target_id}/edit"
        href = _preserve_workspace_view_query(
            href, occurrence.href, preserve_fragment=False
        )
        if public and target_id is not None:
            target = (
                resolution_batch.page(target_id)
                if resolution_batch is not None
                else get_in_workspace(
                    session,
                    ContextPage,
                    workspace_id=page.workspace_id,
                    resource_id=target_id,
                )
            )
            exposed_id = (
                valid_public_exposed_version_id(target, workspace)
                if target is not None and workspace is not None
                else None
            )
            exposed = (
                (
                    resolution_batch.versions.get(exposed_id)
                    if resolution_batch is not None
                    else session.scalar(
                        select_in_workspace(
                            ContextPageVersion,
                            workspace_id=page.workspace_id,
                            conditions=(
                                ContextPageVersion.page_id == target_id,
                                ContextPageVersion.id == exposed_id,
                                ContextPageVersion.version_status
                                == "published_snapshot",
                            ),
                        )
                    )
                )
                if exposed_id is not None
                else None
            )
            if (
                exposed is not None
                and (
                    exposed.workspace_id != page.workspace_id
                    or exposed.page_id != target_id
                    or exposed.version_status != "published_snapshot"
                )
            ):
                exposed = None
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
    self_origins = configured_self_origins()
    context = LinkContext(
        session=session,
        workspace_id=workspace_id,
        actor=actor,
        self_origins=self_origins,
    )
    targets: list[ContextLinkTarget] = []
    seen: set[str] = set()
    occurrences = extract_links(body)
    visible_node_ids = _visible_tree_node_ids_for_links(
        session,
        [occurrence.href for occurrence in occurrences],
        workspace_id=workspace_id,
        actor=actor,
        self_origins=self_origins,
    )
    locators = {
        href: _classify_context_navigation(href, self_origins)
        for href in (occurrence.href for occurrence in occurrences)
    }
    resolution_batch = _resolution_batch(session, list(locators.values()), context)
    navigation_batch = _navigation_batch(
        session,
        [occurrence.href for occurrence in occurrences],
        workspace_id=workspace_id,
        actor=actor,
        public=False,
        self_origins=self_origins,
        tree=visible_node_ids,
    )
    target_kinds = {
        "page_id",
        "stable_full",
        "stable_short",
        "canonical_route",
        "nav_target",
        "concept_path",
        "slug",
        "wikilink",
        "resource_uri",
    }
    for occurrence in occurrences:
        if occurrence.href in seen:
            continue
        seen.add(occurrence.href)
        locator = locators[occurrence.href]
        handled, href = _server_navigation_target(
            session,
            occurrence.href,
            workspace_id=workspace_id,
            actor=actor,
            self_origins=self_origins,
            visible_node_ids=visible_node_ids,
            resolution_batch=resolution_batch,
            navigation_batch=navigation_batch,
        )
        if handled:
            href = _preserve_workspace_view_query(href, occurrence.href)
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
        resolution = _resolve_context_locator(locator, context, resolution_batch)
        available = resolution.state == "ok" and resolution.href is not None
        resolved_href = (
            _preserve_workspace_view_query(
                resolution.href, occurrence.href, preserve_fragment=False
            )
            if available
            else None
        )
        targets.append(
            ContextLinkTarget(
                locator=occurrence.href,
                href=resolved_href,
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
    allow_foreign_explicit: bool = False,
    visible_node_ids: _VisibleTreeNodes | set[UUID] | None = None,
    resolution_batch: LinkResolutionBatch | None = None,
    navigation_batch: _NavigationBatch | None = None,
) -> tuple[bool, str | None]:
    """Confirm non-Context browser destinations in their proven source workspace."""
    reference = _parse_workspace_navigation_reference(href, self_origins=self_origins)
    if reference is None:
        return False, None
    section, kind, target_id, explicit_workspace_id = reference
    if explicit_workspace_id is not None and explicit_workspace_id != workspace_id:
        return True, href if allow_foreign_explicit and not public else None
    if public:
        if section == "context" and kind == "legacy_target":
            # Context's resolver must still apply pins and public exposure to
            # legacy page/placement links. Authenticated folder destinations
            # remain unavailable to the anonymous reader.
            return False, None
        # The public reader never inherits authenticated SPA authority.
        return True, None

    if kind == "overview":
        parts = urlsplit(href)
        path = parts.path
        if parts.query:
            path += f"?{parts.query}"
        if parts.fragment:
            path += f"#{parts.fragment}"
        return True, path

    if section in {"review", "ask", "lint", "chats"}:
        if section == "chats" and not _actor_can_edit_workspace(actor):
            return True, None
        if section == "review":
            visible = (
                target_id in navigation_batch.review_ids
                if navigation_batch is not None
                else _review_target_visible(
                    session, workspace_id, target_id, actor
                )
            )
            return True, review_item_url(workspace_id, target_id) if visible else None
        if section == "ask":
            exists = (
                target_id in navigation_batch.ask_sessions
                if navigation_batch is not None
                else get_in_workspace(
                    session,
                    AskSession,
                    workspace_id=workspace_id,
                    resource_id=target_id,
                )
                is not None
            )
            if not exists:
                return True, None
            return True, f"{workspace_path(workspace_id)}/ask/{target_id}"
        if section == "chats":
            exists = (
                target_id in navigation_batch.chat_sessions
                if navigation_batch is not None
                else get_in_workspace(
                    session,
                    McpChatSession,
                    workspace_id=workspace_id,
                    resource_id=target_id,
                )
                is not None
            )
            if not exists:
                return True, None
            return True, f"{workspace_path(workspace_id)}/chats/{target_id}"
        visible = (
            target_id in navigation_batch.finding_ids
            if navigation_batch is not None
            else _finding_target_visible(session, workspace_id, target_id, actor)
        )
        return (
            True,
            f"{workspace_path(workspace_id)}/lint/findings/{target_id}"
            if visible
            else None,
        )

    if kind == "folder":
        node = (
            navigation_batch.tree.node(target_id)
            if navigation_batch is not None
            else get_in_workspace(
                session,
                WorkspaceTreeNode,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
        )
        if (
            node is None
            or node.section != section
            or node.node_kind != "folder"
            or actor is None
            or not _tree_node_visible(session, node, actor, visible_node_ids)
        ):
            return True, None
        return True, f"{workspace_path(workspace_id)}/{section}/folders/{node.id}"

    if kind == "legacy_target":
        if section == "context":
            # Let the Context link resolver retain locator pin precedence for
            # real page and page-placement targets. A plain folder node has no
            # page pin to honor and gets its typed destination here.
            page_exists = (
                resolution_batch.page(target_id) is not None
                if resolution_batch is not None
                else get_in_workspace(
                    session,
                    ContextPage,
                    workspace_id=workspace_id,
                    resource_id=target_id,
                )
                is not None
            )
            if page_exists:
                return False, None
            node = (
                navigation_batch.tree.node(target_id)
                if navigation_batch is not None
                else get_in_workspace(
                    session,
                    WorkspaceTreeNode,
                    workspace_id=workspace_id,
                    resource_id=target_id,
                )
            )
            if node is None or node.section != "context":
                return False, None
            if node.node_kind != "folder":
                return False, None
            if (
                public
                or actor is None
                or not _tree_node_visible(session, node, actor, visible_node_ids)
            ):
                return True, None
            return True, f"{workspace_path(workspace_id)}/context/folders/{node.id}"
        # Legacy target ids were usually domain ids; only if no domain record
        # matches do we inspect a same-workspace tree node and honor its kind.
        if section == "library":
            item = (
                navigation_batch.library_items.get(target_id)
                if navigation_batch is not None
                else get_in_workspace(
                    session,
                    LibraryItem,
                    workspace_id=workspace_id,
                    resource_id=target_id,
                )
            )
            if item is not None:
                return True, (
                    library_item_url(workspace_id, item.id)
                    if library_item_visible(item, actor, workspace_id=workspace_id)
                    else None
                )
            artifact_ref = _canonical_artifact_from_batch(
                session,
                target_id,
                workspace_id=workspace_id,
                navigation_batch=navigation_batch,
            )
            if artifact_ref is not None:
                item, _version, _artifact = artifact_ref
                return True, (
                    library_artifact_url(workspace_id, target_id)
                    if library_item_visible(item, actor, workspace_id=workspace_id)
                    else None
                )
        else:
            page = (
                navigation_batch.agent_pages.get(target_id)
                if navigation_batch is not None
                else get_in_workspace(
                    session,
                    AgentPage,
                    workspace_id=workspace_id,
                    resource_id=target_id,
                )
            )
            if page is not None:
                return True, (
                    agent_page_url(workspace_id, page.id)
                    if agent_page_visible(page, actor, workspace_id=workspace_id)
                    else None
                )
        node = (
            navigation_batch.tree.node(target_id)
            if navigation_batch is not None
            else get_in_workspace(
                session,
                WorkspaceTreeNode,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
        )
        if (
            node is None
            or node.section != section
            or actor is None
            or not _tree_node_visible(session, node, actor, visible_node_ids)
        ):
            return True, None
        if node.node_kind == "folder":
            return True, f"{workspace_path(workspace_id)}/{section}/folders/{node.id}"
        if (
            section == "library"
            and node.target_kind == "library_item"
            and node.target_id
        ):
            item = (
                navigation_batch.library_items.get(node.target_id)
                if navigation_batch is not None
                else get_in_workspace(
                    session,
                    LibraryItem,
                    workspace_id=workspace_id,
                    resource_id=node.target_id,
                )
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
            artifact_ref = _canonical_artifact_from_batch(
                session,
                node.target_id,
                workspace_id=workspace_id,
                navigation_batch=navigation_batch,
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
            page = (
                navigation_batch.agent_pages.get(node.target_id)
                if navigation_batch is not None
                else get_in_workspace(
                    session,
                    AgentPage,
                    workspace_id=workspace_id,
                    resource_id=node.target_id,
                )
            )
            return True, (
                agent_page_url(workspace_id, page.id)
                if agent_page_visible(page, actor, workspace_id=workspace_id)
                else None
            )
        return True, None

    if section == "library" and kind == "item":
        item = (
            navigation_batch.library_items.get(target_id)
            if navigation_batch is not None
            else get_in_workspace(
                session,
                LibraryItem,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
        )
        return True, (
            library_item_url(workspace_id, item.id)
            if library_item_visible(item, actor, workspace_id=workspace_id)
            else None
        )
    if section == "library" and kind == "artifact":
        artifact_ref = _canonical_artifact_from_batch(
            session,
            target_id,
            workspace_id=workspace_id,
            navigation_batch=navigation_batch,
        )
        if artifact_ref is None:
            return True, None
        item, _version, _artifact = artifact_ref
        return True, (
            library_artifact_url(workspace_id, target_id)
            if library_item_visible(item, actor, workspace_id=workspace_id)
            else None
        )
    if section == "agent" and kind in {"page", "page_edit"}:
        page = (
            navigation_batch.agent_pages.get(target_id)
            if navigation_batch is not None
            else get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
        )
        if not agent_page_visible(page, actor, workspace_id=workspace_id):
            return True, None
        page_href = agent_page_url(workspace_id, page.id)
        return True, (
            page_href + "/edit"
            if kind == "page_edit" and _actor_can_edit_workspace(actor)
            else page_href
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


def _canonical_artifact_from_batch(
    session: Session,
    artifact_id: UUID,
    *,
    workspace_id: UUID,
    navigation_batch: _NavigationBatch | None,
):
    if navigation_batch is None:
        return canonical_library_artifact_lookup(
            session, workspace_id=workspace_id, artifact_id=artifact_id
        )
    return navigation_batch.library_artifacts.get(artifact_id)


def _review_target_visible(
    session: Session,
    workspace_id: UUID,
    review_id: UUID,
    actor: ActorContext | None,
) -> bool:
    from backend.services.review_service import ReviewService

    return review_id in ReviewService().visible_review_ids(
        session,
        workspace_id,
        [review_id],
        actor_user_id=actor.user_id if actor else None,
        actor_role=(actor.role or actor.workspace_role) if actor else "viewer",
    )


def _finding_target_visible(
    session: Session,
    workspace_id: UUID,
    finding_id: UUID,
    actor: ActorContext | None,
) -> bool:
    from backend.services.lint_service import LintService

    try:
        return finding_id in LintService().visible_finding_ids(
            session, workspace_id, [finding_id], actor=actor
        )
    except (ReadServiceError, ValueError):
        return False


def _parse_workspace_navigation_reference(
    href: str,
    *,
    self_origins: frozenset[str] = frozenset(),
) -> tuple[str, str, UUID | None, UUID | None] | None:
    try:
        if href != href.strip() or any(
            ord(char) < 32 or ord(char) == 127 for char in href
        ):
            return None
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            if parts.username or parts.password:
                return None
            locator = classify(href, self_origins)
            if not locator.absolute:
                return None
            href = f"{parts.path}?{parts.query}" if parts.query else parts.path
            parts = urlsplit(href)
        if parts.scheme or parts.netloc or parts.username or parts.password:
            return None
        if "//" in parts.path:
            return None
        if parts.path.endswith("/"):
            parts = parts._replace(path=parts.path[:-1])
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
                "context": "target",
                "library": "target",
                "agent": "target",
                "review": "review",
                "ask": "target",
                "lint": "finding",
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
        if not _safe_workspace_query(parts.query, parts.path):
            return None
        overview = re.fullmatch(r"/workspaces/([0-9a-fA-F-]{36})(?:/(.*))?", parts.path)
        suffix = overview[2] if overview is not None else None
        valid_overviews = {
            None,
            "dashboard",
            "inbox",
            "context",
            "library",
            "agent",
            "activity",
            "review",
            "lint",
            "chats",
            "ask",
            "search",
            "settings",
            "setup",
            "settings/integrations",
            "activity/pending",
            "activity/advisory",
            "activity/history",
            "context/folders/new",
            "context/pages/new",
            "library/items/new",
            "library/folders/new",
            "agent/pages/new",
            "agent/folders/new",
        }
        if overview is not None and suffix in valid_overviews:
            return (
                (suffix or "dashboard").split("/", 1)[0],
                "overview",
                None,
                UUID(overview[1]),
            )
        match = re.fullmatch(
            r"/workspaces/([0-9a-fA-F-]{36})/(context|library|agent)/(items|artifacts|pages|folders)/([0-9a-fA-F-]{36})(/edit)?",
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
            if match[5]:
                if section != "agent" or kind != "page":
                    return None
                kind = "page_edit"
            if (
                (section == "library" and kind == "page")
                or (section == "agent" and kind in {"item", "artifact"})
                or (section == "context" and kind != "folder")
            ):
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


def _tree_node_visible(
    session: Session,
    node: WorkspaceTreeNode,
    actor: ActorContext,
    visible_node_ids: _VisibleTreeNodes | set[UUID] | None,
) -> bool:
    if visible_node_ids is not None:
        if isinstance(visible_node_ids, _VisibleTreeNodes):
            return visible_node_ids.visible(node.id)
        return node.id in visible_node_ids
    return TreeReadService.node_visible_to_actor(session, node, actor)


def _visible_tree_node_ids_for_links(
    session: Session,
    hrefs: list[str],
    *,
    workspace_id: UUID,
    actor: ActorContext | None,
    self_origins: frozenset[str],
) -> _VisibleTreeNodes:
    """Use one workspace tree projection for all folder and placement links."""
    if actor is None:
        return _VisibleTreeNodes({}, set())
    candidate_ids: set[UUID] = set()
    candidate_sections: set[str] = set()
    for href in hrefs:
        reference = _parse_workspace_navigation_reference(
            href, self_origins=self_origins
        )
        if reference is None:
            continue
        section, kind, target_id, explicit_workspace_id = reference
        if (
            target_id is not None
            and section in {"context", "library", "agent"}
            and (explicit_workspace_id is None or explicit_workspace_id == workspace_id)
            and kind in {"folder", "legacy_target"}
        ):
            candidate_ids.add(target_id)
            candidate_sections.add(section)
    if not candidate_ids:
        return _VisibleTreeNodes({}, set())
    nodes = []
    for id_chunk in _chunks(list(candidate_ids)):
        nodes.extend(
            session.scalars(
                select_in_workspace(
                    WorkspaceTreeNode,
                    workspace_id=workspace_id,
                    conditions=(
                        WorkspaceTreeNode.id.in_(id_chunk),
                        WorkspaceTreeNode.section.in_(candidate_sections),
                    ),
                )
            )
        )
    return _VisibleTreeNodes(
        {node.id: node for node in nodes},
        TreeReadService.visible_node_ids(session, nodes, actor),
    )


def _navigation_batch(
    session: Session,
    hrefs: list[str],
    *,
    workspace_id: UUID,
    actor: ActorContext | None,
    public: bool,
    self_origins: frozenset[str],
    tree: _VisibleTreeNodes,
) -> _NavigationBatch:
    library_ids: set[UUID] = set()
    library_artifact_ids: set[UUID] = set()
    agent_ids: set[UUID] = set()
    ask_ids: set[UUID] = set()
    chat_ids: set[UUID] = set()
    review_ids: set[UUID] = set()
    finding_ids: set[UUID] = set()
    for href in hrefs:
        reference = _parse_workspace_navigation_reference(
            href, self_origins=self_origins
        )
        if reference is None:
            continue
        section, kind, target_id, explicit_workspace_id = reference
        if (
            target_id is None
            or (
                explicit_workspace_id is not None
                and explicit_workspace_id != workspace_id
            )
        ):
            continue
        if section == "library" and kind in {"item", "legacy_target"}:
            library_ids.add(target_id)
        if section == "library" and kind in {"artifact", "legacy_target"}:
            library_artifact_ids.add(target_id)
        elif section == "agent" and kind in {"page", "page_edit", "legacy_target"}:
            agent_ids.add(target_id)
        elif kind in {"item", "legacy_target"} and section == "ask":
            ask_ids.add(target_id)
        elif (
            kind in {"item", "legacy_target"}
            and section == "chats"
            and _actor_can_edit_workspace(actor)
        ):
            chat_ids.add(target_id)
        elif section == "review" and kind in {"item", "review"}:
            review_ids.add(target_id)
        elif section == "lint" and kind in {"item", "finding"}:
            finding_ids.add(target_id)
        node = tree.node(target_id) if kind in {"folder", "legacy_target"} else None
        if node is None or node.target_id is None:
            continue
        if section == "library" and node.target_kind == "library_item":
            library_ids.add(node.target_id)
        elif section == "library" and node.target_kind == "library_artifact":
            library_artifact_ids.add(node.target_id)
        elif section == "agent" and node.target_kind == "agent_page":
            agent_ids.add(node.target_id)

    library_items: dict[UUID, LibraryItem] = {}
    library_artifacts = {}
    agent_pages: dict[UUID, AgentPage] = {}
    ask_sessions: set[UUID] = set()
    chat_sessions: set[UUID] = set()
    visible_review_ids: set[UUID] = set()
    visible_finding_ids: set[UUID] = set()
    if not public:
        for id_chunk in _chunks(list(library_ids)):
            library_items.update(
                (item.id, item)
                for item in session.scalars(
                    select_in_workspace(
                        LibraryItem,
                        workspace_id=workspace_id,
                        conditions=(LibraryItem.id.in_(id_chunk),),
                    )
                )
            )
        library_artifacts = canonical_library_artifact_lookup_many(
            session,
            workspace_id=workspace_id,
            artifact_ids=library_artifact_ids,
        )
        for id_chunk in _chunks(list(agent_ids)):
            agent_pages.update(
                (page.id, page)
                for page in session.scalars(
                    select_in_workspace(
                        AgentPage,
                        workspace_id=workspace_id,
                        conditions=(AgentPage.id.in_(id_chunk),),
                    )
                )
            )
        for id_chunk in _chunks(list(ask_ids)):
            ask_sessions.update(
                session.scalars(
                    select_in_workspace(
                        AskSession,
                        workspace_id=workspace_id,
                        conditions=(AskSession.id.in_(id_chunk),),
                    ).with_only_columns(AskSession.id)
                )
            )
        for id_chunk in _chunks(list(chat_ids)):
            chat_sessions.update(
                session.scalars(
                    select_in_workspace(
                        McpChatSession,
                        workspace_id=workspace_id,
                        conditions=(McpChatSession.id.in_(id_chunk),),
                    ).with_only_columns(McpChatSession.id)
                )
            )
        if review_ids:
            from backend.services.review_service import ReviewService

            visible_review_ids = ReviewService().visible_review_ids(
                session,
                workspace_id,
                list(review_ids),
                actor_user_id=actor.user_id if actor else None,
                actor_role=(actor.role or actor.workspace_role) if actor else "viewer",
            )
        if finding_ids:
            from backend.services.lint_service import LintService

            visible_finding_ids = LintService().visible_finding_ids(
                session, workspace_id, list(finding_ids), actor=actor
            )
    return _NavigationBatch(
        library_items,
        library_artifacts,
        agent_pages,
        ask_sessions,
        chat_sessions,
        visible_review_ids,
        visible_finding_ids,
        tree,
    )


def _safe_workspace_query(query: str, path: str = "") -> bool:
    """Reject identity override parameters while allowing presentation state."""
    if not query:
        return True
    params = parse_qs(query, keep_blank_values=True)
    if {
        "target",
        "edit",
        "review",
        "finding",
        "new",
        "newFolder",
        "parentNodeId",
        "parentId",
        "workspaceId",
        "workspace_id",
        "ws",
    } & params.keys():
        return False
    parents = params.get("parent", ())
    if not parents:
        return True
    if not path.endswith("/new") or len(parents) != 1:
        return False
    try:
        UUID(parents[0])
    except ValueError:
        return False
    return True


def _classify_context_navigation(href: str, self_origins: frozenset[str]):
    """Classify typed Context links after validating harmless UI query state."""
    try:
        parts = urlsplit(href)
        if not parts.query or not parts.path.startswith(("/p/", "/workspaces/")):
            return classify(href, self_origins)
        if not _safe_workspace_query(parts.query, parts.path):
            return classify(href, self_origins)
        stripped = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, "", parts.fragment)
        )
        locator = classify(stripped, self_origins)
        if locator.kind not in {"stable_full", "stable_short", "canonical_route"}:
            return classify(href, self_origins)
        return replace(locator, href=href)
    except ValueError:
        return classify(href, self_origins)


def _preserve_workspace_view_query(
    destination: str | None,
    original: str,
    *,
    preserve_fragment: bool = True,
) -> str | None:
    if destination is None:
        return None
    original_parts = urlsplit(original)
    query = original_parts.query
    if query and not _safe_workspace_query(query, original_parts.path):
        return destination
    destination_parts = urlsplit(destination)
    fragment = (
        original_parts.fragment if preserve_fragment else destination_parts.fragment
    )
    return destination_parts._replace(query=query, fragment=fragment).geturl()


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
    links = list(session.scalars(
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
    ))
    resolution_batch = _resolution_batch(
        session,
        [classify(f"[[{link.target_slug}]]") for link in links],
        context,
    )
    result = []
    for link in links:
        resolution = _resolve_context_locator(
            classify(f"[[{link.target_slug}]]"), context, resolution_batch
        )
        visible = resolution.state == "ok"
        result.append(
            WikilinkTarget(
                target_slug=link.target_slug,
                target_page_id=resolution.page_id if visible else None,
                current_slug=resolution.target_slug if visible else None,
            )
        )
    return result


def _resolution_batch(
    session: Session, locators: list, context: LinkContext
) -> LinkResolutionBatch | None:
    return LinkResolutionBatch(locators, context)


def _resolve_context_locator(
    locator, context: LinkContext, batch: LinkResolutionBatch | None
):
    return (
        resolve(locator, context)
        if batch is None
        else resolve(locator, context, batch=batch)
    )
