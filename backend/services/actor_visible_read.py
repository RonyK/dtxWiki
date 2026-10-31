"""Canonical actor-visible read policy for knowledge and audit entities.

Every public read surface must use these predicates instead of deriving its
own category, version, or trust-level rules.  Keeping the decisions here makes
REST, MCP tools, Resources, search, bundles, and legacy Pages agree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from backend.policy.trust_boundaries import (
    _role_allows,
    actor_can_read_drafts,
    actor_can_read_restricted_library,
    actor_can_read_sensitive_agent_pages,
)
from backend.repositories.context_visibility import (
    PUBLISHED_CONTEXT_VERSION_STATUSES,
)
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.agent import SENSITIVE_AGENT_PAGE_CATEGORIES

if TYPE_CHECKING:
    from backend.models.agent import AgentPage, AgentPageVersion
    from backend.models.context import ContextPage, ContextPageVersion
    from backend.policy.auth import ActorContext


VISIBLE_AGENT_SENSITIVITIES = frozenset({"public", "workspace"})


class LibraryVisibilityItem(Protocol):
    """Read-only inputs shared by ORM entities and scalar read projections."""

    @property
    def workspace_id(self) -> UUID: ...

    @property
    def lifecycle_status(self) -> str: ...

    @property
    def trust_level(self) -> str: ...

    @property
    def current_ready_version_id(self) -> UUID | None: ...

    @property
    def current_version_id(self) -> UUID | None: ...


class LibraryVisibilityVersion(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def processing_status(self) -> str: ...


def context_page_visible(
    page: ContextPage | None,
    actor: ActorContext | None,
    *,
    workspace_id=None,
    allow_archived: bool = False,
) -> bool:
    """Return whether ``page`` has an actor-visible Context head.

    ``allow_archived`` is reserved for a caller that has independently proved
    the actor may decide a live review for this page. Ordinary read surfaces
    must keep the default so archived targets remain hidden.
    """

    if page is None or (workspace_id is not None and page.workspace_id != workspace_id):
        return False
    if page.lifecycle_status == "archived" and not allow_archived:
        return False
    # Local import keeps the canonical policy module out of repository cycles.
    from backend.repositories.context_visibility import visible_context_version_id

    return visible_context_version_id(page, actor)[0] is not None


def context_page_id_visible(
    session,
    page_id: UUID,
    actor: ActorContext | None,
    *,
    workspace_id: UUID,
    allow_archived: bool = False,
) -> bool:
    """``context_page_visible`` for a caller that holds only the page id.

    The API layer may not import models, so the row lookup lives here beside
    the rule it feeds rather than being repeated at each route.
    """

    from backend.models.context import ContextPage

    return context_page_visible(
        get_in_workspace(
            session,
            ContextPage,
            workspace_id=workspace_id,
            resource_id=page_id,
        ),
        actor,
        workspace_id=workspace_id,
        allow_archived=allow_archived,
    )


def agent_page_visible(
    page: AgentPage | None,
    actor: ActorContext | None,
    *,
    workspace_id=None,
) -> bool:
    """Return whether ``page`` is readable through ordinary Agent-page reads."""

    if page is None or (workspace_id is not None and page.workspace_id != workspace_id):
        return False
    if page.lifecycle_status == "archived":
        return False
    if not actor_can_read_agent_page(page, actor):
        return False
    return page.current_active_version_id is not None or actor_can_read_drafts(actor)


def library_item_visible(
    item: LibraryVisibilityItem | None,
    actor: ActorContext | None,
    *,
    workspace_id=None,
    version: LibraryVisibilityVersion | None = None,
) -> bool:
    """Return whether ``item`` (and optional canonical ``version``) is readable.

    ``current_version_id`` is the latest submitted source and may still be
    converting. A version-qualified read remains pinned to the last ready
    source while that work is in flight. The completed-current fallback keeps
    pre-0005 rows readable until their ready pointer is backfilled.
    """

    if item is None or (workspace_id is not None and item.workspace_id != workspace_id):
        return False
    # Local import avoids a service-level cycle through OKF visibility callers.
    from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES

    if item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
        return False
    if not actor_can_read_library_trust_level(item.trust_level, actor):
        return False
    if version is None:
        return True
    if item.current_ready_version_id is not None:
        return version.id == item.current_ready_version_id
    return version.id == item.current_version_id and version.processing_status in {
        "completed",
        "ready",
        "ready_partial",
    }


def context_version_is_visible(
    version: ContextPageVersion,
    actor: ActorContext | None,
) -> bool:
    """Return whether an explicit Context version may be read by ``actor``."""

    if version.version_status in PUBLISHED_CONTEXT_VERSION_STATUSES:
        return True
    if not actor_can_read_drafts(actor):
        return False
    # P2.2/P2.6: naming a private `editing` row by its exact version_id must
    # not bypass the same author-only check `context_visibility.py`'s
    # pointer-based lookups apply -- draft-read capability alone answers
    # "may this actor see SOME unpublished content", not "may this actor see
    # THIS specific author's unshared editing".
    from backend.repositories.context_visibility import editing_row_hidden_from_actor

    return not editing_row_hidden_from_actor(version, actor)


def context_parent_projection(
    version: ContextPageVersion,
    parent: ContextPageVersion | None,
    actor: ActorContext | None,
) -> tuple[str, UUID | None]:
    """Project a version's parent without leaking an unreadable version id.

    Every REST/MCP history and detail surface must use this choke point. A
    published version can legitimately descend from an unpublished draft, but
    exposing that draft UUID to a Viewer is still metadata disclosure.
    """

    if version.parent_version_id is None:
        return "root", None
    if parent is None or parent.page_id != version.page_id:
        return "missing", None
    if not context_version_is_visible(parent, actor):
        return "hidden", None
    return "visible", parent.id


def agent_page_requires_sensitive_access(page: AgentPage) -> bool:
    """Treat either a reserved category or private sensitivity as sensitive."""

    return (
        page.category in SENSITIVE_AGENT_PAGE_CATEGORIES
        or page.sensitivity not in VISIBLE_AGENT_SENSITIVITIES
    )


def actor_can_read_agent_page(
    page: AgentPage,
    actor: ActorContext | None,
) -> bool:
    if not agent_page_requires_sensitive_access(page):
        return True
    return actor_can_read_sensitive_agent_pages(actor)


def agent_version_is_visible(
    page: AgentPage,
    version: AgentPageVersion,
    actor: ActorContext | None,
) -> bool:
    if not actor_can_read_agent_page(page, actor):
        return False
    return version.version_status != "draft" or actor_can_read_drafts(actor)


def actor_can_read_library_trust_level(
    trust_level: str | None,
    actor: ActorContext | None,
) -> bool:
    return trust_level != "restricted" or actor_can_read_restricted_library(actor)


def actor_can_read_audit_records(actor: ActorContext | None) -> bool:
    """Audit/change payloads require the audit capability at the service seam.

    Browser sessions are already authenticated and scope-gated by the REST
    route registry.  Bearer and MCP actors must carry the explicit capability;
    a plain ``mcp:read`` token intentionally does not expand to ``audit-read``.

    NOTE: this gate governs change-set reads, change search, and version-history
    (``list_changes``) where the callee ALSO filters by target visibility, so a
    session may pass here and still see only visible targets. Feedback is
    different — it has no per-row target filter — so it uses the stricter
    ``actor_can_read_feedback`` instead.
    """

    if actor is None:
        return False
    if actor.actor_kind == "system" or actor.token_kind == "session":
        return True
    scopes = set(actor.scopes)
    return bool({"*", "audit-read", "mcp:write"} & scopes)


def _referenced_version_id(diff_ref: str | None, prefix: str):
    """The version id a ChangeOperation's ``diff_ref`` names, if it names one.

    HEAD writes two spellings: the bare ``"<kind>:<uuid>"`` used by every page
    write, and the inline-comment anchor JSON carrying ``version_id``
    (change_review_audit.py). Anything else (tree ``parent:...;sort:...``,
    ``context_ingress_proposal:...``, a JSON blob with no ``version_id`` key)
    names no version.
    """
    import json
    from uuid import UUID

    if not diff_ref:
        return None
    raw = None
    if diff_ref.startswith(prefix):
        raw = diff_ref[len(prefix) :]
    elif diff_ref.startswith("{"):
        try:
            raw = json.loads(diff_ref).get("version_id")
        except (ValueError, AttributeError):
            return None
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def change_set_visible(session, change_set, actor: ActorContext | None) -> bool:
    """A change set is readable only if EVERY operation in it is readable.

    LEAK-02: ``actor_can_read_audit_records`` is a capability gate only — it
    says nothing about whether the actor may see the specific pages/agent
    pages/tree nodes a change set's operations touch, and neither the
    change-search lane nor the direct change-set read used to apply any
    target filter at all. An operation is readable when its target entity is
    readable AND, when the operation binds a specific version via
    ``diff_ref``, that version is readable too: an autosave/metadata_patch
    operation on an otherwise-published page still describes the unpublished
    draft bytes (page_service.py ``stage_page_metadata_patch``), so page
    visibility alone is not enough.
    """

    from sqlalchemy import select

    from backend.models.agent import AgentPage, AgentPageVersion
    from backend.models.change import ChangeOperation
    from backend.models.context import ContextPageVersion
    from backend.models.tree import WorkspaceTreeNode
    from backend.services.tree_read import TreeReadService

    if actor is None:
        return False
    workspace_id = change_set.workspace_id
    operations = session.scalars(
        select(ChangeOperation).where(ChangeOperation.change_set_id == change_set.id)
    ).all()
    if not operations:
        # "readable only if EVERY operation is readable" is vacuously true
        # for an empty set, but a change set's own `reason` can be arbitrary
        # free text (e.g. caller-supplied risk_notes) independent of any
        # operation — a visibility predicate must not default to True just
        # because there is nothing (yet) to check. No current write path
        # persists a ChangeSet without an operation in the same transaction,
        # but this predicate's own contract must not rely on that staying
        # true for every future caller.
        return False
    for operation in operations:
        if operation.target_type == "context_page":
            if not context_page_id_visible(
                session, operation.target_id, actor, workspace_id=workspace_id
            ):
                return False
            version_id = _referenced_version_id(
                operation.diff_ref, "context_page_version:"
            )
            if version_id is not None:
                version = get_in_workspace(
                    session,
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    resource_id=version_id,
                )
                if (
                    version is None
                    or version.page_id != operation.target_id
                    or not context_version_is_visible(version, actor)
                ):
                    return False
        elif operation.target_type == "agent_page":
            page = get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=operation.target_id,
            )
            if not agent_page_visible(page, actor, workspace_id=workspace_id):
                return False
            version_id = _referenced_version_id(
                operation.diff_ref, "agent_page_version:"
            )
            if version_id is not None:
                version = get_in_workspace(
                    session,
                    AgentPageVersion,
                    workspace_id=workspace_id,
                    resource_id=version_id,
                )
                if (
                    version is None
                    or version.agent_page_id != page.id
                    or not agent_version_is_visible(page, version, actor)
                ):
                    return False
        elif operation.target_type == "workspace_tree_node":
            node = get_in_workspace(
                session,
                WorkspaceTreeNode,
                workspace_id=workspace_id,
                resource_id=operation.target_id,
            )
            if (
                node is None
                or node.workspace_id != workspace_id
                or node.id
                not in TreeReadService.visible_node_ids(session, [node], actor)
            ):
                return False
        elif operation.target_type == "feedback":
            if not actor_can_read_feedback(actor):
                return False
        else:
            # Unknown target type fails closed, like _require_visible_history_target.
            return False
    return True


def actor_can_read_feedback(actor: ActorContext | None) -> bool:
    """Feedback bodies are audit-class content with NO per-row target-visibility
    filter, so the list/search/read surfaces must gate on the capability itself.

    A ``system`` actor is trusted; a bearer/MCP actor needs the explicit audit
    capability (a plain ``mcp:read`` token does not expand to it). A browser
    session is not scope-gated at the route registry, so it is gated on role:
    only ``editor`` or above may read feedback. Having a session is NOT itself
    authorization — a viewer session is denied exactly like an ``mcp:read`` token.
    """

    if actor is None:
        return False
    if actor.actor_kind == "system":
        return True
    scopes = set(actor.scopes)
    if {"*", "audit-read", "mcp:write"} & scopes:
        return True
    if actor.token_kind == "session":
        role = getattr(actor, "workspace_role", None)
        if not role:
            return False
        try:
            return _role_allows(role, "editor")
        except ValueError:
            return False
    return False


def resolve_visible_context_version(
    session,
    *,
    workspace_id: UUID,
    page_id: UUID | None,
    version_id: UUID | None,
    path: str | None,
    version_preference: str,
    actor: ActorContext | None,
):
    """Resolve a context read target to the (page, version) an actor may see.

    Read surfaces address a context page three ways -- by version, by page, or
    by concept path -- and each way still has to land on a version this actor is
    allowed to read.  That decision belongs here with the other visibility
    predicates rather than in each adapter, so MCP, REST and Resources cannot
    drift apart on which draft a reader is shown.

    Returns ``(None, None)`` for every miss: not found, wrong workspace, or not
    visible.  Callers must not distinguish these, because telling a reader that
    a page exists but is hidden is itself a disclosure.
    """
    # Deferred on purpose, not for lazy-loading. `backend.policy.auth` imports
    # this module, and `concept_resolution` imports `policy.auth`, so hoisting
    # these to module scope raises ImportError on a partially initialized
    # `policy.auth` at startup. Verified by hoisting them and watching the
    # tree fail to import.
    from backend.models.context import ContextPage, ContextPageVersion
    from backend.repositories.context_page_repo import get_open_edit_for_actor
    from backend.services.concept_resolution import resolve_context_concept
    from backend.services.context_visibility import visible_context_version_id
    from backend.services.okf import parse_concept_path

    page = None
    explicit_version = None
    if version_id is not None:
        explicit_version = get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=workspace_id,
            resource_id=version_id,
        )
        if explicit_version is not None:
            page = get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=explicit_version.page_id,
            )
    elif page_id is not None:
        page = get_in_workspace(
            session,
            ContextPage,
            workspace_id=workspace_id,
            resource_id=page_id,
        )
    elif path is not None:
        parsed = parse_concept_path(path)
        if parsed is None or parsed[0] != "context":
            return None, None
        resolved = resolve_context_concept(
            session,
            workspace_id,
            identifier=parsed[1],
            actor=actor,
        )
        if resolved.status not in {"found", "redirected"}:
            return None, None
        page = resolved.page

    if page is None or page.workspace_id != workspace_id:
        return None, None
    if explicit_version is not None:
        if explicit_version.page_id != page.id or not context_version_is_visible(
            explicit_version, actor
        ):
            return None, None
        return page, explicit_version

    if version_preference == "my_edit":
        # The caller's OWN open editing row -- the body its next
        # patch_context_edit applies to. Read-only: a missing edit is a miss,
        # never a reason to open one (that is begin_context_edit's job).
        if actor is None:
            return None, None
        own_edit = get_open_edit_for_actor(
            session,
            workspace_id=workspace_id,
            page_id=page.id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
        )
        if own_edit is None or not context_version_is_visible(own_edit, actor):
            return None, None
        return page, own_edit
    if version_preference == "visible":
        resolved_version_id = visible_context_version_id(page, actor)[0]
    elif version_preference == "published":
        resolved_version_id = page.current_published_version_id
    else:
        if not actor_can_read_drafts(actor):
            return None, None
        resolved_version_id = page.latest_draft_version_id
    version = (
        get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=workspace_id,
            resource_id=resolved_version_id,
        )
        if resolved_version_id
        else None
    )
    if (
        version is None
        or version.page_id != page.id
        or not context_version_is_visible(version, actor)
    ):
        return None, None
    return page, version
