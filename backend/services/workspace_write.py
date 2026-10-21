"""Tier-B workspace lifecycle operations.

These operations deliberately keep registry, membership, and workspace data in
one transaction for the single-database profile. A split deployment must own
workspace create/delete/default orchestration in its overlay so control-plane
and data-plane work can be coordinated explicitly.
"""

import re
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.domain.input_policy import validate_text
from backend.models import (
    AgentInputBundle,
    AgentPage,
    AgentPageVersion,
    AskSession,
    ChangeSet,
    ContextIngressProposal,
    ContextMediaLogicalCharge,
    ContextMediaLogicalQuotaUsage,
    ContextPage,
    FeedbackItem,
    Job,
    LibraryDiscoveryCandidate,
    LibraryDiscoveryRun,
    LibraryItem,
    LibraryLogicalCharge,
    LibraryLogicalQuotaUsage,
    LibrarySyncRun,
    LintRun,
    MCPClient,
    MCPToolCall,
    MediaStagingLease,
    OAuthConsent,
    ReviewItem,
    StoredBlobRow,
    TreePolicy,
    User,
    Workspace,
    WorkspaceCredentialGrant,
    WorkspaceMediaQuotaUsage,
    WorkspaceMediaWriteIntent,
    WorkspaceMembership,
    WorkspaceTreeNode,
)
from backend.models.base import utc_now
from backend.models.mcp_chat import (
    McpChatArtifactLink,
    McpChatSession,
    McpChatToolCallArtifact,
)
from backend.policy.auth import ActorContext
from backend.policy.rbac import (
    grant_workspace_membership,
    normalize_role,
)
from backend.repositories.audit_repo import insert_audit_event
from backend.repositories.workspace_repo import get_workspace_by_slug, insert_workspace
from backend.schemas.input_policy import validate_request_inputs
from backend.schemas.read import WorkspaceCreateRequest, WorkspaceSummary
from backend.scripts.seed_dev import DEFAULT_AGENT_PAGE_SLUG, seed_default_workspace
from backend.services.db_write_intent import acquire_write_intent
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError
from backend.services.tree_service import ensure_context_inbox_node
from backend.services.workspace_purge import purge_workspace_data
from backend.services.workspace_read import WorkspaceReadService
from backend.utils.database_errors import is_single_column_unique_violation

SUPPORTED_WORKSPACE_LANGUAGES = {"en", "ko"}


class WorkspaceCreateConflictError(RuntimeError):
    error_code = "workspace_conflict"


