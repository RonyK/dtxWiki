import logging
import os
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

import pytest
from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.base import Base
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.rbac import grant_workspace_membership
from backend.scripts.seed_dev import seed_default_workspace
from backend.services import credential_hashing
from backend.services.oauth_cimd import clear_cimd_cache
from backend.services.oauth_service import (
    _DCR_BUCKET,
    _JTI_DENYLIST_CACHE,
    _OAUTH_CSRF_CONSUMED_STATES,
    _OAUTH_CSRF_ISSUED_STATES,
    _TOKEN_CLIENT_BUCKET,
    _TOKEN_IP_BUCKET,
)
from backend.services.page_service import PageService, _clear_anchor_resolve_cache
from backend.services.session_service import _AUTH_EMAIL_BUCKET, _AUTH_IP_BUCKET
from backend.tests.utils.environment import (
    TEST_CLIENT_ALLOWED_HOSTS,
    TEST_CLIENT_ALLOWED_HOSTS_ENV,
)
from backend.tests.utils.postgres import keep_sqlite_foreign_keys_inline

# Starlette's TestClient sends ``Host: testserver``. The suite allows that one
# name exactly as an operator allows theirs, so the Host allowlist still
# refuses every other name; tests about the allowlist pass allowed_hosts to
# Settings explicitly, which takes precedence over the environment.
os.environ[TEST_CLIENT_ALLOWED_HOSTS_ENV] = TEST_CLIENT_ALLOWED_HOSTS


def _disable_test_sqlite_sync(dbapi_connection, _connection_record):
    """Skip storage synchronization for disposable pytest SQLite databases.

    The suite builds hundreds of file-backed databases, and with the default
    ``synchronous=FULL`` every autocommitted DDL statement of ``create_all``
    waits on an fsync: about 1.3 s per 100-table schema, against 0.07 s without
    it, which made schema construction the largest single cost of the suite.
    ``synchronous=OFF`` changes durability only, not visibility or locking, so
    the lock, busy-timeout and race tests keep their meaning. An application
    crash still leaves the database recoverable; only an OS crash or power
    loss could corrupt it, which no test exercises.

    Registered on the ``Engine`` class, so it reaches every SQLAlchemy engine
    this process opens, including the ones ``CliRunner`` commands and
    in-process Alembic create; raw ``sqlite3`` connections and subprocesses are
    untouched. Set ``DTXWIKI_TEST_SQLITE_DURABILITY=production`` before starting
    pytest to keep native settings for a whole invocation.
    """

    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA synchronous=OFF")
    finally:
        cursor.close()


if os.environ.get("DTXWIKI_TEST_SQLITE_DURABILITY") != "production":
    event.listen(Engine, "connect", _disable_test_sqlite_sync)

event.listen(Base.metadata, "before_create", keep_sqlite_foreign_keys_inline)

# Argon2id at the production cost (t=3, m=64MiB, p=4) takes ~45 ms to hash and
# ~42 ms to verify. Every TestClient signup/login/PAT call pays it, and the suite
# makes hundreds of them, which is minutes of wall time spent re-proving a KDF
# parameter choice that `test_credential_hashing.py` already owns. Patch the name
# `create_password_hasher` resolves at call time, so the dataclass default_factory,
# the app's lazily-built auth services, and the CLI all pick it up. Tests that are
# genuinely about the production cost opt out with `@pytest.mark.real_argon2`.
_FAST_ARGON2 = dict(
    time_cost=1,
    memory_cost=1024,
    parallelism=1,
    hash_len=16,
    salt_len=16,
    type=Type.ID,
)


@pytest.fixture(autouse=True)
def fast_argon2(request, monkeypatch):
    if request.node.get_closest_marker("real_argon2") is not None:
        return

    def _fast_password_hasher(**_production_cost) -> PasswordHasher:
        # Signature-compatible with argon2.PasswordHasher so
        # create_password_hasher's production keyword arguments are accepted
        # and then ignored.
        return PasswordHasher(**_FAST_ARGON2)

    monkeypatch.setattr(credential_hashing, "PasswordHasher", _fast_password_hasher)


