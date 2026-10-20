"""Instance-level (server-admin) endpoints.

Session-only by design: gated by ``require_server_admin`` and mounted under the
scope-exempt ``/api/v1/admin`` prefix, so bearer tokens (which carry no session)
are rejected. These describe instance/system state shared by all workspaces — the
app process cannot mutate ``/etc`` under the systemd sandbox, so it returns the
``dtxwiki`` commands the operator runs, never applying changes itself.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response

from backend.api.auth import (
    PasswordRecoveryServiceDep,
    require_recent_server_admin_reauthentication,
    require_server_admin,
)
from backend.api.context_media import _error_response as context_media_error_response
from backend.api.deps import (
    DatabaseProviderDep,
    RestActorDep,
    ServicesDep,
    SessionFactoryDep,
    WorkspaceAdminAccessDep,
    WorkspaceSessionFactoryDep,
)
from backend.api.health import _ready_payload as health_ready_payload
from backend.app.errors import build_rest_error_response
from backend.app.settings import oauth_token_lifetime_report
from backend.mcp.envelope import advisory_warnings
from backend.schemas.admin_users import (
    AdminPasswordResetRequest,
    AdminPasswordResetResponse,
    AdminUserListResponse,
)
from backend.schemas.common import RESTErrorBody
from backend.schemas.context_media import (
    AdminContextMediaAttachmentListResponse,
    ContextMediaDeleteResponse,
    ContextMediaOrphanListResponse,
)
from backend.schemas.deployment import DeploymentStatusResponse
from backend.schemas.jobs import (
    AdminJobListResponse,
    JobQueueSummaryResponse,
    JobRetryResponse,
    JobStatus,
)
from backend.services import deployment as deployment_service
from backend.services import update_check_service
from backend.services.admin_context_media import (
    AdminAttachmentError,
    AdminAttachmentUnavailable,
    delete_admin_attachment,
    list_admin_attachments,
)
from backend.services.admin_user_recovery import AdminUserRecoveryService
from backend.services.audit_service import AuditService
from backend.services.context_media_orphan_sweep import (
    list_orphans as list_context_media_orphans,
)
from backend.services.context_media_orphan_sweep import orphan_read_projection
from backend.services.job_queue import JobQueueService, JobRetryUnsupportedTypeError
from backend.services.job_read_service import JobReadService
from backend.services.mail_delivery import EmailDeliveryService
from backend.services.password_recovery_service import (
    PasswordRecoveryAuthorizationError,
    PasswordRecoveryConfigurationError,
    PasswordRecoveryError,
    PasswordRecoveryRecentReauthenticationRequiredError,
    PasswordRecoveryUserNotFoundError,
)
from backend.services.session_service import (
    AuthenticatedUser,
    VerifiedRecentReauthentication,
)
from backend.utils.actor_display import resolve_actor_display_names

router = APIRouter(prefix="/admin", tags=["admin"])

REST_ERROR_RESPONSES = {
    401: {"model": RESTErrorBody},
    403: {"model": RESTErrorBody},
    404: {"model": RESTErrorBody},
    422: {"model": RESTErrorBody},
}


def deployment_status_data(request: Request, session_factory) -> dict:
    """Build the read-only deployment-status payload from process settings."""

    settings = request.app.state.settings
    ready_payload, _ = health_ready_payload(request)
    suggestions = deployment_service.build_suggested_commands(settings)
    signup = deployment_service.signup_configuration(settings)
    with session_factory() as session:
        outbox_status = EmailDeliveryService(settings=settings).status(session)
    recovery = deployment_service.recovery_configuration(
        settings,
        outbox_queued=outbox_status.queued,
        outbox_failed=outbox_status.failed,
        latest_delivery_status=outbox_status.latest_attempt_status,
        latest_delivery_attempt_at=outbox_status.latest_attempt_at,
    )
    # Cache-only + fail-open: never block this request on the package index.
    update_info = update_check_service.check_for_update(settings, allow_network=False)
    return {
        "public_base_url": settings.public_base_url,
        "mcp_url": deployment_service.mcp_url(settings),
        "app_mode": settings.app_mode,
        "app_env": settings.app_env,
        "is_production": settings.is_production,
        "behind_proxy": settings.behind_proxy,
        "https_configured": deployment_service.https_configured(settings),
        "tls_mode": settings.tls_mode,
        "oauth_available": deployment_service.oauth_available(settings),
        "oauth_unavailable_reason": (
            deployment_service.oauth_unavailable_reason(settings)
        ),
        "oauth_dcr_enabled": deployment_service.oauth_dcr_enabled(settings),
        "oauth_token_lifetimes": oauth_token_lifetime_report(settings),
        "health_ready": ready_payload.status == "ready",
        "health_components": ready_payload.components,
        "env_file_hint": "/etc/dtxwiki/dtxwiki.env",
        "signup": {
            "configured": signup.configured,
            "effective": signup.effective,
            "source": signup.source,
            "mode_defaults": signup.mode_defaults,
            "restart_required": signup.restart_required,
            "impact": signup.impact,
            "systemd_command": signup.systemd_command,
            "compose_command": signup.compose_command,
        },
        "recovery": {
            "mode": recovery.mode,
            "admin_assisted": recovery.admin_assisted,
            "link_delivery_available": recovery.link_delivery_available,
            "mail_provider": recovery.mail_provider,
            "mail_status": recovery.mail_status,
            "mail_connection_status": recovery.mail_connection_status,
            "mail_last_attempt_at": recovery.mail_last_attempt_at,
            "mail_from_address": recovery.mail_from_address,
            "smtp_credentials_configured": (recovery.smtp_credentials_configured),
            "canonical_public_url": recovery.canonical_public_url,
            "https_ready": recovery.https_ready,
            "outbox_queued": recovery.outbox_queued,
            "outbox_failed": recovery.outbox_failed,
            "worker_required": recovery.worker_required,
            "restart_required": recovery.restart_required,
            "systemd_command": recovery.systemd_command,
            "compose_command": recovery.compose_command,
        },
        "suggested_commands": [
            {
                "title": item.title,
                "description": item.description,
                "command": item.command,
                "blast_radius": item.blast_radius,
            }
            for item in suggestions
        ],
        "current_version": settings.app_version,
        "latest_version": update_info.latest_version if update_info else None,
        "update_available": bool(update_info and update_info.update_available),
    }


@router.get(
    "/deployment",
    response_model=DeploymentStatusResponse,
    responses=REST_ERROR_RESPONSES,
)
def admin_deployment_status(
    request: Request,
    session_factory: SessionFactoryDep,
    _: Annotated[AuthenticatedUser, Depends(require_server_admin)],
):
    """Instance (all-workspaces) deployment status + reconfiguration guidance.

    Server-admin only. Read-only: the app describes the current config and hands
    back ``dtxwiki config set`` / ``setup production`` commands for the operator.
    """

    return {"data": deployment_status_data(request, session_factory)}


@router.get(
    "/users",
    response_model=AdminUserListResponse,
    responses=REST_ERROR_RESPONSES,
)
def admin_search_users(
    response: Response,
    session_factory: SessionFactoryDep,
    admin: Annotated[
        VerifiedRecentReauthentication,
        Depends(require_recent_server_admin_reauthentication),
    ],
    query: str = Query(default="", max_length=320),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    result = AdminUserRecoveryService(session_factory=session_factory).search_users(
        actor_user_id=admin.user_id,
        query=query,
        limit=limit,
        offset=offset,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {
        "data": {
            "items": [
                {
                    "user_id": record.user.id,
                    "email": record.user.email,
                    "display_name": record.user.display_name,
                    "status": record.user.status,
                    "email_verified_at": record.user.email_verified_at,
                    "workspaces": [
                        {
                            "workspace_id": membership.workspace_id,
                            "slug": membership.slug,
                            "title": membership.title,
                            "role": membership.role,
                        }
                        for membership in record.workspaces
                    ],
                }
                for record in result.items
            ],
            "total": result.total,
            "limit": result.limit,
            "offset": result.offset,
        }
    }


@router.post(
    "/users/{user_id}/password-reset",
    response_model=AdminPasswordResetResponse,
    responses=REST_ERROR_RESPONSES,
)
def admin_issue_password_reset(
    request_body: AdminPasswordResetRequest,
    response: Response,
    user_id: Annotated[UUID, Path()],
    recovery_service: PasswordRecoveryServiceDep,
    admin: Annotated[
        VerifiedRecentReauthentication,
        Depends(require_recent_server_admin_reauthentication),
    ],
):
    try:
        issued = recovery_service.issue_password_reset_for_user(
            target_user_id=user_id,
            delivery=request_body.delivery,
            expires_in_seconds=request_body.expires_in_seconds,
            requested_by_user_id=admin.user_id,
            requested_by_session_generation=admin.session_generation,
            actor_kind="human",
            actor_id=str(admin.user_id),
            revoke_api_tokens=request_body.revoke_api_tokens,
        )
    except PasswordRecoveryError as exc:
        status_code = 422
        if isinstance(exc, PasswordRecoveryRecentReauthenticationRequiredError):
            status_code = 401
        elif isinstance(exc, PasswordRecoveryAuthorizationError):
            status_code = 403
        elif isinstance(exc, PasswordRecoveryUserNotFoundError):
            status_code = 404
        elif isinstance(exc, PasswordRecoveryConfigurationError):
            status_code = 422
        return build_rest_error_response(
            error_code=exc.error_code,
            message=str(exc) or exc.error_code,
            status_code=status_code,
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {
        "data": {
            "user_id": issued.user_id,
            "delivery": issued.delivery,
            "expires_at": issued.expires_at,
            "reset_url": (
                issued.reset_url if request_body.delivery == "link" else None
            ),
            "revoke_api_tokens": request_body.revoke_api_tokens,
        }
    }


@router.get(
    "/jobs/summary",
    response_model=JobQueueSummaryResponse,
    responses=REST_ERROR_RESPONSES,
)
def admin_jobs_summary(
    request: Request,
    session_factory: SessionFactoryDep,
    db: DatabaseProviderDep,
    _: Annotated[AuthenticatedUser, Depends(require_server_admin)],
):
    """Instance-wide background-job queue depth, counted across every
    workspace. Server-admin only. There is no per-workspace counterpart:
    ``GET /workspaces/{id}/jobs`` pages through one workspace's individual
    jobs and returns no counts.

    A workspace whose data store cannot be reached is left out of the counts
    instead of failing the request: its id is returned in
    ``unreachable_workspace_ids`` and the envelope carries a
    ``job_fanout_incomplete`` warning. Without that pairing an operator would
    read a partial queue depth as a genuinely quiet queue.
    """
    lease_timeout_seconds = request.app.state.settings.worker_lease_timeout_seconds
    # Deliberately thin and separate from GET /jobs: one small fan-out surface
    # rather than a second copy of the listing's paging machinery.
    # `session_factory` opens the control session for the Workspace roster
    # (identity plane); every actual count comes from session_for(workspace_id)
    # per workspace inside JobReadService.queue_summary.
    with session_factory() as control_session:
        result = JobReadService().queue_summary(
            control_session=control_session,
            session_for=db.session_for,
            lease_timeout_seconds=lease_timeout_seconds,
        )
    warning_codes = (
        ["job_fanout_incomplete"] if result["unreachable_workspace_ids"] else []
    )
    return {
        "envelope": {"warnings": advisory_warnings(warning_codes)},
        "data": result,
    }


@router.get(
    "/jobs",
    response_model=AdminJobListResponse,
    responses=REST_ERROR_RESPONSES,
)
def admin_list_jobs(
    request: Request,
    session_factory: SessionFactoryDep,
    db: DatabaseProviderDep,
    _: Annotated[AuthenticatedUser, Depends(require_server_admin)],
    workspace_id: UUID | None = None,
    status: JobStatus | None = None,
    job_type: str | None = None,
    problems_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    """Cross-workspace job listing for the operator screen: terminal
    failures with a stable ``error_code``, jobs running past their lease, and
    everything ``manual retry`` needs. Not workspace-scoped -- that is
    ``GET /workspaces/{id}/jobs`` -- this is the server-admin surface.

    A workspace whose data store cannot be reached is skipped rather than
    failing the whole listing; its id is returned in
    ``unreachable_workspace_ids`` alongside a ``job_fanout_incomplete``
    envelope warning, so the caller can tell "short because that is all there
    is" apart from "short because part of the fleet did not answer".
    """
    lease_timeout_seconds = request.app.state.settings.worker_lease_timeout_seconds
    # `session_factory` opens the control session for the Workspace roster
    # (identity plane); each workspace's actual Job rows come from
    # session_for(workspace_id) inside JobReadService.list_all_jobs.
    with session_factory() as control_session:
        result = JobReadService().list_all_jobs(
            control_session=control_session,
            session_for=db.session_for,
            status=status,
            job_type=job_type,
            workspace_id=workspace_id,
            problems_only=problems_only,
            limit=limit,
            cursor=cursor,
            lease_timeout_seconds=lease_timeout_seconds,
        )
    warning_codes = (
        ["job_fanout_incomplete"] if result["unreachable_workspace_ids"] else []
    )
    return {
        "envelope": {"warnings": advisory_warnings(warning_codes)},
        "data": result,
    }


@router.get(
    "/context-media/orphans",
    response_model=ContextMediaOrphanListResponse,
    responses=REST_ERROR_RESPONSES,
)
def admin_list_context_media_orphans(
    workspace_id: UUID,
    session_factory: WorkspaceSessionFactoryDep,
    actor: RestActorDep,
    access: WorkspaceAdminAccessDep,
    control_session_factory: SessionFactoryDep,
    _: Annotated[AuthenticatedUser, Depends(require_server_admin)],
):
    with session_factory() as session:
        items = list_context_media_orphans(
            session,
            workspace_id=workspace_id,
            actor=replace(
                actor,
                workspace_id=workspace_id,
                role=access.role,
                workspace_role=access.role,
            ),
        )
    # Uploader labels come off the CONTROL session -- `users` is identity
    # plane, the listing above is workspace plane. Same split as the
    # workspace-scoped endpoint in backend/api/read.py.
    with control_session_factory() as control_session:
        names = resolve_actor_display_names(
            control_session,
            ((item.created_by_actor_kind, item.created_by_actor_id) for item in items),
        )
    return {
        "data": {
            "items": [
                orphan_read_projection(
                    item,
                    uploaded_by_display_name=names[
                        (item.created_by_actor_kind, item.created_by_actor_id)
                    ],
                )
                for item in items
            ],
        }
    }


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobRetryResponse,
    responses=REST_ERROR_RESPONSES,
)
def admin_retry_job(
    request: Request,
    job_id: Annotated[UUID, Path()],
    workspace_id: UUID,
    session_factory: WorkspaceSessionFactoryDep,
    admin: Annotated[AuthenticatedUser, Depends(require_server_admin)],
):
    """Requeue a terminally-failed job for one more attempt. Server-admin only.

    ``retry_count`` is NOT reset -- attempts accumulate, so this retry does
    not start a fresh retry cycle: the job's very next failure spends the
    last of its budget and fails it terminally again.

    Only job types whose failure state lives entirely on the job row can be
    requeued; ``retryable`` on each listed job says which, and any other type
    is rejected rather than restarted. ``workspace_id`` is required and must
    be the workspace that owns ``job_id`` -- the admin listing returns it on
    every row. A ``workspace_id`` that does not own the job is answered
    exactly like an unknown job, so this endpoint cannot be used to discover
    which workspace a job id belongs to.
    """
    lease_timeout_seconds = request.app.state.settings.worker_lease_timeout_seconds
    # The caller names the workspace rather than this endpoint resolving it:
    # Job is workspace-plane data, so `session_factory` here is the
    # session_for(workspace_id) seam GET /workspaces/{id}/jobs already uses,
    # not the control session the admin listing/summary fan out from. Resolving
    # job_id server-side would mean a per-shard search on every Retry click,
    # and the admin listing already hands the client workspace_id on each row.
    #
    # JOB_TYPES_WITH_DOMAIN_FAILURE_STATE stays excluded because those handlers
    # also wrote a domain-specific terminal marker (e.g. a
    # LibraryAttempt(stage="conversion") left status="failed") that this generic Job-row
    # requeue cannot undo -- retrying one would resume a business handler
    # against domain rows it still believes are permanently done.
    with session_factory.begin() as session:
        try:
            retried = JobQueueService().manual_retry(
                session=session, job_id=job_id, workspace_id=workspace_id
            )
        except JobRetryUnsupportedTypeError as exc:
            return build_rest_error_response(
                error_code="job_retry_unsupported_type",
                message=(
                    f"{exc.job_type} jobs cannot be retried from this screen "
                    "yet -- their failure also updates a domain-specific "
                    "record this action does not know how to reset"
                ),
                status_code=422,
            )
        if retried is None:
            return build_rest_error_response(
                error_code="job_not_found",
                message="job not found or not currently in a failed state",
                status_code=404,
            )
        AuditService().emit(
            session=session,
            workspace_id=retried.workspace_id,
            actor_kind="human",
            actor_id=str(admin.user_id),
            action="admin.jobs.retried",
            target_kind="job",
            target_id=retried.id,
            payload={"job_type": retried.job_type, "retry_count": retried.retry_count},
            event_summary="A server admin manually retried a failed background job",
            retention_class="standard",
        )
        return {
            "data": JobReadService().job_read_by_id(
                session=session,
                job_id=retried.id,
                lease_timeout_seconds=lease_timeout_seconds,
            )
        }


@router.get(
    "/context-media/attachments",
    response_model=AdminContextMediaAttachmentListResponse,
    responses={**REST_ERROR_RESPONSES, 503: {"model": RESTErrorBody}},
)
def admin_list_context_media_attachments(
    db: DatabaseProviderDep,
    svc: ServicesDep,
    response: Response,
    admin: Annotated[AuthenticatedUser, Depends(require_server_admin)],
    q: Annotated[str, Query(max_length=200)] = "",
    mine: bool = False,
    orphan_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
):
    response.headers["Cache-Control"] = "no-store"
    try:
        return {
            "data": list_admin_attachments(
                db=db,
                repository_factory=svc.context_media_repository_factory,
                actor_id=admin.user_id,
                q=q,
                mine=mine,
                orphan_only=orphan_only,
                limit=limit,
                cursor=cursor,
            )
        }
    except ValueError:
        return build_rest_error_response(
            error_code="validation_failed",
            message="Invalid attachment cursor",
            status_code=422,
        )
    except AdminAttachmentUnavailable:
        return build_rest_error_response(
            error_code="media_unavailable",
            message="A workspace could not be read. Refresh to try again.",
            status_code=503,
        )


@router.delete(
    "/context-media/attachments/{asset_id}",
    response_model=ContextMediaDeleteResponse,
    responses={
        **REST_ERROR_RESPONSES,
        **{code: {"model": RESTErrorBody} for code in (409, 412, 503)},
    },
)
def admin_delete_context_media_attachment(
    asset_id: UUID,
    workspace_id: UUID,
    db: DatabaseProviderDep,
    svc: ServicesDep,
    response: Response,
    admin: Annotated[AuthenticatedUser, Depends(require_server_admin)],
    usage_token: Annotated[str, Header(alias="If-Match", min_length=1)],
):
    response.headers["Cache-Control"] = "no-store"
    try:
        delete_admin_attachment(
            db=db,
            repository_factory=svc.context_media_repository_factory,
            workspace_id=workspace_id,
            asset_id=asset_id,
            actor_id=admin.user_id,
            usage_token=usage_token.strip('"'),
        )
    except AdminAttachmentError as exc:
        return context_media_error_response(exc)
    except AdminAttachmentUnavailable:
        return build_rest_error_response(
            error_code="media_unavailable",
            message="Workspace is unavailable",
            status_code=503,
        )
    return {
        "data": {
            "id": asset_id,
            "association_state": "tombstoned",
            "gc_state": "queued",
        }
    }