class WorkspaceDeleteRejectedError(RuntimeError):
    error_code = "workspace_delete_rejected"

    def __init__(
        self,
        message: str,
        *,
        blockers: dict[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.blockers = dict(blockers or {})


class DefaultWorkspaceMembershipMissingError(RuntimeError):
    error_code = "workspace_membership_missing"


class DefaultWorkspaceUserNotFoundError(RuntimeError):
    error_code = "user_not_found"


def set_user_default_workspace(
    session_factory,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> None:
    """Set a user's default (login-landing) workspace.

    Keeps model access in the service layer: the user must be a member of
    the target workspace. Raises a typed error the API maps to a response.
    """
    with session_factory.begin() as session:
        # Keep the same lock order as workspace deletion.  Without the
        # workspace lock, PostgreSQL can update the user first and then wait
        # on the default_workspace_id FK while a concurrent delete holds the
        # workspace and waits on that user row.
        acquire_write_intent(session)
        workspace = session.scalar(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            raise DefaultWorkspaceMembershipMissingError(
                "You are not a member of the selected workspace."
            )
        user = session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            raise DefaultWorkspaceUserNotFoundError("User not found.")
        membership = session.scalar(
            select(WorkspaceMembership)
            .where(
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if membership is None:
            raise DefaultWorkspaceMembershipMissingError(
                "You are not a member of the selected workspace."
            )
        user.default_workspace_id = workspace_id


class WorkspaceWriteService:
    def create_workspace(
        self,
        *,
        session: Session,
        request: WorkspaceCreateRequest,
        actor: ActorContext,
    ) -> WorkspaceSummary:
        validate_request_inputs(request)
        if actor.user_id is None:
            raise RejectedByPolicyError(
                "human session is required",
                error_code="workspace_user_required",
            )
        slug = normalize_workspace_slug(
            request.slug if request.slug is not None else request.title,
            generated=request.slug is None,
        )
        if get_workspace_by_slug(session, slug) is not None:
            raise WorkspaceCreateConflictError("workspace slug already exists")
        try:
            # Insert before seeding: seeding is get-or-create by slug, so a
            # competitor that committed after the check above would otherwise
            # be adopted and re-owned instead of failing the UNIQUE slug.
            insert_workspace(session, slug=slug, title=request.title.strip())
            workspace = seed_default_workspace(
                session,
                slug=slug,
                title=request.title.strip(),
            )
            ensure_context_inbox_node(session, workspace.id)
            if request.goal is not None:
                workspace.goal = request.goal.strip()
            if request.default_language is not None:
                default_language = request.default_language.strip()
                if default_language not in SUPPORTED_WORKSPACE_LANGUAGES:
                    raise RejectedByPolicyError(
                        "unsupported language",
                        error_code="unsupported_language",
                    )
                workspace.default_language = default_language
            if request.visibility is not None:
                workspace.visibility = request.visibility.strip()
            workspace.created_by_actor_id = actor.user_id
            workspace.owner_actor_id = actor.user_id
            grant_workspace_membership(
                session,
                user_id=actor.user_id,
                workspace_id=workspace.id,
                role="owner",
            )
            session.flush()
        except IntegrityError as exc:
            if not is_single_column_unique_violation(
                exc, table="workspace", column="slug"
            ):
                raise
            raise WorkspaceCreateConflictError("workspace slug already exists") from exc
        return WorkspaceReadService._summary(workspace, role="owner")

    def delete_workspace(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        actor: ActorContext,
        purge_contents: bool = False,
    ) -> UUID:
        """Delete a workspace after re-authorizing the owner in this transaction.

        ``purge_contents`` is the owner's second, explicit confirmation. Without
        it a workspace holding any content or operational rows is refused and
        the counts come back as blockers -- the safe default, and the only
        behaviour before this flag existed. With it, those rows are deleted
        first. That second step is not a convenience: rows such as ``job`` and
        ``lint_run`` accumulate on their own and no screen can clear them, so a
        blocker-only gate meant a workspace with any history could never be
        deleted at all.
        """
        self.require_delete_authorization(
            session=session,
            workspace_id=workspace_id,
            actor=actor,
        )
        media_intent = self._begin_media_delete(
            session=session,
            workspace_id=workspace_id,
        )
        blockers = self._dependency_counts(session, workspace_id)
        if blockers and not purge_contents:
            summary = ", ".join(f"{name}: {count}" for name, count in blockers.items())
            message = (
                f"workspace has retained dependencies; clear blockers first ({summary})"
            )
            raise WorkspaceDeleteRejectedError(
                message,
                blockers=blockers,
            )
        if purge_contents:
            # PURGE_AUTHZ_PLAN S-2: the route validates and consumes a
            # single-use purge grant before calling here, and rebinds the
            # actor it passes in with this scope -- but that is enforcement
            # at the caller, not this service. Requiring it again here closes
            # the "call the service directly, skip the route" path (T-3b);
            # without this check `purge_contents=True` alone was sufficient.
            if "workspace-purge" not in actor.scopes:
                raise RejectedByPolicyError(
                    "a workspace purge grant is required to purge contents",
                    error_code="purge_grant_required",
                )
            purged = purge_workspace_data(session, workspace_id)
            # Written before the workspace row goes away, and deliberately not
            # cleaned up afterwards: audit_event.workspace_id is ON DELETE SET
            # NULL, so this record detaches and survives as the only trace that
            # the contents were destroyed and by whom.
            insert_audit_event(
                session,
                workspace_id=workspace_id,
                actor_kind=actor.actor_kind,
                actor_id=str(actor.actor_id),
                action="workspace.purge",
                target_kind="workspace",
                target_id=workspace_id,
                payload={
                    "blockers": blockers,
                    "deleted_rows": purged,
                    "deleted_row_total": sum(purged.values()),
                },
                event_summary="workspace contents purged before deletion",
            )
        self._delete_seed_workspace(
            session=session,
            workspace_id=workspace_id,
            media_intent=media_intent,
        )
        return workspace_id

    @staticmethod
    def require_delete_authorization(
        *,
        session: Session,
        workspace_id: UUID,
        actor: ActorContext,
    ) -> Workspace:
        """Lock and verify the live browser-session owner for deletion.

        The router's dependency check is intentionally not trusted here: it
        runs in an earlier transaction, so an owner can be demoted or removed
        after that check and before destructive work begins.  Every caller,
        including direct service callers, must pass this gate in the same
        transaction that will delete the workspace.

        Media writes take locks in media-intent -> workspace order, while
        membership mutations take workspace -> membership.  Deletion follows
        the combined media-intent -> workspace -> user -> membership order so
        it serializes with both workflows without creating a lock cycle.
        SQLite ignores ``FOR UPDATE``, so acquire its database writer lock
        before any decision read.
        """

        if (
            actor.token_kind != "session"
            or actor.provenance != "browser_session"
            or actor.user_id is None
        ):
            raise RejectedByPolicyError(
                "a browser session is required to delete a workspace",
                error_code="human_session_required",
            )

        acquire_write_intent(session)
        media_intent = WorkspaceWriteService._lock_media_delete_intent(
            session=session,
            workspace_id=workspace_id,
        )
        if media_intent is None:
            # Every workspace receives this row when it is created (and older
            # rows were backfilled by migration 0009).  Inserting it after the
            # workspace lock would invert the media writer's intent ->
            # workspace lock order, so a missing row is a broken invariant,
            # not a condition deletion may repair lazily.
            if session.get(Workspace, workspace_id) is None:
                raise ReadNotFoundError("workspace not found")
            raise RuntimeError("workspace_media_intent_not_configured")
        workspace = session.scalar(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            raise ReadNotFoundError("workspace not found")

        user = session.scalar(
            select(User).where(User.id == actor.user_id).with_for_update()
        )
        membership = session.scalar(
            select(WorkspaceMembership)
            .where(
                WorkspaceMembership.user_id == actor.user_id,
                WorkspaceMembership.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if user is None or membership is None:
            raise RejectedByPolicyError(
                "workspace membership is required",
                error_code="workspace_membership_missing",
            )
        if user.status != "active" or normalize_role(membership.role) != "owner":
            raise RejectedByPolicyError(
                "an active workspace owner is required",
                error_code="workspace_role_insufficient",
            )
        return workspace

    @staticmethod
    def _dependency_counts(session: Session, workspace_id: UUID) -> dict[str, int]:
        counts = {
            "context_pages": count_rows(session, ContextPage, workspace_id),
            "library": count_rows(session, LibraryItem, workspace_id),
            "ask_sessions": count_rows(session, AskSession, workspace_id),
            "context_ingress": count_rows(
                session,
                ContextIngressProposal,
                workspace_id,
            ),
            "changes": count_rows(session, ChangeSet, workspace_id),
            "reviews": count_rows(session, ReviewItem, workspace_id),
            "feedback": count_rows(session, FeedbackItem, workspace_id),
            "lint_runs": count_rows(session, LintRun, workspace_id),
            "jobs": count_rows(session, Job, workspace_id),
            "library_discovery_runs": count_rows(
                session,
                LibraryDiscoveryRun,
                workspace_id,
            ),
            "library_discovery_candidates": count_rows(
                session,
                LibraryDiscoveryCandidate,
                workspace_id,
            ),
            "library_sync_runs": count_rows(session, LibrarySyncRun, workspace_id),
            "agent_bundles": count_rows(session, AgentInputBundle, workspace_id),
            "mcp_tool_calls": count_rows(session, MCPToolCall, workspace_id),
            "mcp_chats": count_rows(session, McpChatSession, workspace_id),
            # audit_events is deliberately NOT a blocker (WS-02): audit rows
            # are a byproduct of ordinary admin actions (member add/role
            # change/remove, access changes) that a user has no way to clear,
            # so counting them here made workspace deletion permanently
            # unreachable after the first such action. The audit_event.
            # workspace_id FK is ON DELETE SET NULL (see backend/models/
            # audit.py), so the database detaches these append-only rows when
            # the workspace row is deleted -- the service neither counts nor
            # deletes them itself.
            # D-01v2/D-01v3 (P10.3 T1): oauth_codes/oauth_refresh_tokens lost
            # their workspace_id column entirely -- OAuth's family unit is
            # now (user, client), not tied to any single workspace, so
            # neither can block or need cleanup on this workspace's deletion.
            # oauth_consents keeps its per-(user, client, workspace) row and
            # is still a real blocker (ON DELETE CASCADE handles cleanup).
            "oauth_consents": count_rows(session, OAuthConsent, workspace_id),
            "custom_agent_pages": count_custom_agent_pages(session, workspace_id),
            "media": count_media_dependencies(session, workspace_id),
        }
        return {name: count for name, count in counts.items() if count > 0}

    @staticmethod
    def _begin_media_delete(
        *,
        session: Session,
        workspace_id: UUID,
    ) -> WorkspaceMediaWriteIntent:
        """Establish the DB-authoritative media generation fence.

        Existing workspaces receive this row from migration 0009 and every
        current creation path inserts it.  A missing row is therefore a broken
        invariant.  It must fail before destructive work instead of being
        inserted after the workspace lock, which would invert the media
        writer's intent -> workspace lock order.
        """

        intent = WorkspaceWriteService._lock_media_delete_intent(
            session=session,
            workspace_id=workspace_id,
        )
        if intent is None:
            raise RuntimeError("workspace_media_intent_not_configured")
        if intent.state == "open":
            intent.state = "deleting"
            intent.delete_generation += 1
            intent.updated_at = utc_now()
        elif intent.state != "deleting":
            raise RuntimeError("invalid_workspace_media_intent_state")
        session.flush()
        return intent

    @staticmethod
    def _lock_media_delete_intent(
        *,
        session: Session,
        workspace_id: UUID,
    ) -> WorkspaceMediaWriteIntent | None:
        """Lock the media fence before the workspace row on PostgreSQL."""

        return session.scalar(
            select(WorkspaceMediaWriteIntent)
            .where(WorkspaceMediaWriteIntent.workspace_id == workspace_id)
            .with_for_update()
        )

    @staticmethod
    def _delete_seed_workspace(
        session: Session,
        workspace_id: UUID,
        *,
        media_intent: WorkspaceMediaWriteIntent,
    ) -> None:
        from backend.services.indexing.fts5 import (
            purge_workspace_search_projections,
        )

        purge_workspace_search_projections(session, workspace_id)
        WorkspaceWriteService._delete_empty_media_scope(
            session=session,
            workspace_id=workspace_id,
            media_intent=media_intent,
        )
        session.execute(
            update(User)
            .where(User.default_workspace_id == workspace_id)
            .values(default_workspace_id=None)
        )
        session.execute(
            update(User)
            .where(User.personal_workspace_id == workspace_id)
            .values(personal_workspace_id=None)
        )
        agent_page_ids = session.scalars(
            select(AgentPage.id).where(AgentPage.workspace_id == workspace_id)
        ).all()
        if agent_page_ids:
            session.execute(
                update(AgentPage)
                .where(AgentPage.id.in_(agent_page_ids))
                .values(current_active_version_id=None, latest_draft_version_id=None)
            )
            session.execute(
                delete(AgentPageVersion).where(
                    AgentPageVersion.agent_page_id.in_(agent_page_ids)
                )
            )
            session.execute(delete(AgentPage).where(AgentPage.id.in_(agent_page_ids)))
        # Delete the MCP chat graph BEFORE mcp_client / mcp_tool_call: chat
        # sessions RESTRICT-reference mcp_client, and the link/artifact rows
        # RESTRICT-reference mcp_tool_call, so they must go first (Codex #20).
        chat_session_ids = session.scalars(
            select(McpChatSession.id).where(McpChatSession.workspace_id == workspace_id)
        ).all()
        if chat_session_ids:
            session.execute(
                delete(McpChatArtifactLink).where(
                    McpChatArtifactLink.chat_session_id.in_(chat_session_ids)
                )
            )
            session.execute(
                delete(McpChatToolCallArtifact).where(
                    McpChatToolCallArtifact.chat_session_id.in_(chat_session_ids)
                )
            )
            session.execute(
                delete(McpChatSession).where(McpChatSession.id.in_(chat_session_ids))
            )
        # Codex #20: delete MCPToolCall rows explicitly (after the link/artifact
        # rows above that RESTRICT-reference them, before MCPClient). In practice
        # _dependency_counts refuses deletion while mcp_tool_calls > 0, so this is
        # a no-op on the seed-delete path today; making it explicit keeps this
        # routine self-consistent (it deletes everything it counts) and robust if
        # that dependency gate ever changes.
        session.execute(
            delete(MCPToolCall).where(MCPToolCall.workspace_id == workspace_id)
        )
        session.execute(delete(MCPClient).where(MCPClient.workspace_id == workspace_id))
        # T4.5/D-01v3: workspace_credential_grant is workspace-plane (like
        # MCPClient just above), so this single-database-profile method
        # deletes it explicitly rather than relying on its FK's own
        # ondelete="CASCADE" (backend/models/workspace_credential_grant.py)
        # -- that FK constraint only fires within the SAME physical
        # database, and this method already does not rely on cascades for
        # any other workspace-plane table in this section (MCPClient's FK
        # has no cascade at all). Redundant, not load-bearing, on the
        # single-database profile with SQLite FK enforcement on
        # (backend/app/db.py's PRAGMA foreign_keys=ON) or PostgreSQL; it IS
        # load-bearing under the plain sessionmaker test engines this
        # codebase's own test fixtures use (backend/tests/conftest.py's
        # session_factory does not enable that pragma), and matches this
        # method's own established convention of not depending on any
        # table's declared cascade.
        session.execute(
            delete(WorkspaceCredentialGrant).where(
                WorkspaceCredentialGrant.workspace_id == workspace_id
            )
        )
        session.execute(
            update(WorkspaceTreeNode)
            .where(WorkspaceTreeNode.workspace_id == workspace_id)
            .values(parent_id=None)
        )
        session.execute(
            delete(TreePolicy).where(TreePolicy.workspace_id == workspace_id)
        )
        session.execute(
            delete(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id
            )
        )
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id
            )
        )
        workspace = session.get(Workspace, workspace_id)
        if workspace is not None:
            session.delete(workspace)
        session.flush()

    @staticmethod
    def _delete_empty_media_scope(
        *,
        session: Session,
        workspace_id: UUID,
        media_intent: WorkspaceMediaWriteIntent,
    ) -> None:
        """Remove only provisioned, zero-content media control rows."""

        if (
            media_intent.workspace_id != workspace_id
            or media_intent.state != "deleting"
        ):
            raise RuntimeError("workspace_media_delete_fence_required")
        session.execute(
            delete(ContextMediaLogicalQuotaUsage).where(
                ContextMediaLogicalQuotaUsage.workspace_id == workspace_id
            )
        )
        session.execute(
            delete(LibraryLogicalQuotaUsage).where(
                LibraryLogicalQuotaUsage.workspace_id == workspace_id
            )
        )
        session.execute(
            delete(WorkspaceMediaQuotaUsage).where(
                WorkspaceMediaQuotaUsage.workspace_id == workspace_id
            )
        )
        session.delete(media_intent)
        session.flush()


def normalize_workspace_slug(value: str, *, generated: bool = False) -> str:
    if not generated:
        validate_text(value, field="slug", max_length=80, min_length=1)
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if generated:
        slug = slug[:80].strip("-")
    if not slug:
        raise WorkspaceCreateConflictError("workspace slug is required")
    if not generated:
        validate_text(slug, field="slug", max_length=80, min_length=1)
    return slug


def count_rows(session: Session, model, workspace_id: UUID) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.workspace_id == workspace_id)
        )
        or 0
    )


def count_media_dependencies(session: Session, workspace_id: UUID) -> int:
    """Count retained media roots or non-empty quota ledgers.

    Child install, inspection, physical-charge, and outbox rows cannot exist
    without a staging lease or StoredBlob root. Logical-charge provenance is a
    retained root even after release. Zero-valued quota rows are provisioning
    scaffolding and are drained by the terminal delete transaction.
    """

    retained_roots = sum(
        count_rows(session, model, workspace_id)
        for model in (
            MediaStagingLease,
            StoredBlobRow,
            LibraryLogicalCharge,
            ContextMediaLogicalCharge,
        )
    )
    non_empty_ledgers = sum(
        int(
            session.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.workspace_id == workspace_id,
                    model.charged_bytes > 0,
                )
            )
            or 0
        )
        for model in (
            WorkspaceMediaQuotaUsage,
            LibraryLogicalQuotaUsage,
            ContextMediaLogicalQuotaUsage,
        )
    )
    return retained_roots + non_empty_ledgers


def count_custom_agent_pages(session: Session, workspace_id: UUID) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(AgentPage)
            .where(
                AgentPage.workspace_id == workspace_id,
                AgentPage.slug != DEFAULT_AGENT_PAGE_SLUG,
            )
        )
        or 0
    )
