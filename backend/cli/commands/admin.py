"""Local user + workspace administration commands (`dtxwiki users|workspace`).

Extracted from cli/main.py (R3). main.py registers users_app/workspace_app here.
"""

from __future__ import annotations

import getpass
import json
import sys
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

import typer
from sqlalchemy import select

from backend.app.bootstrap import bootstrap as bootstrap_app
from backend.app.db import make_session_factory_from_settings
from backend.app.settings import Settings
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.services import server_admin_service
from backend.services.api_token_service import (
    ALLOWED_API_TOKEN_SCOPES,
    ApiTokenService,
    ApiTokenServiceError,
    InvalidApiTokenRequestError,
    WorkspaceGrantRequest,
)
from backend.services.credential_hashing import CredentialHasher
from backend.services.mail_delivery import EmailDeliveryService, MailDeliveryError
from backend.services.password_recovery_service import (
    DEFAULT_PASSWORD_RESET_TTL_SECONDS,
    MAX_PASSWORD_RESET_TTL_SECONDS,
    MIN_PASSWORD_RESET_TTL_SECONDS,
    PasswordRecoveryConfigurationError,
    PasswordRecoveryError,
    PasswordRecoveryService,
)
from backend.services.session_service import (
    DuplicateEmailError,
    InvalidCredentialsError,
    PasswordPolicyError,
    normalize_email,
)
from backend.services.user_admin_service import UserAdminService
from backend.services.workspace_membership_service import (
    WorkspaceMembershipNotFoundError,
    WorkspaceMembershipService,
)

users_app = typer.Typer(help="Manage local dtxWiki users.")
workspace_app = typer.Typer(help="Manage local dtxWiki workspaces.")
workspace_members_app = typer.Typer(help="Manage workspace members.")
server_admin_app = typer.Typer(
    help=(
        "Manage instance-level (server) admins — system settings, not "
        "workspace content."
    )
)
USER_ROLES = {"owner", "editor", "viewer"}


class PasswordResetDelivery(StrEnum):
    auto = "auto"
    email = "email"
    link = "link"


