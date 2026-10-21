"""Database-level concurrency contracts for MCP Access lifecycle services."""

import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql

from backend.app.db import make_engine, make_session_factory
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.base import Base, utc_now
from backend.models.oauth import OAuthClient, OAuthConsent, OAuthRefreshToken
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.policy.auth import ActorContext
from backend.services import settings_service
from backend.services.api_token_service import ApiTokenService, WorkspaceGrantRequest
from backend.services.credential_hashing import CredentialHasher
from backend.services.mcp_client_service import evaluate_mcp_client_runtime
from backend.services.mcp_credential_health import CredentialHealth
from backend.services.oauth_service import WorkspaceOAuthHealth
from backend.services.read_errors import ReadNotFoundError
from backend.services.settings_service import SettingsService
from backend.tests.conftest import captured_log_records


class _ReachedGuardedRead(Exception):
    def __init__(self, outside_writer_blocked: bool) -> None:
        super().__init__("reached guarded read")
        self.outside_writer_blocked = outside_writer_blocked


@pytest.fixture
def db(tmp_path):
    database_path = tmp_path / "mcp-access-consistency.sqlite"
    engine = make_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        workspace = Workspace(
            slug="mcp-access-consistency",
            title="MCP Access consistency",
            name="MCP Access consistency",
            description=None,
        )
        user = User(
            email="mcp-access-consistency@example.com",
            password_hash="not-used",
            display_name="MCP Access consistency",
        )
        session.add_all([workspace, user])
        session.flush()
        session.add(
            WorkspaceMembership(
                user_id=user.id,
                workspace_id=workspace.id,
                role="owner",
            )
        )
        session.add(
            MCPClient(
                workspace_id=workspace.id,
                name="Seed client",
                client_type="generic",
                scopes=["mcp:read"],
                status="active",
                token_metadata={},
            )
        )
        seed_token = ApiToken(
            user_id=user.id,
            name="Seed key",
            token_lookup_hash="sha256:seed",
            hashed_token="not-used",
            scopes=["mcp:read"],
        )
        session.add(seed_token)
        session.flush()
        session.add(
            ApiTokenWorkspace(
                token_id=seed_token.id,
                workspace_id=workspace.id,
                desired_scopes=["mcp:read"],
                revision=1,
                sync_state="synced",
            )
        )
        workspace_id = workspace.id
        user_id = user.id
    try:
        yield SimpleNamespace(
            path=database_path,
            engine=engine,
            factory=factory,
            workspace_id=workspace_id,
            user_id=user_id,
        )
    finally:
        engine.dispose()


def _human_actor(db) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=str(db.user_id),
        user_id=db.user_id,
        workspace_id=db.workspace_id,
        role="owner",
        token_kind="session",
        provenance="browser_session",
    )


def _add_api_token(session, db, *, name: str, lookup_hash: str) -> ApiToken:
    token = ApiToken(
        user_id=db.user_id,
        name=name,
        token_lookup_hash=lookup_hash,
        hashed_token="not-used",
        scopes=["mcp:read"],
    )
    session.add(token)
    session.flush()
    session.add(
        ApiTokenWorkspace(
            token_id=token.id,
            workspace_id=db.workspace_id,
            desired_scopes=["mcp:read"],
            revision=1,
            sync_state="synced",
        )
    )
    return token