@pytest.fixture(autouse=True)
def reset_auth_rate_limit():
    # /auth/login + /auth/signup rate-limit buckets are module-globals keyed
    # by client IP and email; every TestClient request shares the same
    # "testclient" IP, so leftover timestamps from a prior test would produce
    # spurious 429s.
    _AUTH_IP_BUCKET.clear()
    _AUTH_EMAIL_BUCKET.clear()
    yield
    _AUTH_IP_BUCKET.clear()
    _AUTH_EMAIL_BUCKET.clear()


@pytest.fixture(autouse=True)
def reset_oauth_dcr_rate_limit():
    _DCR_BUCKET.clear()
    # Sprint_5 Phase_3 Task 3.3: CSRF state buckets are in-process; tests
    # share a Python interpreter so leftover entries would let one test's
    # consumed state classify as ``replay`` in the next test.
    _OAUTH_CSRF_ISSUED_STATES.clear()
    _OAUTH_CSRF_CONSUMED_STATES.clear()
    # Sprint_5 Phase_3 Task 3.4: jti denylist cache is module-global so a
    # stale True for a jti minted in a prior test would 401 a fresh access
    # JWT minted in this test's in-memory DB. Clear before each test for
    # deterministic isolation.
    _JTI_DENYLIST_CACHE.clear()
    # Sprint_5 Phase_3 Task 3.6 (ADR A12): /oauth/token rate-limit buckets
    # are module-globals; leftover timestamps from a prior test could push
    # a subsequent test past the 60/120 per-minute thresholds and produce
    # spurious 429s.
    _TOKEN_IP_BUCKET.clear()
    _TOKEN_CLIENT_BUCKET.clear()
    clear_cimd_cache()
    yield
    _DCR_BUCKET.clear()
    _OAUTH_CSRF_ISSUED_STATES.clear()
    _OAUTH_CSRF_CONSUMED_STATES.clear()
    _JTI_DENYLIST_CACHE.clear()
    _TOKEN_IP_BUCKET.clear()
    _TOKEN_CLIENT_BUCKET.clear()
    clear_cimd_cache()


@pytest.fixture(autouse=True)
def reset_page_service_anchor_cache():
    _clear_anchor_resolve_cache()
    yield
    _clear_anchor_resolve_cache()


@pytest.fixture(scope="session")
def _empty_sqlite_schema():
    """One unseeded in-memory ORM schema per pytest worker.

    ``session_factory`` copies it with the SQLite backup API (~1 ms) instead of
    running ``create_all`` (~36 ms) for each of the ~2,450 tests that use it.
    It stays unseeded so every test still seeds fresh random UUIDs: a seeded
    template would hand every test the same workspace id and let id-keyed
    process caches (``graph_ppr._subgraph_cache``) leak between tests.
    """

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
        raw = engine.raw_connection()
        try:
            source = raw.driver_connection
            assert isinstance(source, sqlite3.Connection)
            assert not source.in_transaction
            yield source
        finally:
            raw.close()
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(_empty_sqlite_schema):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    engine = None
    try:
        _empty_sqlite_schema.backup(connection)
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            creator=lambda: connection,
            poolclass=StaticPool,
        )
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory.begin() as session:
            workspace = seed_default_workspace(session)
            # Production creates MCPClient lifecycle rows only when an
            # authenticated PAT/OAuth connection first touches a workspace.
            # Service-level tests that need an FK target receive an explicit
            # fixture-only row instead.
            session.add(
                MCPClient(
                    workspace_id=workspace.id,
                    name="fixture-system-client",
                    client_type="system_managed",
                    scopes=["*"],
                    status="active",
                )
            )
        yield factory
    finally:
        try:
            if engine is not None:
                engine.dispose()
        finally:
            connection.close()


@pytest.fixture
def page_service(session_factory):
    return PageService(session_factory)


