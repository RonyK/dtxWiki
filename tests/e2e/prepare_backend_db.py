import os
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from sqlalchemy.orm import sessionmaker

from backend.app.bootstrap import run_alembic_upgrade
from backend.app.db import make_engine
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.password_reset import PasswordResetToken
from backend.models.tree import WorkspaceTreeNode
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.scripts.seed_dev import seed_default_workspace
from backend.services import server_admin_service
from backend.services.credential_hashing import CredentialHasher
from backend.services.password_recovery_service import password_reset_token_digest
from backend.services.session_service import SessionService, SessionTokenCodec
from backend.tests.utils.workspace_graph_corpora import (
    WorkspaceGraphCorpusBuilder,
    build_knowledge_40,
    build_synthetic_workspace,
    fixture_time,
    fixture_uuid,
)
from backend.tests.utils.workspace_graph_synthetic import SYNTHETIC_GENERATOR_VERSION

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
E2E_GRAPH_OWNER_EMAIL = "e2e-graph-owner@example.com"
E2E_GRAPH_2K_OWNER_EMAIL = "e2e-graph-2k-owner@example.com"
E2E_GRAPH_10K_OWNER_EMAIL = "e2e-graph-10k-owner@example.com"
E2E_GRAPH_REFINE_OWNER_EMAIL = "e2e-graph-refine-owner@example.com"
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
    ("chromium", "firefox", "webkit") if _E2E_PROFILE == "exhaustive" else ("chromium",)
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
    _seed_workspace_graph_owner_corpus(session_factory)
    _seed_workspace_graph_refine_corpus(session_factory)
    if os.environ.get("DTXWIKI_E2E_GRAPH_2K") == "1":
        graph_2k_started = perf_counter()
        _seed_workspace_graph_2k_owner_corpus(session_factory)
        print(
            f"[e2e graph-2k] seeded isolated corpus in "
            f"{perf_counter() - graph_2k_started:.1f}s",
            flush=True,
        )
    if os.environ.get("DTXWIKI_E2E_GRAPH_10K") == "1":
        graph_10k_started = perf_counter()
        _seed_workspace_graph_10k_owner_corpus(session_factory)
        print(
            f"[e2e graph-10k] seeded isolated corpus in "
            f"{perf_counter() - graph_10k_started:.1f}s",
            flush=True,
        )


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


def _seed_workspace_graph_owner_corpus(session_factory) -> None:
    """Seed the deterministic knowledge-40 corpus behind one owner session.

    The corpus builder is the same persisted-model fixture used by the backend
    acceptance suite. Its stable owner UUID is also used for the browser user,
    so the REST actor, membership, and workspace ownership agree without a
    test-only API or a production data path.
    """

    owner_id = fixture_uuid("knowledge-40", "owner")
    password_hash = CredentialHasher().hash_password(E2E_PASSWORD)
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session)
        owner = User(
            id=owner_id,
            email=E2E_GRAPH_OWNER_EMAIL,
            email_verified_at=fixture_time(),
            password_hash=password_hash,
            display_name="E2E Graph Owner",
            status="active",
        )
        session.add(owner)
        session.flush()

        workspace = session.get(Workspace, corpus.workspace_id)
        assert workspace is not None
        workspace.owner_actor_id = owner_id
        workspace.owner_user_id = owner_id
        # The owner has switched the Knowledge Map on in Settings; that
        # workspace column is the product switch the /graph route checks.
        workspace.workspace_graph_enabled = True
        owner.default_workspace_id = corpus.workspace_id
        session.add(
            WorkspaceMembership(
                user_id=owner_id,
                workspace_id=corpus.workspace_id,
                role="owner",
                joined_at=fixture_time(),
            )
        )


