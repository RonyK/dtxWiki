from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from backend.app.db import make_session_factory_from_settings
from backend.app.settings import Settings
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.user import User
from backend.models.workspace import WorkspaceMembership
from backend.scripts.seed_remote_mcp_smoke import (
    format_env,
    seed_remote_mcp_smoke,
)
from backend.services.api_token_service import ApiTokenService
from backend.services.credential_hashing import CredentialHasher
from backend.services.session_service import SessionService, SessionTokenCodec


def test_seed_remote_mcp_smoke_outputs_usable_material(tmp_path):
    settings = Settings(
        app_env="development",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'remote-smoke.sqlite'}",
        session_secret="seed-test-session-secret",
    )

    result = seed_remote_mcp_smoke(
        settings,
        login_password="correct-password",
    )

    env = result.as_env()
    required = {
        "DTXWIKI_REMOTE_E2E_PAT_READ",
        "DTXWIKI_REMOTE_E2E_PAT_AGENT",
        "DTXWIKI_REMOTE_E2E_WORKSPACE_ID",
        "DTXWIKI_REMOTE_E2E_PAGE_ID",
        "DTXWIKI_REMOTE_E2E_FOREIGN_WORKSPACE_ID",
        "DTXWIKI_REMOTE_E2E_FOREIGN_PAGE_ID",
        "DTXWIKI_REMOTE_E2E_RESTRICTED_WORKSPACE_ID",
        "DTXWIKI_REMOTE_E2E_RESTRICTED_ARTIFACT_ID",
        "DTXWIKI_REMOTE_E2E_LOGIN_EMAIL",
        "DTXWIKI_REMOTE_E2E_LOGIN_PASSWORD",
    }
    assert required.issubset(env)
    assert all(env[key] for key in required)

    session_factory = make_session_factory_from_settings(settings)
    hasher = CredentialHasher()
    session_service = SessionService(
        session_factory=session_factory,
        credential_hasher=hasher,
        token_codec=SessionTokenCodec(secret=settings.effective_session_secret),
    )
    logged_in = session_service.login(
        email=result.login_email,
        password=result.login_password,
    )
    assert logged_in.user.email == result.alice_email

    token_service = ApiTokenService(
        session_factory=session_factory,
        credential_hasher=hasher,
    )
    read_actor = token_service.authenticate_token(result.pat_read)
    agent_actor = token_service.authenticate_token(result.pat_agent)
    assert read_actor.scopes == ("mcp:read",)
    assert set(agent_actor.scopes) == {"mcp:read", "mcp:write", "mcp:agent"}

    with session_factory() as session:
        alice = session.scalar(select(User).where(User.email == result.alice_email))
        assert alice is not None
        assert alice.personal_workspace_id is not None
        assert session.get(
            WorkspaceMembership,
            (alice.id, alice.personal_workspace_id),
        ).role == "owner"
        assert (
            session.get(
                WorkspaceMembership,
                (alice.id, UUID(result.foreign_workspace_id)),
            )
            is None
        )
        artifact = session.get(LibraryArtifact, UUID(result.restricted_artifact_id))
        assert artifact is not None
        version = session.get(LibraryItemVersion, artifact.library_item_version_id)
        assert version is not None
        source = session.get(LibraryItem, version.library_item_id)
        assert source is not None
        assert source.workspace_id == UUID(result.restricted_workspace_id)
        assert source.trust_level == "restricted"


def test_seed_remote_mcp_smoke_is_rerunnable_on_same_database(tmp_path):
    settings = Settings(
        app_env="development",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'remote-smoke.sqlite'}",
        session_secret="seed-test-session-secret",
    )

    first = seed_remote_mcp_smoke(settings, login_password="correct-password")
    second = seed_remote_mcp_smoke(settings, login_password="correct-password")

    assert second.page_id == first.page_id
    assert second.foreign_page_id == first.foreign_page_id
    assert second.restricted_artifact_id == first.restricted_artifact_id
    assert second.pat_read != first.pat_read
    token_service = ApiTokenService(
        session_factory=make_session_factory_from_settings(settings),
        credential_hasher=CredentialHasher(),
    )
    assert token_service.authenticate_token(second.pat_read).scopes == ("mcp:read",)


def test_seed_remote_mcp_smoke_env_format_is_sourceable(tmp_path):
    settings = Settings(
        app_env="development",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'remote-smoke.sqlite'}",
    )
    result = seed_remote_mcp_smoke(
        settings,
        login_password="correct-password",
    )

    output = format_env(result)

    assert output.startswith("# Source this file only")
    assert "export DTXWIKI_REMOTE_E2E_PAT_READ=" in output
    assert "export DTXWIKI_REMOTE_E2E_LOGIN_PASSWORD=correct-password" in output
    assert "<redacted>" not in output
