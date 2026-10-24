"""Authorize the minimal location needed by authenticated page permalinks."""

from __future__ import annotations

from collections import deque
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING
from uuid import UUID

from backend.policy.auth import ActorContext
from backend.policy.mcp_scopes import CONTEXT_PAGE_BODY_SCOPES, has_required_scope
from backend.policy.rbac import (
    rebind_actor_to_workspace_access,
    require_workspace_role,
)
from backend.policy.trust_boundaries import actor_with_target_workspace_scope
from backend.schemas.page import ContextPageLocation
from backend.services.actor_visible_read import resolve_visible_context_version
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError

if TYPE_CHECKING:
    from backend.app.container import DatabaseProvider, WorkspaceDirectory


PAGE_LOCATION_RATE_LIMIT_PER_MINUTE = 60


class PageLocationRateLimitError(Exception):
    pass


class PageLocationService:
    def __init__(self) -> None:
        # Container-owned, shared across credentials and page ids for a user.
        # The supported server runs in one process, as do the auth limiters.
        self._attempts: dict[tuple[str, str], deque[float]] = {}
        self._lock = Lock()

    def locate(
        self,
        page_id: UUID,
        *,
        actor: ActorContext,
        db: DatabaseProvider,
        directory: WorkspaceDirectory,
    ) -> ContextPageLocation:
        self._consume_attempt(actor)
        try:
            with db.control_session_factory()() as session:
                # Directory lookup is routing, never authorization. The local
                # directory uses the single DB; a shard overlay must replace it.
                workspace_id = directory.workspace_id_by_context_page(session, page_id)
                if workspace_id is None:
                    raise ReadNotFoundError("page not found")
                actor = actor_with_target_workspace_scope(
                    actor, workspace_id, session=session
                )
                # Reject a known capability denial before workspace-plane
                # failures can distinguish an existing target from a miss.
                self._require_context_read_scope(actor)
                access = require_workspace_role(
                    session=session,
                    actor=actor,
                    workspace_id=workspace_id,
                    required_role="viewer",
                    workspace_session_factory=db.session_for(workspace_id),
                )
                actor = rebind_actor_to_workspace_access(actor, access)
                # Check after the workspace-plane grant intersection, using
                # the same read capability as the REST scope registry.
                self._require_context_read_scope(actor)
        except (PermissionError, RejectedByPolicyError) as exc:
            raise ReadNotFoundError("page not found") from exc

        with db.session_for(workspace_id)() as session:
            page, version = resolve_visible_context_version(
                session,
                workspace_id=workspace_id,
                page_id=page_id,
                version_id=None,
                path=None,
                version_preference="visible",
                actor=actor,
            )
            if version is None or page.lifecycle_status == "archived":
                raise ReadNotFoundError("page not found")
        return ContextPageLocation(workspace_id=workspace_id, page_id=page_id)

    @staticmethod
    def _require_context_read_scope(actor: ActorContext) -> None:
        if actor.token_kind != "session" and not has_required_scope(
            actor, CONTEXT_PAGE_BODY_SCOPES
        ):
            raise ReadNotFoundError("page not found")

    def _consume_attempt(self, actor: ActorContext) -> None:
        key = (actor.actor_kind, actor.actor_id)
        with self._lock:
            now = monotonic()
            # Bound idle state as well as each active user's request window.
            for idle_key in list(self._attempts):
                if now - self._attempts[idle_key][-1] >= 60:
                    del self._attempts[idle_key]
            attempts = self._attempts.setdefault(key, deque())
            while attempts and now - attempts[0] >= 60:
                attempts.popleft()
            if len(attempts) >= PAGE_LOCATION_RATE_LIMIT_PER_MINUTE:
                raise PageLocationRateLimitError
            attempts.append(now)