@pytest.fixture
def media_installer_factory(tmp_path):
    """A real, tmp_path-isolated ``media_installer_factory`` for tests that
    construct ``ExtractionService``/``ContextWriteService`` directly.

    ``ExtractionService`` requires this explicitly (A-1: no service outside
    ``backend/app/container.py`` may import a concrete media adapter), so
    tests that used to rely on its removed environment-variable-based
    default now get an equivalent real installer here instead, isolated to
    this test's own ``tmp_path`` rather than a shared on-disk location.
    """

    from backend.media.adapters.sqlalchemy import SqlAlchemyMediaInstallJournal
    from backend.media.application import InstallValidatedBlob
    from backend.services.library_blob_storage import FilesystemLibraryBlobStore

    store = FilesystemLibraryBlobStore(tmp_path / "library-blobs")

    def _factory(session, _workspace_id):
        return InstallValidatedBlob(
            stager=store,
            publisher=store,
            deleter=store,
            layout=store,
            journal=SqlAlchemyMediaInstallJournal.for_session(session),
        )

    return _factory


@pytest.fixture
def mcp_default_actor(page_service):
    """Build a complete PAT actor for lower-level MCP contract tests.

    Production MCP requests arrive through bearer authentication. These tests
    call protocol handlers directly, so the fixture persists the same user,
    membership, PAT grant, and workspace-plane replica that authentication and
    issuance would otherwise supply. Callers may name a tool-level capability
    for readability; it is converted to the smallest public PAT umbrella that
    can actually be issued.
    """
    from sqlalchemy import select

    from backend.models.workspace import Workspace
    from backend.policy.auth import ActorContext

    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None, "default workspace must be seeded"
        workspace_id = workspace.id
    users_by_actor_id = {}

    read_capabilities = {
        "guide",
        "search",
        "read",
        "context-read",
        "workspace-read",
        "agent-page-read",
    }
    write_capabilities = {
        "audit-read",
        "review-read",
        "draft-write",
        "review-write",
        "cite",
        "feedback-write",
        "maintenance-write",
    }
    agent_capabilities = {"agent-bundle", "agent-page-draft"}

    def _issuable_scopes(requested: tuple[str, ...]) -> tuple[str, ...]:
        requested_set = set(requested)
        known = {
            "*",
            "mcp:read",
            "mcp:write",
            "mcp:agent",
            *read_capabilities,
            *write_capabilities,
            *agent_capabilities,
        }
        unknown = requested_set - known
        if unknown:
            raise ValueError(f"unsupported fixture MCP scope: {sorted(unknown)[0]}")

        issued: list[str] = []
        if "mcp:read" in requested_set or requested_set & read_capabilities:
            issued.append("mcp:read")
        if (
            "*" in requested_set
            or "mcp:write" in requested_set
            or requested_set & write_capabilities
        ):
            issued.append("mcp:write")
        if (
            "*" in requested_set
            or "mcp:agent" in requested_set
            or requested_set & agent_capabilities
        ):
            issued.append("mcp:agent")
        return tuple(issued)

    def _build(
        *,
        scopes: tuple[str, ...],
        actor_id: str = "dev-local-mcp",
        user_id=None,
        role: str | None = None,
    ):
        issued_scopes = _issuable_scopes(scopes)
        resolved_user_id = user_id or users_by_actor_id.get(actor_id)
        token_id = uuid4()
        with page_service.session_factory.begin() as session:
            user = session.get(User, resolved_user_id) if resolved_user_id else None
            if user is None:
                resolved_role = role or "owner"
                user = User(
                    id=resolved_user_id or uuid4(),
                    email=f"mcp-fixture-{uuid4().hex}@example.com",
                    password_hash="fixture-only",
                    display_name="MCP fixture user",
                    default_workspace_id=workspace_id,
                )
                session.add(user)
                session.flush()
                grant_workspace_membership(
                    session,
                    user_id=user.id,
                    workspace_id=workspace_id,
                    role=resolved_role,
                )
            else:
                membership = session.get(
                    WorkspaceMembership,
                    (user.id, workspace_id),
                )
                if membership is None:
                    resolved_role = role or "owner"
                    grant_workspace_membership(
                        session,
                        user_id=user.id,
                        workspace_id=workspace_id,
                        role=resolved_role,
                    )
                elif role is not None and membership.role != role:
                    raise ValueError(
                        "fixture actor_id cannot be reused with a different role"
                    )
                else:
                    resolved_role = membership.role
            users_by_actor_id[actor_id] = user.id
            session.add_all(
                [
                    ApiToken(
                        id=token_id,
                        user_id=user.id,
                        name=f"MCP fixture {token_id}",
                        token_lookup_hash=f"fixture-lookup-{token_id}",
                        hashed_token=f"fixture-hash-{token_id}",
                        scopes=list(issued_scopes),
                        mcp_client_type="generic",
                        mcp_client_label="MCP fixture",
                    ),
                    ApiTokenWorkspace(
                        token_id=token_id,
                        workspace_id=workspace_id,
                        desired_scopes=list(issued_scopes),
                        revision=1,
                        sync_state="synced",
                    ),
                    WorkspaceCredentialGrant(
                        workspace_id=workspace_id,
                        credential_kind="pat",
                        credential_id=token_id,
                        user_id=user.id,
                        granted_scopes=list(issued_scopes),
                        synced_revision=1,
                    ),
                ]
            )
        return ActorContext(
            actor_kind="human",
            actor_id=actor_id,
            scopes=issued_scopes,
            workspace_id=workspace_id,
            workspace_role=resolved_role,
            role=resolved_role,
            token_kind="api_token",
            token_id=token_id,
            token_workspace_ids=(workspace_id,),
            user_id=users_by_actor_id[actor_id],
            provenance="personal_access_token",
        )

    return _build