def test_mcp_client_counts_and_page_share_one_sqlite_snapshot(db):
    inserted = False

    def insert_after_total_count(_conn, _cursor, statement, *_args):
        nonlocal inserted
        normalized = " ".join(statement.lower().split())
        if (
            inserted
            or "select count(*)" not in normalized
            or "mcp_client" not in normalized
        ):
            return
        inserted = True
        with db.factory.begin() as writer:
            writer.add(
                MCPClient(
                    workspace_id=db.workspace_id,
                    name="Concurrent client",
                    client_type="generic",
                    scopes=["mcp:read"],
                    status="active",
                    token_metadata={},
                )
            )

    event.listen(db.engine, "after_cursor_execute", insert_after_total_count)
    try:
        # Two sessions, the single-database profile's shape: the control reads
        # (workspace gate + OAuth health) finish and release their transaction
        # before the workspace reader's snapshot opens, so the concurrent write
        # below still lands inside the window this test is about.
        with db.factory() as control_reader, db.factory() as reader:
            page = SettingsService().list_mcp_clients(
                control_session=control_reader,
                workspace_session=reader,
                workspace_id=db.workspace_id,
                actor=_human_actor(db),
                cursor_secret="snapshot-secret",
                status="all",
            )
    finally:
        event.remove(db.engine, "after_cursor_execute", insert_after_total_count)

    assert inserted
    assert page.total_count == page.filtered_count == len(page.data) == 1
    with db.factory() as observer:
        assert observer.scalar(select(func.count()).select_from(MCPClient)) == 2


def test_access_key_counts_and_page_share_one_sqlite_snapshot(db):
    inserted = False

    def insert_after_total_count(_conn, _cursor, statement, *_args):
        nonlocal inserted
        normalized = " ".join(statement.lower().split())
        if (
            inserted
            # D-01v2/T4: list_user_tokens is account-scoped, so its count
            # query is a plain `count(*)` over api_tokens (no
            # api_token_workspaces join, no DISTINCT).
            or "select count(*)" not in normalized
            or "from api_tokens" not in normalized
        ):
            return
        inserted = True
        with db.factory.begin() as writer:
            _add_api_token(
                writer,
                db,
                name="Concurrent key",
                lookup_hash="sha256:concurrent",
            )

    event.listen(db.engine, "after_cursor_execute", insert_after_total_count)
    try:
        page = ApiTokenService(
            session_factory=db.factory,
            credential_hasher=CredentialHasher(),
            cursor_secret="snapshot-secret",
        ).list_user_tokens(
            user_id=db.user_id,
        )
    finally:
        event.remove(db.engine, "after_cursor_execute", insert_after_total_count)

    assert inserted
    assert page.total_count == page.filtered_count == len(page.data) == 1
    with db.factory() as observer:
        assert observer.scalar(select(func.count()).select_from(ApiToken)) == 2


def _outside_writer_blocked(database_path) -> bool:
    contender = sqlite3.connect(database_path, timeout=0)
    try:
        contender.execute("BEGIN IMMEDIATE")
        contender.rollback()
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        contender.close()


def _assert_writer_lock_precedes_first_select(db, call) -> None:
    def inspect_first_select(_conn, _cursor, statement, *_args):
        if statement.lstrip().lower().startswith("select"):
            raise _ReachedGuardedRead(_outside_writer_blocked(db.path))

    event.listen(db.engine, "before_cursor_execute", inspect_first_select)
    try:
        with pytest.raises(_ReachedGuardedRead) as excinfo:
            call()
    finally:
        event.remove(db.engine, "before_cursor_execute", inspect_first_select)
    assert excinfo.value.outside_writer_blocked


def test_settings_client_mutations_lock_before_guarded_reads(db):
    service = SettingsService()
    actor = _human_actor(db)
    calls = (
        lambda control_session, session: service.update_mcp_client_status(
            control_session=control_session,
            workspace_session=session,
            workspace_id=db.workspace_id,
            actor=actor,
            client_id=uuid4(),
            status="disabled",
        ),
        lambda control_session, session: service.delete_mcp_client(
            control_session=control_session,
            workspace_session=session,
            workspace_id=db.workspace_id,
            actor=actor,
            client_id=uuid4(),
        ),
    )
    for call in calls:
        with db.factory() as control_session, db.factory.begin() as session:
            _assert_writer_lock_precedes_first_select(
                db,
                lambda call=call: call(control_session, session),
            )


