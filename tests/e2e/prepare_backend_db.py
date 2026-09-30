import os
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from backend.app.bootstrap import run_alembic_upgrade
from backend.app.db import make_engine
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.password_reset import PasswordResetToken
from backend.models.user import User
from backend.models.workspace import MCPClient
from backend.scripts.seed_dev import seed_default_workspace
from backend.services import server_admin_service
from backend.services.credential_hashing import CredentialHasher
from backend.services.password_recovery_service import password_reset_token_digest
from backend.services.session_service import SessionService, SessionTokenCodec

_DATABASE_PATH = os.environ.get("DTXWIKI_E2E_DATABASE_PATH")
_DATABASE_URL = os.environ.get("DTXWIKI_E2E_DATABASE_URL")
if not _DATABASE_PATH or not _DATABASE_URL:
    raise RuntimeError(
        "Both DTXWIKI_E2E_DATABASE_PATH and DTXWIKI_E2E_DATABASE_URL must be "
        "set by tests/e2e/run-e2e.mjs. Do not invoke this script directly; "
        "use `npm run test:e2e`."
    )

DATABASE_PATH = Path(_DATABASE_PATH)
DATABASE_URL = _DATABASE_URL
E2E_PASSWORD = "correct-password"
E2E_VIEWPORT_KEYS = ("1280x720", "1440x900", "1920x1080", "390x844")
# Which browser projects get their own fixtures. Every account below is
# created through the real signup service, so each one costs a production
# Argon2 hash -- seeding three projects when two of them only run the
# @cross-browser specs (which all sign up fresh users of their own) was pure
# preparation time. Keep this derived from the same profile the Playwright
# config reads: if a spec that needs a seeded account is ever tagged
# @cross-browser, its per-project lookup will fail loudly rather than silently
# authenticating as the wrong fixture.
_E2E_PROFILE = os.environ.get("DTXWIKI_E2E_PROFILE", "standard")
E2E_PROJECT_KEYS = (
    ("chromium", "firefox", "webkit")
    if _E2E_PROFILE == "exhaustive"
    else ("chromium",)
)


def main() -> None:
    DATABASE_PATH.unlink(missing_ok=True)
    run_alembic_upgrade(DATABASE_URL)
    engine = make_engine(DATABASE_URL)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory.begin() as session:
        seed_default_workspace(session)
    _seed_identity_recovery_accounts(session_factory)
    _seed_mcp_access_account(session_factory)


def _seed_identity_recovery_accounts(session_factory) -> None:
    """Create isolated identity fixtures for every mutating browser journey.

    Recovery changes the target password, so browser projects cannot share a
    viewport-only account. Context media also needs a verified owner whose
    password remains stable while the recovery spec runs in parallel. The
    normal signup domain service is used so every seeded account receives the
    same Personal Workspace anchor as a real browser signup.
    """

    service = SessionService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        token_codec=SessionTokenCodec(secret="dtxwiki-e2e-seed-secret"),
        # AUTH-09 made the dataclass default fail-closed, so an assembly point
        # that omits this no longer silently inherits open signup. This seeder
        # deliberately creates accounts, so it opts in explicitly rather than
        # relying on a default -- which is the whole point of that change.
        signup_enabled=True,
    )
    for viewport_key in E2E_VIEWPORT_KEYS:
        token_state_user = service.signup(
            email=f"e2e-reset-state-{viewport_key}@example.com",
            password=E2E_PASSWORD,
            display_name=f"E2E Reset State {viewport_key}",
        ).user
        with session_factory.begin() as session:
            _seed_password_reset_token_states(
                session,
                user_id=token_state_user.user_id,
                viewport_key=viewport_key,
            )

    for project_key in E2E_PROJECT_KEYS:
        for viewport_key in E2E_VIEWPORT_KEYS:
            admin = service.signup(
                email=f"e2e-admin-{project_key}-{viewport_key}@example.com",
                password=E2E_PASSWORD,
                display_name=f"E2E Admin {project_key} {viewport_key}",
            ).user
            target = service.signup(
                email=f"e2e-recovery-{project_key}-{viewport_key}@example.com",
                password=E2E_PASSWORD,
                display_name=f"E2E Recovery {project_key} {viewport_key}",
            ).user
            with session_factory.begin() as session:
                server_admin_service.grant_server_admin(
                    session,
                    admin.user_id,
                    granted_by="e2e-seed",
                )
                target_model = session.get(User, target.user_id)
                assert target_model is not None
                target_model.email_verified_at = datetime.now(UTC)

        media_owner = service.signup(
            email=f"e2e-context-media-{project_key}@example.com",
            password=E2E_PASSWORD,
            display_name=f"E2E Context Media {project_key}",
        ).user
        with session_factory.begin() as session:
            media_owner_model = session.get(User, media_owner.user_id)
            assert media_owner_model is not None
            media_owner_model.email_verified_at = datetime.now(UTC)