@users_app.command(name="add")
def users_add(
    email: Annotated[str, typer.Argument(help="User email.")],
    password: Annotated[
        str,
        typer.Option(
            "--password",
            prompt=True,
            hide_input=True,
            confirmation_prompt=True,
            help="Initial password.",
        ),
    ],
    display_name: str | None = typer.Option(None, "--display-name"),
    db: str | None = typer.Option(None, help="Override database URL."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create a local user account and its Personal workspace."""
    service = make_user_admin_service(db)
    try:
        user = service.add_user(
            email=email,
            password=password,
            display_name=display_name,
        )
    except (DuplicateEmailError, InvalidCredentialsError, PasswordPolicyError) as exc:
        typer.echo(f"user add failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    payload = user_payload(user)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"created user {payload['email']} ({payload['user_id']})")


@users_app.command(name="reset-password")
def users_reset_password(
    email: Annotated[str, typer.Argument(help="User email.")],
    password: Annotated[
        str,
        typer.Option(
            "--password",
            prompt=True,
            hide_input=True,
            confirmation_prompt=True,
            help="New password.",
        ),
    ],
    db: str | None = typer.Option(None, help="Override database URL."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reset a local user's password (deprecated emergency command)."""
    typer.echo(
        "Warning: users reset-password is deprecated; it lets the operator "
        "choose the password. Use issue-password-reset when available.",
        err=True,
    )
    service = make_user_admin_service(db)
    try:
        user = service.reset_password(
            email=email,
            password=password,
            actor_id=cli_operator_actor_id(),
        )
    except (InvalidCredentialsError, PasswordPolicyError) as exc:
        typer.echo(f"password reset failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    payload = user_payload(user)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"password reset for {payload['email']} ({payload['user_id']})")


@users_app.command(name="issue-password-reset")
def users_issue_password_reset(
    email: Annotated[str, typer.Argument(help="User email.")],
    delivery: Annotated[
        PasswordResetDelivery,
        typer.Option("--delivery", help="auto|email|link"),
    ] = PasswordResetDelivery.auto,
    expires_in: str = typer.Option(
        "30m",
        "--expires-in",
        help="Expiry such as 30m or 2h (minimum 5m, maximum 24h).",
    ),
    revoke_api_tokens: bool = typer.Option(
        False,
        "--revoke-api-tokens",
        help="Revoke active PAT, MCP, and OAuth credentials when reset completes.",
    ),
    db: str | None = typer.Option(None, help="Override database URL."),
) -> None:
    """Issue a one-time reset link without choosing the user's password."""

    try:
        settings, session_factory = password_recovery_context(db)
        effective_delivery = resolve_cli_password_reset_delivery(
            delivery,
            settings=settings,
            session_factory=session_factory,
            email=email,
        )
        ttl_seconds = parse_duration_seconds(expires_in)
    except (PasswordRecoveryError, ValueError) as exc:
        typer.echo(f"password reset issue failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    if effective_delivery == PasswordResetDelivery.link and not cli_stdout_is_tty():
        typer.echo(
            "password reset issue failed: link delivery requires an interactive "
            "terminal; redirected output could disclose the one-time URL",
            err=True,
        )
        raise typer.Exit(2)
    try:
        service = make_password_recovery_service(
            db,
            settings=settings,
            session_factory=session_factory,
        )
        issued = service.issue_password_reset(
            email=email,
            delivery=effective_delivery.value,
            expires_in_seconds=ttl_seconds,
            requested_via="cli",
            actor_kind="system",
            actor_id=cli_operator_actor_id(),
            revoke_api_tokens=revoke_api_tokens,
        )
    except (
        InvalidCredentialsError,
        PasswordRecoveryError,
        ValueError,
    ) as exc:
        typer.echo(f"password reset issue failed: {exc}", err=True)
        raise typer.Exit(2) from exc

    if effective_delivery == PasswordResetDelivery.email:
        if issued.outbox_id is None or service.mail_delivery_service is None:
            typer.echo(
                "password reset issue failed: email delivery was not queued",
                err=True,
            )
            raise typer.Exit(2)
        try:
            # max_attempts=1: this path is synchronous and reports the outcome
            # to the operator now, so its first failure is its last. The
            # orchestrator's settle phase then writes the terminal marker and
            # the cli_immediate audit event in its own committed transaction
            # -- which is exactly what the second transaction that used to sit
            # here was hand-rolling, and it no longer has a rolled-back
            # in-transaction marker to repair.
            #
            # That holds for a RENDER failure (expired/consumed/revoked token,
            # unset mail_from_address) as much as for a provider failure: both
            # settle outside the claim transaction now, so both clear the
            # encrypted reset envelope and emit exactly one private audit
            # event. Until MAIL-01 was fixed the render branch settled INSIDE
            # the claim transaction, whose rollback discarded the marker, the
            # audit event and the envelope erasure -- leaving recoverable
            # reset material on a row the operator had just been told failed.
            service.mail_delivery_service.deliver(
                session_factory=session_factory,
                outbox_id=issued.outbox_id,
                delivery_path="cli_immediate",
                max_attempts=1,
            )
        except MailDeliveryError as exc:
            typer.echo(f"password reset issue failed: {exc}", err=True)
            raise typer.Exit(2) from exc
        typer.echo("Password reset email sent; no link was printed.")
    else:
        typer.echo(
            "One-time password reset link (copy now; it cannot be retrieved):"
        )
        typer.echo(issued.reset_url)
    typer.echo(f"expires_at: {issued.expires_at.isoformat()}")
    typer.echo("browser_sessions: revoke on completion")
    typer.echo(
        "api_and_mcp_tokens: "
        + ("revoke on completion" if revoke_api_tokens else "keep active")
    )


@users_app.command(name="issue-pat")
def users_issue_pat(
    user: Annotated[str, typer.Option("--user", help="Email of target user.")],
    workspace: Annotated[
        str,
        typer.Option(
            "--workspace",
            help="Workspace slug to bind this PAT to.",
        ),
    ],
    name: Annotated[str, typer.Option("--name", help="Display name for the PAT.")],
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Comma-separated scopes: mcp:read,mcp:write,mcp:agent",
        ),
    ],
    expires_at: Annotated[
        str | None,
        typer.Option("--expires-at", help="ISO-8601 expiration timestamp."),
    ] = None,
    db: str | None = typer.Option(None, help="Override database URL."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Issue a personal API token directly from the local database."""
    settings = Settings(database_url=db) if db else Settings()
    bootstrap_app(settings)
    session_factory = make_session_factory_from_settings(settings)
    try:
        normalized_email = normalize_email(user)
        scopes = parse_pat_scopes(scope)
        parsed_expires_at = parse_pat_expires_at(expires_at)
        with session_factory() as session:
            model = session.scalar(select(User).where(User.email == normalized_email))
            if model is None:
                raise InvalidApiTokenRequestError("user not found")
            workspace_model = session.scalar(
                select(Workspace).where(Workspace.slug == workspace.strip())
            )
            if workspace_model is None:
                raise InvalidApiTokenRequestError("workspace not found")
            user_id = model.id
            workspace_id = workspace_model.id
        issued = ApiTokenService(
            session_factory=session_factory,
            credential_hasher=CredentialHasher(),
        ).create_token(
            user_id=user_id,
            workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
            name=name,
            scopes=scopes,
            expires_at=parsed_expires_at,
        )
    except (InvalidCredentialsError, ApiTokenServiceError, ValueError) as exc:
        typer.echo(f"issue-pat failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    payload = {
        "plain_token": issued.plain_token,
        "token_id": str(issued.api_token.id),
        "scopes": issued.api_token.scopes,
        "expires_at": (
            issued.api_token.expires_at.isoformat().replace("+00:00", "Z")
            if issued.api_token.expires_at
            else None
        ),
        "name": issued.api_token.name,
    }
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True))
        return
    typer.echo("PAT issued (copy now; this is the only time it is shown):")
    typer.echo("")
    typer.echo(f"    {issued.plain_token}")
    typer.echo("")
    typer.echo(f"token_id: {issued.api_token.id}")
    typer.echo(f"scopes: {','.join(issued.api_token.scopes)}")


def parse_pat_scopes(scope: str) -> list[str]:
    scopes = [value.strip() for value in scope.split(",") if value.strip()]
    unknown = [value for value in scopes if value not in ALLOWED_API_TOKEN_SCOPES]
    if unknown:
        raise ValueError(f"unsupported scope: {unknown[0]}")
    if not scopes:
        raise ValueError("scope is required")
    return scopes


def parse_pat_expires_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    if parsed <= datetime.now(UTC):
        raise ValueError("expires_at must be in the future")
    return parsed


def make_user_admin_service(db: str | None) -> UserAdminService:
    settings = Settings(database_url=db) if db else Settings()
    bootstrap_app(settings)
    return UserAdminService(
        session_factory=make_session_factory_from_settings(settings),
        credential_hasher=CredentialHasher(),
    )


def password_recovery_context(db: str | None):
    settings = Settings(database_url=db) if db else Settings()
    bootstrap_app(settings)
    return settings, make_session_factory_from_settings(settings)


def make_password_recovery_service(
    db: str | None,
    *,
    settings: Settings | None = None,
    session_factory=None,
) -> PasswordRecoveryService:
    if settings is None or session_factory is None:
        settings, session_factory = password_recovery_context(db)
    if not settings.public_base_url:
        raise PasswordRecoveryConfigurationError(
            "DTXWIKI_PUBLIC_BASE_URL must be configured before issuing reset links"
        )
    mail_delivery_service = EmailDeliveryService(settings=settings)
    return PasswordRecoveryService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url=settings.public_base_url,
        mail_delivery_service=mail_delivery_service,
    )


def cli_operator_actor_id() -> str:
    """Stable audit identity for the OS principal running a local CLI command."""

    try:
        principal = getpass.getuser().strip()
    except (ImportError, KeyError, OSError):
        principal = ""
    return f"cli:{principal or 'unknown'}"


def cli_stdout_is_tty() -> bool:
    return sys.stdout.isatty()


def resolve_cli_password_reset_delivery(
    requested: PasswordResetDelivery,
    *,
    settings: Settings,
    session_factory,
    email: str,
) -> PasswordResetDelivery:
    if requested == PasswordResetDelivery.email:
        if not settings.mail_configured:
            raise PasswordRecoveryConfigurationError(
                "email delivery is not configured; no reset link was issued "
                "or printed"
            )
        return PasswordResetDelivery.email
    if requested == PasswordResetDelivery.link:
        return PasswordResetDelivery.link
    if not settings.mail_configured:
        return PasswordResetDelivery.link
    normalized = normalize_email(email)
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == normalized))
        if (
            user is not None
            and user.status == "active"
            and user.email_verified_at is not None
        ):
            return PasswordResetDelivery.email
    return PasswordResetDelivery.link


def parse_duration_seconds(value: str) -> int:
    normalized = value.strip().lower()
    if len(normalized) < 2 or normalized[-1] not in {"s", "m", "h"}:
        raise ValueError("duration must use s, m, or h (for example 30m)")
    try:
        amount = int(normalized[:-1])
    except ValueError as exc:
        raise ValueError("duration must use s, m, or h (for example 30m)") from exc
    multipliers = {"s": 1, "m": 60, "h": 60 * 60}
    seconds = amount * multipliers[normalized[-1]]
    if not MIN_PASSWORD_RESET_TTL_SECONDS <= seconds <= MAX_PASSWORD_RESET_TTL_SECONDS:
        default_minutes = DEFAULT_PASSWORD_RESET_TTL_SECONDS // 60
        raise ValueError(
            "duration must be between 5m and 24h "
            f"(the default is {default_minutes}m)"
        )
    return seconds


def _admin_session_factory(db: str | None):
    settings = Settings(database_url=db) if db else Settings()
    bootstrap_app(settings)
    return make_session_factory_from_settings(settings)


@server_admin_app.command(name="grant")
def server_admin_grant(
    email: Annotated[str, typer.Option("--email", help="Email of an existing user.")],
    db: str | None = typer.Option(None, help="Override database URL."),
) -> None:
    """Grant instance-level (server) admin to an existing user."""
    factory = _admin_session_factory(db)
    with factory.begin() as session:
        user = session.scalar(
            select(User).where(User.email == normalize_email(email))
        )
        if user is None:
            typer.echo(
                f"admin grant failed: no user with email {email!r} "
                f"(create one first: dtxwiki users add {email})",
                err=True,
            )
            raise typer.Exit(2)
        server_admin_service.grant_server_admin(session, user.id, granted_by="cli")
    typer.echo(f"granted server-admin to {email}")


@server_admin_app.command(name="revoke")
def server_admin_revoke(
    email: Annotated[str, typer.Option("--email", help="Email of the admin user.")],
    db: str | None = typer.Option(None, help="Override database URL."),
) -> None:
    """Revoke instance-level (server) admin from a user."""
    factory = _admin_session_factory(db)
    with factory.begin() as session:
        user = session.scalar(
            select(User).where(User.email == normalize_email(email))
        )
        if user is None:
            typer.echo(f"admin revoke failed: no user with email {email!r}", err=True)
            raise typer.Exit(2)
        removed = server_admin_service.revoke_server_admin(session, user.id)
    if removed:
        typer.echo(f"revoked server-admin from {email}")
    else:
        typer.echo(f"{email} was not a server-admin")


@server_admin_app.command(name="list")
def server_admin_list(
    db: str | None = typer.Option(None, help="Override database URL."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List instance-level (server) admins."""
    factory = _admin_session_factory(db)
    with factory.begin() as session:
        rows = []
        for admin in server_admin_service.list_server_admins(session):
            user = session.get(User, admin.user_id)
            rows.append(
                {
                    "email": user.email if user else None,
                    "user_id": str(admin.user_id),
                    "granted_at": admin.granted_at.isoformat(),
                    "granted_by": admin.granted_by,
                }
            )
    if json_output:
        typer.echo(json.dumps(rows, indent=2, sort_keys=True))
        return
    if not rows:
        typer.echo("no server admins")
        return
    for row in rows:
        typer.echo(f"{row['email']} (granted_by={row['granted_by']})")


@workspace_members_app.command(name="list")
def workspace_members_list(
    workspace: str = typer.Option(..., "--workspace", help="Workspace slug."),
    db: str | None = typer.Option(None, help="Override database URL."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List users that belong to a workspace."""
    service = make_workspace_membership_service(db)
    try:
        members = service.list_members(workspace_slug=workspace)
    except WorkspaceMembershipNotFoundError as exc:
        typer.echo(f"workspace members list failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    payload = [membership_payload(member) for member in members]
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    for member in payload:
        typer.echo(f"{member['email']} role={member['role']}")


@workspace_members_app.command(name="add")
def workspace_members_add(
    email: Annotated[str, typer.Argument(help="User email.")],
    workspace: str = typer.Option(..., "--workspace", help="Workspace slug."),
    role: str = typer.Option("editor", "--role", help="owner|editor|viewer"),
    db: str | None = typer.Option(None, help="Override database URL."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Add or update a user's workspace membership."""
    if role not in USER_ROLES:
        raise typer.BadParameter("--role must be one of: owner, editor, viewer")
    service = make_workspace_membership_service(db)
    try:
        member = service.add_member(
            workspace_slug=workspace,
            email=email,
            role=role,
        )
    except (
        InvalidCredentialsError,
        WorkspaceMembershipNotFoundError,
    ) as exc:
        typer.echo(f"workspace members add failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    payload = membership_payload(member)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"added {payload['email']} role={payload['role']}")


@workspace_members_app.command(name="remove")
def workspace_members_remove(
    email: Annotated[str, typer.Argument(help="User email.")],
    workspace: str = typer.Option(..., "--workspace", help="Workspace slug."),
    db: str | None = typer.Option(None, help="Override database URL."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Remove a user's workspace membership."""
    service = make_workspace_membership_service(db)
    try:
        removed = service.remove_member(workspace_slug=workspace, email=email)
    except (
        InvalidCredentialsError,
        WorkspaceMembershipNotFoundError,
    ) as exc:
        typer.echo(f"workspace members remove failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    payload = {"email": email, "workspace": workspace, "removed": removed}
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"removed {email} from {workspace}: {str(removed).lower()}")


def make_workspace_membership_service(db: str | None) -> WorkspaceMembershipService:
    settings = Settings(database_url=db) if db else Settings()
    bootstrap_app(settings)
    return WorkspaceMembershipService(
        session_factory=make_session_factory_from_settings(settings),
    )


def membership_payload(member) -> dict:
    return {
        "user_id": str(member.user_id),
        "email": member.email,
        "display_name": member.display_name,
        "workspace_id": str(member.workspace_id),
        "workspace": member.workspace_slug,
        "role": member.role,
    }


def user_payload(user) -> dict:
    return {
        "user_id": str(user.user_id),
        "email": user.email,
        "display_name": user.display_name,
        "status": user.status,
    }


workspace_app.add_typer(workspace_members_app, name="members")