def test_settings_client_mutations_take_write_intent_on_a_workspace_table(db):
    """The RESERVED-lock statement must name a table the WRITTEN database has.

    ``MCPClient`` is workspace-plane, so these two mutations run on a workspace
    session -- which on a real shard is a database with no ``workspace`` table
    in it at all. A lock statement aimed at ``workspace`` would not merely fail
    to serialize there, it would raise ``no such table``. The single-file
    fixture here cannot show that (both tables exist), so this asserts the
    statement's target directly.
    """

    service = SettingsService()
    actor = _human_actor(db)
    write_intents: list[str] = []

    def capture_write_intent(_conn, _cursor, statement, *_args):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("update ") and normalized.endswith("where 1 = 0"):
            write_intents.append(normalized)

    calls = (
        lambda control_session, session: service.update_mcp_client_status(
            control_session=control_session,
            workspace_session=session,
            workspace_id=db.workspace_id,
            actor=actor,
            client_id=uuid4(),
            status="disabled",
        ),
        lambda control_session, session: service.delete_mcp_client(
            control_session=control_session,
            workspace_session=session,
            workspace_id=db.workspace_id,
            actor=actor,
            client_id=uuid4(),
        ),
    )
    event.listen(db.engine, "before_cursor_execute", capture_write_intent)
    try:
        for call in calls:
            with db.factory() as control_session, db.factory.begin() as session:
                # Both calls die on the missing client; the lock is taken first.
                with pytest.raises(ReadNotFoundError):
                    call(control_session, session)
    finally:
        event.remove(db.engine, "before_cursor_execute", capture_write_intent)

    assert len(write_intents) == 2, write_intents
    assert all("context_page" in statement for statement in write_intents), (
        "the write is workspace-plane, so its lock statement must name a "
        f"workspace-plane table: {write_intents}"
    )


def test_api_token_mutations_lock_before_guarded_reads(db):
    service = ApiTokenService(
        session_factory=db.factory,
        credential_hasher=CredentialHasher(),
        cursor_secret="snapshot-secret",
    )
    calls = (
        lambda: service.create_token(
            user_id=db.user_id,
            workspace_grants=[WorkspaceGrantRequest(workspace_id=db.workspace_id)],
            name="New key",
            scopes=["mcp:read"],
        ),
        lambda: service.update_token(
            user_id=db.user_id,
            token_id=uuid4(),
            name="Updated key",
        ),
        lambda: service.revoke_token(user_id=db.user_id, token_id=uuid4()),
        lambda: service.delete_revoked_token(
            user_id=db.user_id,
            token_id=uuid4(),
        ),
        lambda: service.authenticate_token("dtxw_test-token"),
    )
    for call in calls:
        _assert_writer_lock_precedes_first_select(db, call)


def test_runtime_lifecycle_evaluation_locks_before_guarded_read(db):
    actor = ActorContext(
        actor_kind="human",
        actor_id=str(db.user_id),
        user_id=db.user_id,
        workspace_id=db.workspace_id,
        token_kind="oauth_jwt",
        token_id=uuid4(),
        client_id="lock-order-oauth",
        token_workspace_ids=(db.workspace_id,),
        provenance="oauth_client",
    )
    with db.factory.begin() as session:
        _assert_writer_lock_precedes_first_select(
            db,
            lambda: evaluate_mcp_client_runtime(
                session,
                workspace_id=db.workspace_id,
                actor=actor,
            ),
        )


