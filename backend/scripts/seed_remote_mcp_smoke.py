"""Seed Sprint 5 Phase 5 remote MCP smoke material.

This script prepares the database rows and one-time PAT values consumed by
``tests/e2e/remote_mcp_smoke.py`` when it runs in remote Docker mode.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shlex
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from sqlalchemy import select

from backend.app.bootstrap import ensure_app_version_synced
from backend.app.db import make_session_factory_from_settings
from backend.app.settings import Settings
from backend.models.api_token import ApiToken
from backend.models.base import utc_now
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.policy.rbac import grant_workspace_membership, remove_workspace_membership
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.api_token_service import ApiTokenService, WorkspaceGrantRequest
from backend.services.credential_hashing import CredentialHasher
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.page_service import PageNotFoundError, PageService
from backend.services.personal_workspace_service import ensure_personal_workspace
from backend.services.session_service import (
    normalize_display_name,
    normalize_email,
    validate_password,
)

DEFAULT_ALICE_EMAIL = "alice@dtxwiki.test"
DEFAULT_BOB_EMAIL = "bob@dtxwiki.test"
DEFAULT_WORKSPACE_A = "wsA"
DEFAULT_WORKSPACE_B = "wsB"
DEFAULT_PAGE_SLUG = "phase5-remote-page"
READ_PAT_NAME = "Phase 5 Remote Smoke Read"
AGENT_PAT_NAME = "Phase 5 Remote Smoke Agent"


@dataclass(frozen=True)
class RemoteSmokeSeedResult:
    alice_email: str
    bob_email: str
    workspace_a: str
    workspace_b: str
    pat_read: str
    pat_agent: str
    workspace_id: str
    page_id: str
    page_slug: str
    foreign_workspace_id: str
    foreign_page_id: str
    restricted_workspace_id: str
    restricted_artifact_id: str
    login_email: str
    login_password: str

    def as_env(self) -> dict[str, str]:
        return {
            "DTXWIKI_REMOTE_E2E_ALICE": self.alice_email,
            "DTXWIKI_REMOTE_E2E_BOB": self.bob_email,
            "DTXWIKI_REMOTE_E2E_WORKSPACE_A": self.workspace_a,
            "DTXWIKI_REMOTE_E2E_WORKSPACE_B": self.workspace_b,
            "DTXWIKI_REMOTE_E2E_PAT_READ": self.pat_read,
            "DTXWIKI_REMOTE_E2E_PAT_AGENT": self.pat_agent,
            "DTXWIKI_REMOTE_E2E_WORKSPACE_ID": self.workspace_id,
            "DTXWIKI_REMOTE_E2E_PAGE_ID": self.page_id,
            "DTXWIKI_REMOTE_E2E_PAGE_SLUG": self.page_slug,
            "DTXWIKI_REMOTE_E2E_FOREIGN_WORKSPACE_ID": self.foreign_workspace_id,
            "DTXWIKI_REMOTE_E2E_FOREIGN_PAGE_ID": self.foreign_page_id,
            "DTXWIKI_REMOTE_E2E_RESTRICTED_WORKSPACE_ID": (
                self.restricted_workspace_id
            ),
            "DTXWIKI_REMOTE_E2E_RESTRICTED_ARTIFACT_ID": (
                self.restricted_artifact_id
            ),
            "DTXWIKI_REMOTE_E2E_LOGIN_EMAIL": self.login_email,
            "DTXWIKI_REMOTE_E2E_LOGIN_PASSWORD": self.login_password,
        }


def seed_remote_mcp_smoke(
    settings: Settings,
    *,
    alice_email: str = DEFAULT_ALICE_EMAIL,
    bob_email: str = DEFAULT_BOB_EMAIL,
    workspace_a: str = DEFAULT_WORKSPACE_A,
    workspace_b: str = DEFAULT_WORKSPACE_B,
    login_password: str | None = None,
) -> RemoteSmokeSeedResult:
    """Prepare deterministic rows for the remote 8-scenario smoke suite."""

    resolved_password = login_password or secrets.token_urlsafe(24)
    validate_password(resolved_password)
    ensure_app_version_synced(settings)

    session_factory = make_session_factory_from_settings(settings)
    hasher = CredentialHasher()
    alice_email = normalize_email(alice_email)
    bob_email = normalize_email(bob_email)

    with session_factory.begin() as session:
        ws_a = seed_default_workspace(
            session,
            slug=workspace_a,
            title="Phase 5 Remote Smoke A",
        )
        ws_b = seed_default_workspace(
            session,
            slug=workspace_b,
            title="Phase 5 Remote Smoke B",
        )
        alice = _upsert_user(
            session=session,
            email=alice_email,
            password=resolved_password,
            hasher=hasher,
        )
        bob = _upsert_user(
            session=session,
            email=bob_email,
            password=resolved_password,
            hasher=hasher,
        )
        grant_workspace_membership(
            session,
            user_id=alice.id,
            workspace_id=ws_a.id,
            role="owner",
        )
        grant_workspace_membership(
            session,
            user_id=bob.id,
            workspace_id=ws_b.id,
            role="owner",
        )
        remove_workspace_membership(session, user_id=alice.id, workspace_id=ws_b.id)
        remove_workspace_membership(session, user_id=bob.id, workspace_id=ws_a.id)
        alice_id = alice.id
        workspace_id = ws_a.id
        foreign_workspace_id = ws_b.id

    page_service = PageService(session_factory)
    page = _upsert_page(
        page_service=page_service,
        workspace_slug=workspace_a,
        slug=DEFAULT_PAGE_SLUG,
        title="Phase 5 Remote Page",
        body_md="# Phase 5 Remote Page\n\nRemote smoke content snippet.",
    )
    foreign_page = _upsert_page(
        page_service=page_service,
        workspace_slug=workspace_b,
        slug="phase5-foreign-page",
        title="Phase 5 Foreign Page",
        body_md="# Phase 5 Foreign Page\n\nInvisible foreign page.",
    )
    with session_factory.begin() as session:
        ws_a = session.scalar(select(Workspace).where(Workspace.slug == workspace_a))
        if ws_a is None:
            raise RuntimeError(f"workspace not found after seed: {workspace_a}")
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=ws_a.id,
            canonical_uri="https://example.com/phase5-restricted",
            title="Phase 5 Restricted Source",
            trust_level="restricted",
            raw_text="# Restricted Library item\n\nrestricted Library item needle",
            actor_type="system",
        )
        from backend.app.container import ServiceRegistry

        services = ServiceRegistry()
        services.settings = settings
        artifact = ExtractionService(
            media_installer_factory=services.validated_media_installer_for_session
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
            actor_type="system",
        )
        restricted_workspace_id = ws_a.id
        restricted_artifact_id = artifact.id
        _revoke_named_tokens(
            session=session,
            user_id=alice_id,
            names={READ_PAT_NAME, AGENT_PAT_NAME},
        )

    token_service = ApiTokenService(
        session_factory=session_factory,
        credential_hasher=hasher,
    )
    pat_read = token_service.create_token(
        user_id=alice_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name=READ_PAT_NAME,
        scopes=["mcp:read"],
    ).plain_token
    pat_agent = token_service.create_token(
        user_id=alice_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_id)],
        name=AGENT_PAT_NAME,
        scopes=["mcp:read", "mcp:write", "mcp:agent"],
    ).plain_token

    return RemoteSmokeSeedResult(
        alice_email=alice_email,
        bob_email=bob_email,
        workspace_a=workspace_a,
        workspace_b=workspace_b,
        pat_read=pat_read,
        pat_agent=pat_agent,
        workspace_id=str(workspace_id),
        page_id=str(page.id),
        page_slug=page.slug,
        foreign_workspace_id=str(foreign_workspace_id),
        foreign_page_id=str(foreign_page.id),
        restricted_workspace_id=str(restricted_workspace_id),
        restricted_artifact_id=str(restricted_artifact_id),
        login_email=alice_email,
        login_password=resolved_password,
    )


def format_env(result: RemoteSmokeSeedResult) -> str:
    lines = [
        "# Source this file only on the remote-smoke runner; do not commit it.",
    ]
    lines.extend(
        f"export {key}={shlex.quote(value)}" for key, value in result.as_env().items()
    )
    return "\n".join(lines) + "\n"


def _upsert_page(
    *,
    page_service: PageService,
    workspace_slug: str,
    slug: str,
    title: str,
    body_md: str,
):
    try:
        current = page_service.read_page(workspace_slug=workspace_slug, slug=slug)
    except PageNotFoundError:
        return page_service.write_page(
            workspace_slug=workspace_slug,
            slug=slug,
            title=title,
            body_md=body_md,
            actor_kind="system",
            actor_id="phase5-remote-seed",
            actor_provenance="internal",
        )
    return page_service.write_page(
        workspace_slug=workspace_slug,
        slug=slug,
        title=title,
        body_md=body_md,
        actor_kind="system",
        actor_id="phase5-remote-seed",
        actor_provenance="internal",
        base_version_id=current.version_id,
        base_content_hash=current.content_hash,
    )


def _upsert_user(
    *,
    session,
    email: str,
    password: str,
    hasher: CredentialHasher,
) -> User:
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            password_hash=hasher.hash_password(password),
            display_name=normalize_display_name(None, email),
            status="active",
        )
        session.add(user)
    else:
        user.password_hash = hasher.hash_password(password)
        user.display_name = normalize_display_name(user.display_name, email)
        user.status = "active"
    session.flush()
    # The remote smoke seeder is a real user-provisioning path, not a fixture
    # exemption. Re-running it also repairs rows created by older seeders that
    # predate the Personal anchor invariant.
    ensure_personal_workspace(session, user=user)
    return user


def _revoke_named_tokens(*, session, user_id, names: set[str]) -> None:
    for token in session.scalars(
        select(ApiToken).where(ApiToken.user_id == user_id, ApiToken.name.in_(names))
    ):
        if token.revoked_at is None:
            token.revoked_at = utc_now()
            session.add(token)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed Sprint 5 Phase 5 remote MCP smoke data.",
    )
    parser.add_argument("--db", help="Override DTXWIKI_DATABASE_URL.")
    parser.add_argument("--alice-email", default=DEFAULT_ALICE_EMAIL)
    parser.add_argument("--bob-email", default=DEFAULT_BOB_EMAIL)
    parser.add_argument("--workspace-a", default=DEFAULT_WORKSPACE_A)
    parser.add_argument("--workspace-b", default=DEFAULT_WORKSPACE_B)
    parser.add_argument(
        "--login-password",
        help="Password for the seeded Alice login. Defaults to a generated secret.",
    )
    parser.add_argument("--format", choices=("env", "json"), default="env")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    settings = Settings(database_url=args.db) if args.db else Settings()
    result = seed_remote_mcp_smoke(
        settings,
        alice_email=args.alice_email,
        bob_email=args.bob_email,
        workspace_a=args.workspace_a,
        workspace_b=args.workspace_b,
        login_password=args.login_password,
    )
    if args.format == "json":
        indent = 2 if args.pretty else None
        print(json.dumps(asdict(result), indent=indent, sort_keys=True))
        return
    print(format_env(result), end="")


if __name__ == "__main__":
    main()