def _seed_workspace_graph_refine_corpus(session_factory) -> None:
    """Three independent published documents for real search/folder journeys.

    Root-level placement and no placement both belong to the emitted unplaced
    facet. Keep this workspace separate from the stable knowledge-40/2K cases.
    """
    corpus_key = "e2e-graph-refine"
    owner_id = fixture_uuid(corpus_key, "owner")
    password_hash = CredentialHasher().hash_password(E2E_PASSWORD)
    with session_factory.begin() as session:
        builder = WorkspaceGraphCorpusBuilder(
            session, corpus_key=corpus_key, title="Graph Refine Cases"
        )
        folder_id = builder.add_folder(0, section="context", title="Research Delta")
        alpha = builder.add_context(0, slug="alpha", title="Alpha")
        beta = builder.add_context(1, slug="beta", title="Beta")
        gamma = builder.add_context(2, slug="gamma", title="Gamma")
        builder.place(
            0,
            entity_kind="context",
            entity_id=alpha,
            title="Alpha",
            folder_id=folder_id,
        )
        session.add(
            WorkspaceTreeNode(
                id=fixture_uuid(corpus_key, "root-placement"),
                workspace_id=builder.workspace.id,
                section="context",
                node_kind="page_ref",
                parent_id=None,
                title="Beta",
                slug="beta",
                sort_key="000001",
                target_kind="context_page",
                target_id=beta,
                primary_placement=True,
                visibility_status="visible",
                can_have_children=False,
                created_at=fixture_time(),
                updated_at=fixture_time(),
            )
        )
        for index, (source, target) in enumerate(((alpha, beta), (gamma, alpha))):
            builder.add_relation(
                index,
                source_context_id=source,
                target_kind="context_page",
                target_id=target,
                edge_kind="wikilink",
            )
        corpus = builder.finish("graph-refine-three")
        owner = User(
            id=owner_id,
            email=E2E_GRAPH_REFINE_OWNER_EMAIL,
            email_verified_at=fixture_time(),
            password_hash=password_hash,
            display_name="E2E Graph Refine Owner",
            status="active",
            default_workspace_id=corpus.workspace_id,
        )
        session.add(owner)
        session.flush()
        builder.workspace.owner_actor_id = owner_id
        builder.workspace.owner_user_id = owner_id
        session.add(
            WorkspaceMembership(
                user_id=owner_id,
                workspace_id=corpus.workspace_id,
                role="owner",
                joined_at=fixture_time(),
            )
        )


def _seed_workspace_graph_2k_owner_corpus(session_factory) -> None:
    """Add a separate real-API scale workspace only for the opt-in browser spec.

    The normal knowledge-40 account and default E2E seed are unchanged. The
    wrapper supplies a fresh temporary SQLite URL, so this never reaches a
    developer, shared, or production database.
    """

    corpus_key = "e2e-knowledge-2k"
    owner_id = fixture_uuid(corpus_key, "owner")
    password_hash = CredentialHasher().hash_password(E2E_PASSWORD)
    with session_factory.begin() as session:
        corpus = build_synthetic_workspace(
            session,
            documents=2_000,
            profile="synthetic",
            seed=260917,
            workspace_index=0,
            restricted_library_pct=0,
            corpus_key=corpus_key,
        )
        assert len(corpus.document_ids) == 2_000
        owner = User(
            id=owner_id,
            email=E2E_GRAPH_2K_OWNER_EMAIL,
            email_verified_at=fixture_time(),
            password_hash=password_hash,
            display_name="E2E Graph 2K Owner",
            status="active",
        )
        session.add(owner)
        session.flush()

        workspace = session.get(Workspace, corpus.workspace_id)
        assert workspace is not None
        workspace.owner_actor_id = owner_id
        workspace.owner_user_id = owner_id
        workspace.workspace_graph_enabled = True
        owner.default_workspace_id = corpus.workspace_id
        session.add(
            WorkspaceMembership(
                user_id=owner_id,
                workspace_id=corpus.workspace_id,
                role="owner",
                joined_at=fixture_time(),
            )
        )


def _seed_workspace_graph_10k_owner_corpus(session_factory) -> None:
    """Opt-in v3 scale corpus in the wrapper's fresh temporary SQLite only.

    Its account, workspace and deterministic IDs are independent of the 2K
    and default fixtures. The seed pins the authored Combined relation oracle;
    projection storage is not counted as additional authored relationships.
    """

    assert SYNTHETIC_GENERATOR_VERSION == "workspace-graph-synthetic.v3"
    corpus_key = "e2e-knowledge-10k"
    owner_id = fixture_uuid(corpus_key, "owner")
    password_hash = CredentialHasher().hash_password(E2E_PASSWORD)
    with session_factory.begin() as session:
        corpus = build_synthetic_workspace(
            session,
            documents=10_000,
            profile="synthetic",
            seed=260917,
            workspace_index=0,
            restricted_library_pct=0,
            corpus_key=corpus_key,
        )
        assert len(corpus.document_ids) == 10_000
        assert len(corpus.context_ids) == 9_000
        assert len(corpus.library_ids) == 1_000
        assert len(corpus.relation_ids) == 30_895
        assert not corpus.restricted_library_ids
        owner = User(
            id=owner_id,
            email=E2E_GRAPH_10K_OWNER_EMAIL,
            email_verified_at=fixture_time(),
            password_hash=password_hash,
            display_name="E2E Graph 10K Owner",
            status="active",
        )
        session.add(owner)
        session.flush()

        workspace = session.get(Workspace, corpus.workspace_id)
        assert workspace is not None
        workspace.owner_actor_id = owner_id
        workspace.owner_user_id = owner_id
        workspace.workspace_graph_enabled = True
        owner.default_workspace_id = corpus.workspace_id
        session.add(
            WorkspaceMembership(
                user_id=owner_id,
                workspace_id=corpus.workspace_id,
                role="owner",
                joined_at=fixture_time(),
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