def _oauth_client(client_id: str, *, client_name: str) -> OAuthClient:
    return OAuthClient(
        id=client_id,
        client_name=client_name,
        redirect_uris=["http://127.0.0.1:54321/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        application_type="native",
        scope="mcp:read",
        created_via="dcr",
    )


def _mcp_client_in_bind_counts(db, run) -> list[int]:
    """Bind-parameter counts of every workspace-plane MCPClient ``IN`` statement."""

    counts: list[int] = []

    def record(_conn, _cursor, statement, parameters, *_args):
        normalized = " ".join(statement.lower().split())
        if "mcp_client" not in normalized or " in (" not in normalized:
            return
        try:
            counts.append(len(parameters))
        except TypeError:  # pragma: no cover - defensive, parameters is a sequence
            counts.append(-1)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        run()
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    return counts


def test_name_search_pushdown_is_bounded_by_this_workspace(db):
    """A search term must not drag every tenant's OAuth apps into workspace SQL.

    ``client_name`` matching used to run an unrestricted ``LIKE`` over the whole
    control-plane ``oauth_client`` table and push every id it found down as an
    ``IN`` list against this workspace's ``mcp_client`` rows. The ``IN`` only
    ever filtered this workspace's rows, so nothing leaked -- but the bind
    count was set by a global table and steered by an attacker-supplied string,
    which is an availability problem in a viewer-reachable admin read.

    The population the filter can possibly match is the set of OAuth client ids
    this workspace's own rows name, so bounding the match to that set is
    result-identical and workspace-bounded.
    """

    foreign_client_count = 200
    with db.factory.begin() as session:
        session.add(_oauth_client("app-mine", client_name="Common App Mine"))
        for index in range(foreign_client_count):
            session.add(
                _oauth_client(f"app-{index}", client_name=f"Common App {index}")
            )
        session.add(
            MCPClient(
                workspace_id=db.workspace_id,
                name="OAuth connection",
                client_type="oauth",
                scopes=["mcp:read"],
                status="active",
                issued_to_user_id=db.user_id,
                token_metadata={"oauth_client_id": "app-mine"},
            )
        )

    page = None

    def run():
        nonlocal page
        with db.factory() as control_session, db.factory() as workspace_session:
            page = SettingsService().list_mcp_clients(
                control_session=control_session,
                workspace_session=workspace_session,
                workspace_id=db.workspace_id,
                actor=_human_actor(db),
                status="all",
                query="common app",
                cursor_secret="bind-count-secret",
            )

    counts = _mcp_client_in_bind_counts(db, run)

    # Behaviour first: the search still finds the workspace's own connection by
    # its OAuth client_name, which is the whole point of the name arm.
    assert [row.name for row in page.data] == ["OAuth connection"]
    assert page.filtered_count == 1

    assert counts, "expected at least one MCPClient IN statement to be measured"
    assert max(counts) < foreign_client_count, (
        "the name-search key set scales with the global oauth_client table: "
        f"bind counts {counts} for a workspace holding a single connection"
    )


def test_pushdown_guard_counts_the_name_search_key_set(monkeypatch):
    """The guard has to measure the set that is actually pushed down.

    It used to count only ``active_connection_keys()``, which is bounded by
    this workspace's consents. The name-search set is the one that is neither
    workspace-bounded at HEAD nor O(1), so a guard that cannot see it reports
    "nothing to look at" on exactly the request that needs looking at.
    """

    monkeypatch.setattr(
        settings_service, "MCP_CLIENT_PUSHDOWN_KEY_WARNING_THRESHOLD", 3
    )
    health = WorkspaceOAuthHealth(
        workspace_id=uuid4(),
        now=utc_now(),
        known_oauth_client_ids=frozenset(),
        oauth_client_names={},
        name_matched_oauth_client_ids=frozenset(f"app-{index}" for index in range(9)),
    )

    # NOT caplog: it reads through the root logger, and a CLI test's
    # dictConfig(disable_existing_loggers=True) disables this logger for the
    # rest of the run, so the assertion below would pass or fail according to
    # which module the suite loaded first. captured_log_records does the four
    # steps that survive that -- see its docstring in backend/tests/conftest.py.
    with captured_log_records("backend.services.settings_service") as records:
        settings_service._warn_if_pushdown_key_set_is_large(health)

    warnings = [
        record
        for record in records
        if record.getMessage() == "settings.mcp_clients.pushdown_keys_large"
    ]
    assert warnings, (
        "no warning for a 9-key name-search pushdown against a threshold of 3"
    )
    assert warnings[0].pushdown_key_count == 9


def test_two_users_sharing_one_oauth_client_are_faceted_apart(db):
    """The exact ``(user, client)`` arm decides these two rows, nothing else.

    Both connections name the SAME OAuth client and both are physically
    ``active``; only one user holds a live refresh token. Collapsing the exact
    key to a client-id-only membership test -- the obvious simplification of
    ``tuple_(issued_to_user_id, oauth_client_id).in_(...)`` -- makes both rows
    Active and both facets wrong, in the same direction, with no other test
    noticing.
    """

    with db.factory.begin() as session:
        other_user = User(
            email="second-holder@example.com",
            password_hash="not-used",
            display_name="Second holder",
        )
        session.add(other_user)
        session.add(_oauth_client("shared-app", client_name="Shared App"))
        session.flush()
        other_user_id = other_user.id
        live_grant_id = uuid4()
        for user_id, name in (
            (db.user_id, "Live holder"),
            (other_user_id, "Tokenless holder"),
        ):
            session.add(
                OAuthConsent(
                    user_id=user_id,
                    client_id="shared-app",
                    workspace_id=db.workspace_id,
                    scope="mcp:read",
                )
            )
            session.add(
                MCPClient(
                    workspace_id=db.workspace_id,
                    name=name,
                    client_type="oauth",
                    scopes=["mcp:read"],
                    status="active",
                    issued_to_user_id=user_id,
                    runtime_binding_key=f"oauth_grant:{live_grant_id}"
                    if user_id == db.user_id
                    else None,
                    token_metadata={"oauth_client_id": "shared-app"},
                )
            )
        session.add(
            OAuthRefreshToken(
                grant_id=live_grant_id,
                token_lookup_hash="sha256:live-holder",
                user_id=db.user_id,
                client_id="shared-app",
                scope="mcp:read",
                expires_at=utc_now() + timedelta(days=7),
            )
        )

    def listing(status: str):
        with db.factory() as control_session, db.factory() as workspace_session:
            return SettingsService().list_mcp_clients(
                control_session=control_session,
                workspace_session=workspace_session,
                workspace_id=db.workspace_id,
                actor=_human_actor(db),
                status=status,
                cursor_secret="shared-client-secret",
            )

    everything = listing("all")
    states = {row.name: row.connection_state for row in everything.data}
    assert states["Live holder"] == "active"
    assert states["Tokenless holder"] == "unknown"

    # The facet is the SQL twin of that classification, so the server-side
    # split has to land the two rows where the per-row rendering just did.
    # `Seed client` is the fixture's OAuth-less row and rides the first arm.
    assert {row.name for row in listing("active").data} == {
        "Live holder",
    }
    assert {row.name for row in listing("disabled").data} == {
        "Tokenless holder",
        "Seed client",
    }
    assert everything.status_counts.active == 1
    assert everything.status_counts.disabled == 2


def test_active_connection_predicate_compiles_on_postgresql():
    """The pushdown's row-value ``IN`` has to be legal SQL on the real target.

    The rewrite replaced portable correlated ``EXISTS`` subqueries with
    ``tuple_(Uuid column, JSON text extraction).in_(...)``. There is no
    PostgreSQL server in this environment, so this compiles the expression
    against the PostgreSQL dialect rather than executing it: it catches a shape
    psycopg2 could never render, not a runtime behaviour difference. The
    executing counterpart is
    ``test_c9_mcp_connections.test_postgres_mcp_client_listing_facets_split``,
    which is skipped without a live database.
    """

    workspace_id = uuid4()
    user_id = uuid4()
    health = WorkspaceOAuthHealth(
        credential_health={
            (user_id, f"oauth_grant:{uuid4()}"): CredentialHealth(
                "active", utc_now(), "shared-app"
            )
        },
        workspace_id=workspace_id,
        now=utc_now(),
        known_oauth_client_ids=frozenset({"shared-app"}),
        oauth_client_names={"shared-app": "Shared App"},
        name_matched_oauth_client_ids=frozenset({"shared-app"}),
    )
    compiled = str(
        settings_service._mcp_client_active_connection_predicate(health).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "mcp_client.issued_to_user_id" in compiled
    assert "token_metadata" in compiled