def authenticate_test_client(
    client: TestClient,
    email: str = "test@example.com",
) -> None:
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "correct-password"},
    )
    assert response.status_code in {200, 201}, response.text


def authenticate_test_client_in_workspace(
    client: TestClient,
    session_factory,
    *,
    email: str = "test@example.com",
    workspace_slug: str = "default",
    role: str = "owner",
) -> str:
    """Authenticate and explicitly opt a legacy scenario into a workspace.

    Product signup creates and selects only the account's own starter
    workspace. Scenarios that rely on fixtures seeded into another workspace
    must therefore grant their test user access to it and select it
    explicitly.
    """

    authenticate_test_client(client, email=email)
    with session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == email))
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == workspace_slug)
        )
        assert user is not None, f"signed-up test user {email!r} was not persisted"
        assert workspace is not None, (
            f"test workspace {workspace_slug!r} must be seeded before authentication"
        )
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role=role,
        )
        user.default_workspace_id = workspace.id
        workspace_id = workspace.id
    return str(workspace_id)


@contextmanager
def _captured_log_sink(logger_name: str, level: int, render):
    """The four steps that survive whatever the rest of the suite did to logging.

    `caplog` reads through the root logger, and a full-suite run leaves that
    unusable: a CLI test can call `dictConfig(disable_existing_loggers=True)`,
    which sets `.disabled` on every existing logger, and a manager-level
    `logging.disable` can still be in force. An assertion that reads the root
    logger therefore passes or fails according to suite order.

    Attach to the named logger directly, clear the manager-level disable, force
    the level, and clear `.disabled` -- restoring all three afterwards. The two
    public wrappers differ only in what `render` keeps off each record, so the
    fragile part exists once.
    """

    logger = logging.getLogger(logger_name)
    collected: list = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collected.append(render(record))

    handler = _Collect(level=level)
    logging.disable(logging.NOTSET)
    previous_level = logger.level
    previous_disabled = logger.disabled
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.disabled = False
    try:
        yield collected
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled


@contextmanager
def captured_log_records(logger_name: str, level: int = logging.WARNING):
    """Collect one logger's LogRecords regardless of global logging state.

    Same guarantee as `captured_log_messages`, but hands back the records, so a
    test can assert on the structured fields a logger attaches via `extra=`
    rather than only on the rendered text.
    """

    with _captured_log_sink(logger_name, level, lambda record: record) as records:
        yield records


@contextmanager
def captured_log_messages(logger_name: str, level: int = logging.WARNING):
    """Collect one logger's messages regardless of global logging state.

    See `_captured_log_sink` for why `caplog` cannot be used here. The same four
    steps are open-coded in `test_search_service.py` and
    `test_semantic_hybrid_retrieval.py`, which is where this came from.

    Yields a plain `list[str]` that grows as records arrive, so callers can
    compare it against a literal list.
    """

    with _captured_log_sink(
        logger_name, level, lambda record: record.getMessage()
    ) as messages:
        yield messages
