"""Authorization projections for Context-owned media transports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn
from uuid import UUID

from sqlalchemy import select

from backend.app.container import DatabaseProvider
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.context_media import ContextMediaAsset
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.rbac import (
    GrantReplicaMissingError,
    WorkspaceAccess,
    WorkspacePlaneUnavailableError,
    rebind_actor_to_workspace_access,
    require_workspace_role,
    role_allows,
)
from backend.policy.trust_boundaries import (
    actor_kind_for_check,
    actor_with_target_workspace_scope,
)
from backend.services.actor_visible_read import context_version_is_visible
from backend.services.public_page_exposure import (
    exposure_scope_for_workspace,
    valid_public_exposed_version_id,
)
from backend.services.read_errors import RejectedByPolicyError


class ContextMediaAccessError(RuntimeError):
    """Stable outer-layer error used by Context media transport adapters."""

    def __init__(self, error_code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PublicContextMediaScope:
    workspace_id: UUID
    page_id: UUID
    page_version_id: UUID
    exposure_scope: str


class ContextMediaAccessPolicy:
    """Resolve workspace membership and exact PageVersion visibility.

    ``ContextPage``/``ContextPageVersion``/``ContextMediaAsset`` are
    workspace-plane domain tables under a routing-capable ``DatabaseProvider``:
    every method here resolves ``workspace_id`` on the control plane through
    ``directory`` (a thin, overlay-swappable id lookup -- the same seam
    ``backend.api.context_pages._workspace_page_service`` already uses), then opens the
    actual page/version read on ``db.session_for(workspace_id)``.
    ``require_workspace_role`` reads ``WorkspaceMembership``, an
    identity-plane table (backend/policy/rbac.py, ``get_workspace_access``),
    so its role check must not move to the workspace session -- but WHEN it
    runs relative to the workspace read differs by actor. Session actors keep
    the existing existence/archived-FIRST ordering: a role-shaped error (403)
    must never be observable for a page that doesn't exist or is archived, or
    the response status code becomes an existence oracle keyed on the caller's
    role. PAT/OAuth callers also depend on a workspace-plane credential
    replica. The REST path already names the expected workspace, so those
    actors are fully preauthorized against it before either the page-directory
    lookup or domain read. A missing replica therefore cannot reveal whether
    the supplied page or version exists. ``require_workspace`` and
    ``require_session`` have no page/version to check for existence, so their
    role check still runs directly on the directory-lookup control session,
    same as before. ``require_workspace_role``'s own MCP-connection-disabled
    check is a separate, per-call exception in every case -- ``MCPClient`` is
    workspace-plane data, so ``_require_role`` below always hands it
    ``self._db.session_for(workspace_id)`` as the required
    ``workspace_session_factory`` kwarg even though the role check itself
    still runs on ``session`` (control).
    """

    def __init__(self, db, directory) -> None:
        self._db = db
        self._directory = directory

    def require_workspace(
        self, actor: ActorContext, workspace_id: UUID, *, role: str
    ) -> None:
        # workspace_id is already known -- no directory lookup needed. The
        # role check itself still needs the control plane: it reads
        # WorkspaceMembership (an identity-plane table, see class docstring),
        # not anything routable by workspace_id.
        with self._db.control_session_factory()() as session:
            self._require_role(
                session,
                actor,
                workspace_id,
                role=role,
                hide_membership=False,
            )

    def assert_page_not_archived(self, page_id: UUID) -> None:
        """Assert that a page exists and is active without authorizing a caller.

        The MCP dispatcher (``backend.mcp.router.resolve_target_actor`` /
        ``dispatch``) has already resolved workspace access before upload
        handlers call this method.  This is deliberately not an authorization
        boundary and must not be reused for membership or role checks.
        """
        control_factory = self._db.control_session_factory()
        with control_factory() as session:
            workspace_id = self._directory.workspace_id_by_context_page(
                session, page_id
            )
            if workspace_id is None:
                raise _not_found("page not found")
        with self._db.session_for(workspace_id)() as session:
            page = session.get(ContextPage, page_id)
            if page is None or page.lifecycle_status == "archived":
                raise _not_found("page not found")

    def require_page(
        self,
        actor: ActorContext,
        page_id: UUID,
        *,
        expected_workspace_id: UUID,
        role: str,
    ) -> UUID:
        preauthorized_access = self._preauthorize_credential_target(
            actor,
            expected_workspace_id,
            role=role,
            hide_membership=True,
        )
        control_factory = self._db.control_session_factory()
        with control_factory() as session:
            workspace_id = self._directory.workspace_id_by_context_page(
                session, page_id
            )
            if workspace_id is None or workspace_id != expected_workspace_id:
                raise _not_found("page not found")
        # Existence/archived must be settled before the role check can raise
        # anything role-shaped -- otherwise an archived page's status code
        # depends on the caller's role (403 for an insufficient-role member,
        # 404 for a sufficient one), which is both an existence oracle and a
        # broken "archived reads as not found" contract. See the fix commit
        # this comment shipped with for the reproduction.
        with self._db.session_for(workspace_id)() as session:
            page = session.get(ContextPage, page_id)
            if page is None or page.lifecycle_status == "archived":
                raise _not_found("page not found")
            if preauthorized_access is None:
                with control_factory() as role_session:
                    self._require_role(
                        role_session,
                        actor,
                        workspace_id,
                        role=role,
                        hide_membership=True,
                    )
            return workspace_id

    def require_page_version(
        self,
        actor: ActorContext,
        page_id: UUID,
        page_version_id: UUID,
        *,
        expected_workspace_id: UUID,
        role: str,
    ) -> UUID:
        preauthorized_access = self._preauthorize_credential_target(
            actor,
            expected_workspace_id,
            role=role,
            hide_membership=True,
        )
        control_factory = self._db.control_session_factory()
        with control_factory() as session:
            workspace_id = self._directory.workspace_id_by_context_page(
                session, page_id
            )
            if workspace_id is None or workspace_id != expected_workspace_id:
                raise _not_found("page version not found")
        # Role-independent existence/archived must be settled before the role
        # check can raise anything role-shaped (same fix as require_page
        # above). The role-DEPENDENT visibility check below (draft/history
        # hiding) stays after the role check on purpose -- that hiding is
        # meant to vary by role, unlike "this page is archived."
        with self._db.session_for(workspace_id)() as session:
            page = session.get(ContextPage, page_id)
            version = session.get(ContextPageVersion, page_version_id)
            if (
                page is None
                or page.lifecycle_status == "archived"
                or version is None
                or version.page_id != page_id
            ):
                raise _not_found("page version not found")
            access = preauthorized_access
            if access is None:
                with control_factory() as role_session:
                    access = self._require_role(
                        role_session,
                        actor,
                        workspace_id,
                        role=role,
                        hide_membership=True,
                    )
            scoped_actor = rebind_actor_to_workspace_access(actor, access)
            may_read_history = role_allows(access.role, "editor")
            if not context_version_is_visible(version, scoped_actor) or (
                not may_read_history
                and page.current_published_version_id != page_version_id
            ):
                raise _not_found("page version not found")
            return workspace_id

    def require_workspace_asset(
        self, actor: ActorContext, workspace_id: UUID, asset_id: UUID, *, role: str
    ) -> UUID:
        """Derive an asset's owning page for the asset-addressed DELETE
        route (§5.3: ``(workspace_id, asset_id)`` names the command, page_id
        is never a caller-supplied input). Same existence-before-role
        ordering as ``require_page``/``require_page_version`` -- this is an
        existence check on workspace-plane data, not merely a role check
        like ``require_workspace``.

        The fine-grained "does THIS actor own the still-open editing
        session that created this asset" check stays in
        DeleteUnusedMedia (context_media/application/use_cases.py) -- this
        method only establishes the coarse workspace/editor-role boundary
        and resolves the page_id the command needs.
        """

        preauthorized_access = self._preauthorize_credential_target(
            actor,
            workspace_id,
            role=role,
            hide_membership=True,
        )
        with self._db.session_for(workspace_id)() as session:
            page_id = session.scalar(
                select(ContextMediaAsset.page_id).where(
                    ContextMediaAsset.workspace_id == workspace_id,
                    ContextMediaAsset.id == asset_id,
                )
            )
            if page_id is None:
                raise _not_found("media not found")
            if preauthorized_access is None:
                with self._db.control_session_factory()() as role_session:
                    self._require_role(
                        role_session,
                        actor,
                        workspace_id,
                        role=role,
                        hide_membership=True,
                    )
            return page_id

    def _preauthorize_credential_target(
        self,
        actor: ActorContext,
        workspace_id: UUID,
        *,
        role: str,
        hide_membership: bool,
    ) -> WorkspaceAccess | None:
        """Gate replica-backed credentials before target-domain existence reads."""

        try:
            check_kind = actor_kind_for_check(actor)
        except PermissionError as exc:
            self._raise_permission_error(exc, hide_membership=hide_membership)
        if check_kind not in {"pat", "oauth_jwt"}:
            return None
        with self._db.control_session_factory()() as session:
            return self._require_role(
                session,
                actor,
                workspace_id,
                role=role,
                hide_membership=hide_membership,
            )

    def _require_role(
        self,
        session,
        actor: ActorContext,
        workspace_id: UUID,
        *,
        role: str,
        hide_membership: bool,
    ) -> WorkspaceAccess:
        try:
            scoped_actor = actor_with_target_workspace_scope(
                actor,
                workspace_id,
                session=session,
            )
            return require_workspace_role(
                session=session,
                actor=scoped_actor,
                workspace_id=workspace_id,
                required_role=role,
                # MCPClient (the row the disable check reads) is workspace-plane
                # data, unlike WorkspaceMembership above -- see class docstring.
                workspace_session_factory=self._db.session_for(workspace_id),
            )
        except (WorkspacePlaneUnavailableError, GrantReplicaMissingError) as exc:
            # D-01v3-e/D-01v3-b: neither is a RejectedByPolicyError
            # (deliberately -- see rbac.py), so neither belongs behind the
            # hide_membership existence-oracle guard below -- that guard is
            # specifically about identity/membership denials, not a
            # transient/self-healing database-plane or sync-state condition.
            # Previously uncaught here; would have fallen through to a
            # generic 500 identical to a real application bug.
            raise ContextMediaAccessError(
                exc.error_code,
                str(exc),
                status_code=503,
            ) from exc
        except PermissionError as exc:
            self._raise_permission_error(exc, hide_membership=hide_membership)
        except RejectedByPolicyError as exc:
            self._raise_policy_error(exc, hide_membership=hide_membership)

    @staticmethod
    def _raise_permission_error(
        exc: PermissionError, *, hide_membership: bool
    ) -> NoReturn:
        error_code = str(exc).split(":", 1)[0] or "workspace_membership_missing"
        if hide_membership and error_code in {
            "workspace_membership_missing",
            "token_workspace_denied",
            "mcp_client_workspace_denied",
        }:
            raise _not_found("media not found") from exc
        raise ContextMediaAccessError(
            error_code,
            str(exc) or "workspace access denied",
            status_code=403,
        ) from exc

    @staticmethod
    def _raise_policy_error(
        exc: RejectedByPolicyError, *, hide_membership: bool
    ) -> NoReturn:
        if hide_membership and exc.error_code in {
            "workspace_membership_missing",
            "token_workspace_denied",
            "mcp_client_workspace_denied",
        }:
            raise _not_found("media not found") from exc
        raise ContextMediaAccessError(
            exc.error_code,
            str(exc) or "workspace access denied",
            status_code=403,
        ) from exc


def resolve_public_context_media_scope(
    db: DatabaseProvider,
    *,
    workspace_slug: str,
    page_slug: str,
) -> PublicContextMediaScope | None:
    """Resolve the exact public-exposure grant for one page's media, if any.

    Used to span one query across ``Workspace`` (identity-plane) and
    ``ContextPage``/``ContextPageVersion`` (workspace-plane) -- impossible
    once those tables can live in different databases. ``workspace`` resolves first,
    on the control session, because it is the only identifier the workspace
    session can be opened with; the page/version read then runs on
    ``db.session_for(workspace.id)``. Both rows are handed together, from
    this same call, into ``valid_public_exposed_version_id``/
    ``exposure_scope_for_workspace`` -- the actual public-authorization
    decision -- so that decision is made from one consistent pair of reads,
    not from a value cached across requests or computed from only one side.
    """

    with db.control_session_factory()() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == workspace_slug)
        )
    if workspace is None:
        return None
    with db.session_for(workspace.id)() as session:
        row = session.execute(
            select(ContextPage, ContextPageVersion)
            .join(
                ContextPageVersion,
                ContextPageVersion.id == ContextPage.public_exposed_version_id,
            )
            .where(
                ContextPage.workspace_id == workspace.id,
                ContextPage.slug == page_slug,
                ContextPageVersion.page_id == ContextPage.id,
                ContextPageVersion.version_status == "published_snapshot",
            )
        ).first()
        if row is None:
            return None
        page, _version = row
        exposed_version_id = valid_public_exposed_version_id(page, workspace)
        if exposed_version_id is None:
            return None
        return PublicContextMediaScope(
            workspace_id=workspace.id,
            page_id=page.id,
            page_version_id=exposed_version_id,
            exposure_scope=exposure_scope_for_workspace(workspace),
        )


def _not_found(message: str) -> ContextMediaAccessError:
    return ContextMediaAccessError("not_found", message, status_code=404)


__all__ = [
    "ContextMediaAccessError",
    "ContextMediaAccessPolicy",
    "PublicContextMediaScope",
    "resolve_public_context_media_scope",
]