def _seed_mcp_access_account(session_factory) -> None:
    """Seed page-size-plus lifecycle facets for MCP Access browser QA."""

    service = SessionService(
        session_factory=session_factory,
        credential_hasher=CredentialHasher(),
        token_codec=SessionTokenCodec(secret="dtxwiki-e2e-mcp-access-seed"),
        signup_enabled=True,
    )
    for project_key in E2E_PROJECT_KEYS:
        signed_up = service.signup(
            email=f"e2e-mcp-access-{project_key}@example.com",
            password=E2E_PASSWORD,
            display_name=f"E2E MCP Access {project_key}",
        ).user
        now = datetime.now(UTC)
        with session_factory.begin() as session:
            user = session.get(User, signed_up.user_id)
            assert user is not None
            workspace_id = user.personal_workspace_id
            assert workspace_id is not None

            # P10.3 T1 (D-01v2): ApiToken.workspace_id is gone -- a PAT's
            # allowed workspaces are ApiTokenWorkspace rows now. These seeded
            # keys exist only to populate the Settings list (their
            # hashed_token is deliberately not authenticatable), so they need
            # the control-plane index row but no workspace-plane replica.
            seeded_tokens: list[ApiToken] = []
            for index in range(25):
                created_at = now - timedelta(minutes=index)
                seeded_tokens.append(
                    ApiToken(
                        user_id=user.id,
                        name=f"Active UI key {index + 1:02d}",
                        token_lookup_hash=(
                            f"e2e-mcp-access-{project_key}-active-{index}"
                        ),
                        hashed_token="e2e-not-an-authenticatable-secret",
                        token_preview=f"dtxw_ui{index:02d}",
                        scopes=["mcp:read"],
                        mcp_client_type="generic",
                        mcp_client_label="Other MCP client",
                        created_at=created_at,
                        expires_at=now + timedelta(days=30),
                    )
                )

            for index in range(25):
                is_revoked = index % 2 == 0
                created_at = now - timedelta(days=1, minutes=index)
                seeded_tokens.append(
                    ApiToken(
                        user_id=user.id,
                        name=f"Inactive UI key {index + 1:02d}",
                        token_lookup_hash=(
                            f"e2e-mcp-access-{project_key}-inactive-{index}"
                        ),
                        hashed_token="e2e-not-an-authenticatable-secret",
                        token_preview=f"dtxw_ix{index:02d}",
                        scopes=["mcp:read"],
                        mcp_client_type="generic",
                        mcp_client_label="Other MCP client",
                        created_at=created_at,
                        expires_at=now - timedelta(minutes=1),
                        revoked_at=(now - timedelta(hours=1) if is_revoked else None),
                    )
                )

            session.add_all(seeded_tokens)
            session.flush()
            for token in seeded_tokens:
                session.add(
                    ApiTokenWorkspace(
                        token_id=token.id,
                        workspace_id=workspace_id,
                        desired_scopes=list(token.scopes),
                        revision=1,
                        sync_state="synced",
                    )
                )

            for status in ("active", "disabled"):
                for index in range(25):
                    session.add(
                        MCPClient(
                            workspace_id=workspace_id,
                            name=f"{status.title()} UI client {index + 1:02d}",
                            client_type="generic",
                            scopes=["mcp:read"],
                            status=status,
                            runtime_binding_key=(
                                f"e2e-ui:{project_key}:{status}:{index}"
                            ),
                            created_by=user.id,
                            issued_to_user_id=user.id,
                            brand_hint="generic",
                            created_at=now
                            - timedelta(
                                days=2 if status == "disabled" else 0,
                                minutes=index,
                            ),
                        )
                    )


def _seed_password_reset_token_states(session, *, user_id, viewport_key: str) -> None:
    """Seed stable expired/used links without exposing a production endpoint.

    The raw fixture values are reproducible by Playwright, but only their
    purpose-bound digests enter the database. A dedicated user keeps these
    rows stable when the recovery journey issues fresh tokens for its target.
    """

    now = datetime.now(UTC)
    expired_token = _password_reset_state_token(viewport_key, "expired")
    used_token = _password_reset_state_token(viewport_key, "used")
    session.add_all(
        [
            PasswordResetToken(
                user_id=user_id,
                token_digest=password_reset_token_digest(expired_token),
                purpose="password_reset",
                delivery="link",
                requested_by_user_id=None,
                requested_via="cli",
                revoke_api_tokens=False,
                created_at=now - timedelta(hours=1),
                expires_at=now - timedelta(minutes=1),
            ),
            PasswordResetToken(
                user_id=user_id,
                token_digest=password_reset_token_digest(used_token),
                purpose="password_reset",
                delivery="link",
                requested_by_user_id=None,
                requested_via="cli",
                revoke_api_tokens=False,
                created_at=now - timedelta(minutes=20),
                expires_at=now + timedelta(minutes=10),
                used_at=now - timedelta(minutes=10),
            ),
        ]
    )


def _password_reset_state_token(viewport_key: str, state: str) -> str:
    seed = f"dtxwiki-e2e-password-reset:{state}:{viewport_key}".encode()
    payload = urlsafe_b64encode(sha256(seed).digest()).decode().rstrip("=")
    return f"dtxr_0_{payload}"


if __name__ == "__main__":
    main()
