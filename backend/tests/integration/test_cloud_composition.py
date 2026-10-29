"""Sprint-2 completion gate: a cloud overlay composes with the core app.

A fake routing-capable DatabaseProvider plus an overlay lifespan are injected
through ``assemble``/``create_app``; the test asserts the three assembly
contracts an overlay depends on (R5'/R7'/B-12):

1. the overlay lifespan runs WITH the core MCP lifespan, not instead of it;
2. the injected provider is preserved end to end (never re-wrapped);
3. workspace-scoped tool dispatch acquires sessions via
   ``session_for(workspace_id)`` so a sharding provider sees the routing key.
"""

import ast
import base64
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import APIRouter, Response
from fastapi.testclient import TestClient
from pydantic import AnyUrl
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import OperationalError

from backend.app.container import (
    BlobStoreProvider,
    DatabaseProvider,
    ServiceRegistry,
    ToolCallContext,
    assemble,
)
from backend.app.db import make_engine, make_session_factory
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.app.state import get_session_factory_from_app
from backend.mcp.protocol.prompts_resources import (
    CONTEXT_WORKFLOW_PROMPT_NAME,
    get_dtxwiki_prompt,
    list_dtxwiki_prompts,
    list_dtxwiki_resources,
    read_dtxwiki_resource,
)
from backend.mcp.protocol.runner import call_tool_async
from backend.mcp.router import PolicyRejection, dispatch, resolve_target_actor
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.base import Base
from backend.models.context import ContextPage
from backend.models.context_media import ContextMediaAsset
from backend.models.job import Job
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.mail import EmailOutbox
from backend.models.mcp_chat import McpChatSession
from backend.models.mcp_trace import MCPToolCall
from backend.models.media import MediaInstallRequest, StoredBlobRow
from backend.models.oauth import OAuthClient, OAuthConsent, OAuthRefreshToken
from backend.models.user import User
from backend.models.workspace import (
    MCPClient,
    Workspace,
    WorkspaceMembership,
    default_library_policy,
)
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.auth import ActorContext
from backend.policy.rbac import require_workspace_role
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_media_access import resolve_public_context_media_scope
from backend.services.credential_hashing import CredentialHasher
from backend.services.db_write_intent import (
    acquire_workspace_write_intent,
    acquire_write_intent,
)
from backend.services.entity_ref import context_resource_uri
from backend.services.job_queue import JobQueueService
from backend.services.mail_delivery import EmailDeliveryService
from backend.services.oauth_service import OAuthService
from backend.services.page_service import PageService
from backend.services.password_recovery_service import PasswordRecoveryService
from backend.services.public_page_exposure import grant_public_page_exposure
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError
from backend.services.restricted_library_policy import (
    resolve_restricted_library_target,
)
from backend.services.settings_service import SettingsService
from backend.services.workspace_read import WorkspaceReadService
from backend.tenancy_manifest import (
    CONTROL_PLANE_TABLES,
    MIXED_PLANE_TABLES,
    WORKSPACE_PLANE_TABLES,
)
from backend.tests.conftest import authenticate_test_client
from backend.worker.runtime import OneShotWorker

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c"
    "020000000b4944415478da6364f80f00010501012718e3660000000049454e44"
    "ae426082"
)


class RecordingProvider(DatabaseProvider):
    def __init__(self, settings):
        super().__init__(settings)
        self.session_for_calls: list[object] = []

    def session_for(self, workspace_id):
        self.session_for_calls.append(workspace_id)
        return super().session_for(workspace_id)


class SplitProvider(DatabaseProvider):
    def __init__(self, control_factory, data_factory):
        super().__init__(None, session_factory=control_factory)
        self._control_factory = control_factory
        self._data_factory = data_factory
        self.control_calls = 0
        self.session_for_calls: list[object] = []

    def control_session_factory(self):
        self.control_calls += 1
        return self._control_factory

    def session_for(self, workspace_id):
        self.session_for_calls.append(workspace_id)
        return self._data_factory


def _persist_pat_actor(
    session_factories,
    *,
    workspace_roles: dict[UUID, str],
    default_workspace_id: UUID,
    scopes: tuple[str, ...],
    label: str,
) -> ActorContext:
    """Persist the complete bearer identity direct protocol calls normally receive.

    Split-provider tests create their own databases instead of using the shared
    ``mcp_default_actor`` fixture. Mirror the authenticated user, PAT index,
    membership, and workspace grant into each synthetic plane so these tests
    exercise the server HTTP identity shape rather than the removed stdio actor.
    """

    if default_workspace_id not in workspace_roles:
        raise ValueError("default workspace must be one of the PAT workspaces")
    user_id = uuid4()
    token_id = uuid4()
    for session_factory in session_factories:
        with session_factory.begin() as session:
            session.add(
                User(
                    id=user_id,
                    email=f"{label}-{user_id}@example.com",
                    password_hash="fixture-only",
                    display_name=label,
                    default_workspace_id=default_workspace_id,
                )
            )
            session.add(
                ApiToken(
                    id=token_id,
                    user_id=user_id,
                    name=label,
                    token_lookup_hash=f"fixture-lookup-{token_id}",
                    hashed_token=f"fixture-hash-{token_id}",
                    scopes=list(scopes),
                    mcp_client_type="generic",
                    mcp_client_label=label,
                )
            )
            for workspace_id, role in workspace_roles.items():
                session.add_all(
                    [
                        WorkspaceMembership(
                            user_id=user_id,
                            workspace_id=workspace_id,
                            role=role,
                        ),
                        ApiTokenWorkspace(
                            token_id=token_id,
                            workspace_id=workspace_id,
                            desired_scopes=list(scopes),
                            revision=1,
                            sync_state="synced",
                        ),
                        WorkspaceCredentialGrant(
                            workspace_id=workspace_id,
                            credential_kind="pat",
                            credential_id=token_id,
                            user_id=user_id,
                            granted_scopes=list(scopes),
                            synced_revision=1,
                        ),
                    ]
                )

    role = workspace_roles[default_workspace_id]
    return ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        token_kind="api_token",
        token_id=token_id,
        scopes=scopes,
        token_workspace_ids=tuple(workspace_roles),
        user_id=user_id,
        workspace_id=default_workspace_id,
        workspace_role=role,
        role=role,
        provenance="personal_access_token",
    )


class ProtocolOnlyProvider:
    """Structural provider with no concrete-only attributes."""

    def __init__(self, control_factory, data_factory=None):
        self._control_factory = control_factory
        self._data_factory = data_factory or control_factory

    def control_session_factory(self):
        return self._control_factory

    def session_for(self, workspace_id):
        return self._data_factory


class RecordingBlobStoreProvider(BlobStoreProvider):
    """D-5 test double: records every workspace_id passed to store_for."""

    def __init__(self, store):
        super().__init__(None, store=store)
        self.store_for_calls: list[object] = []

    def store_for(self, workspace_id):
        self.store_for_calls.append(workspace_id)
        return super().store_for(workspace_id)


class SplitBlobStoreProvider(BlobStoreProvider):
    """Routes each workspace to a distinct physical-store sentinel."""

    def __init__(self, stores):
        super().__init__(None, store=object())
        self.stores = stores
        self.store_for_calls: list[object] = []

    def store_for(self, workspace_id):
        self.store_for_calls.append(workspace_id)
        return self.stores[workspace_id]


class SplitWorkspaceDirectory:
    """Control-plane directory double for globally routable entity IDs."""

    def __init__(self, *, workspace_id, page_ids=()):
        self.workspace_id = workspace_id
        self.page_ids = set(page_ids)

    def workspace_id_by_slug(self, session, workspace_slug):
        del session
        return self.workspace_id if workspace_slug == "split" else None

    def workspace_id_by_context_page(self, session, page_id):
        del session
        return self.workspace_id if page_id in self.page_ids else None

    def slug_by_workspace_id(self, session, workspace_id):
        # P9.1: `context_pages.py`'s create/by-slug routes resolve the
        # workspace's own slug from its id (the reverse of
        # `workspace_id_by_slug` above) -- this double only ever models one
        # workspace, slug "split".
        del session
        return "split" if workspace_id == self.workspace_id else None


class DataDbAssertingIndexer:
    def __init__(self, provider: DatabaseProvider):
        self.provider = provider
        self.seen_title: str | None = None

    def upsert(self, *, workspace_id, source_kind, source_id):
        assert source_kind == "context_page"
        with self.provider.session_for(workspace_id).begin() as session:
            page = session.get(ContextPage, source_id)
            if page is None:
                raise AssertionError("worker opened the control DB for domain work")
            self.seen_title = page.title
            page.title = "Worker wrote data DB"

    def delete(self, *, workspace_id, source_kind, source_id):
        raise AssertionError("delete was not expected")

    def rebuild(self, *, workspace_id, source_kind=None, source_id=None):
        return {}


@dataclass(frozen=True)
class TrueShard:
    """A control database plus a workspace shard that is genuinely separable.

    The distinction this type exists to make. Handing BOTH engines
    ``Base.metadata.create_all`` proves *routing* -- which factory a call
    reached for -- because a read on the wrong plane answers ZERO ROWS
    instead of raising. It cannot prove *separability*, because every table exists on
    both sides, so no cross-plane reference is ever unsatisfiable and no
    misrouted statement can fail. A true shard carries the workspace-plane
    tables and none of the control-plane ones, so a statement that reaches for
    the wrong plane raises ``OperationalError: no such table: ...``.

    Both shapes are legitimate and most of this module wants the routing one:
    decoy rows on both planes name which plane answered, which a shard cannot
    do for a read that must succeed. Use this fixture when the claim under
    test is that a plane STANDS ALONE.

    Shape of this module, from these commands rather than from a review -- a
    snapshot, not a contract, so re-run them instead of trusting the digits::

        env -u DTXWIKI_MCP_PAT .venv/bin/python -m pytest \\
            backend/tests/integration/test_cloud_composition.py \\
            --collect-only -q | tail -1
        grep -c 'Base[.]metadata[.]create_all(' \\
            backend/tests/integration/test_cloud_composition.py

    33 collected tests over 38 schema builds now; run against
    ``git show HEAD:`` of this file from before the fixture existed, 31 and
    41. The counts barely moved, which is the point -- the hole was never how
    many schemas got built. It was that of those 31 tests exactly ONE dropped
    the manifest's control-plane set, and a second dropped a single table by
    hand while its own name claimed "no control tables". Everything else gave
    both engines everything, so it could only ever have proved routing.

    The structural invariant is asserted, not described: no test in this
    module may hand-roll a shard, and every table this fixture drops is
    verified gone before any test sees it.
    """

    control_factory: object
    shard_factory: object
    seed_factory: object
    control_path: Path
    shard_path: Path
    dropped_tables: frozenset[str]
    remaining_tables: frozenset[str]

    def expect_missing_table(self, table: str):
        """``pytest.raises`` for a statement that reached the wrong plane.

        SQLite spells the same failure two ways depending on who wrote the
        SQL: the ORM emits ``no such table: main.workspace`` (schema-qualified)
        while ``acquire_write_intent``'s hand-written ``UPDATE workspace``
        gets ``no such table: workspace``. Both are measured in this module,
        so the pattern accepts either and the trailing word boundary keeps
        ``workspace`` from matching ``workspace_memberships``.
        """

        assert table in self.dropped_tables, (
            f"{table!r} is not a table this shard dropped; asserting it is "
            f"missing would pass for the wrong reason. Dropped: "
            f"{sorted(self.dropped_tables)}"
        )
        pattern = rf"no such table: (?:main\.)?{re.escape(table)}\b"
        return pytest.raises(OperationalError, match=pattern)


def build_true_shard(tmp_path, *, name: str = "shard") -> TrueShard:
    """Build a control database and a workspace shard with no control tables.

    Generalized from the only test in this module that ever dropped the
    manifest's whole control-plane set by hand
    (``test_settings_mcp_client_writes_still_fail_on_a_real_shard``), so that
    asserting separability costs one fixture argument rather than a copy of
    its DDL surgery. One other test dropped a single control table by hand;
    it is converted here too, which is how the two shapes were found to
    disagree about what "a shard" meant.

    The control side keeps the FULL schema on purpose: ``single_database`` is
    the shipped topology, so the control database is the one that holds
    everything today. Only the shard is reduced.
    """

    control_path = tmp_path / f"{name}-control.sqlite"
    shard_path = tmp_path / f"{name}-data.sqlite"
    control_engine = make_engine(f"sqlite+pysqlite:///{control_path}")
    shard_engine = make_engine(f"sqlite+pysqlite:///{shard_path}")
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(shard_engine)

    # `.tables`, not `.sorted_tables`: sorting emits a cycle warning on this
    # metadata and dependency order is irrelevant to a name census.
    mapped = frozenset(Base.metadata.tables)
    droppable = frozenset(CONTROL_PLANE_TABLES & mapped)
    raw = sqlite3.connect(shard_path)
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        for table in sorted(droppable):
            raw.execute(f"DROP TABLE IF EXISTS {table}")
        raw.commit()
        remaining = frozenset(
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        )
    finally:
        raw.close()

    # Three build-time checks, so a fixture that has quietly stopped being a
    # shard fails here instead of making every separability assertion in the
    # module pass vacuously.
    assert droppable, (
        "the tenancy manifest and Base.metadata no longer share a single "
        "control-plane table name, so this fixture dropped nothing and is "
        "not a shard"
    )
    leftovers = remaining & CONTROL_PLANE_TABLES
    assert not leftovers, (
        f"control-plane tables survived the drop: {sorted(leftovers)} -- a "
        "statement reaching for the wrong plane would silently succeed"
    )
    kept = remaining & WORKSPACE_PLANE_TABLES
    assert kept, (
        "no workspace-plane table survived, so this shard cannot answer "
        "anything and 'the read half still works' would prove nothing"
    )

    return TrueShard(
        control_factory=make_session_factory(control_engine),
        shard_factory=make_session_factory(shard_engine),
        # NOT a redundant third factory. ``make_engine`` sets
        # ``PRAGMA foreign_keys=ON`` (backend/app/db.py), and on a true shard
        # the FK parents of most workspace-plane rows are the tables that were
        # just dropped, so seeding through ``shard_factory`` raises before any
        # test can begin. This engine is deliberately pragma-free so a test
        # can PLANT the rows whose parents live on the other plane; every
        # statement the code under test runs still goes through
        # ``shard_factory`` with enforcement on.
        #
        # This is the hack the fixture could not remove, and its necessity is
        # asserted rather than asserted-in-prose by
        # ``test_true_shard_seeding_still_needs_foreign_keys_off`` below --
        # which goes RED the day the workspace->control FK edges are closed,
        # at which point this factory should be deleted.
        seed_factory=make_session_factory(
            create_engine(f"sqlite+pysqlite:///{shard_path}", future=True)
        ),
        control_path=control_path,
        shard_path=shard_path,
        dropped_tables=droppable,
        remaining_tables=remaining,
    )


@pytest.fixture
def true_shard(tmp_path) -> TrueShard:
    """The default one-shard case; call ``build_true_shard`` for more."""

    return build_true_shard(tmp_path)


def test_true_shard_seeding_still_needs_foreign_keys_off(true_shard):
    """Why ``TrueShard.seed_factory`` exists, as an assertion.

    On a true shard, inserting a workspace-plane row whose FK parent is a
    control-plane table fails outright -- not with an integrity error about a
    missing parent ROW, but because the parent TABLE is gone. Nullability is
    irrelevant: SQLite resolves the referenced table when it enforces the
    constraint, so the statement dies whatever the column holds.

    This test is the fixture's own self-invalidation. If the workspace->control
    FK edges are ever closed, ``MCPClient`` inserts start succeeding through
    the pragma-configured factory, this test goes red, and ``seed_factory``
    plus every comment justifying it should be deleted rather than kept as
    folklore.
    """

    row = dict(
        workspace_id=uuid4(),
        name="fk-probe",
        client_type="oauth",
        scopes=["mcp:read"],
        status="active",
        issued_to_user_id=uuid4(),
    )

    assert "users" in true_shard.dropped_tables
    with true_shard.expect_missing_table("users"):
        with true_shard.shard_factory.begin() as session:
            session.add(MCPClient(**row))

    # The pragma-free factory is the only reason a shard test can be seeded.
    with true_shard.seed_factory.begin() as session:
        session.add(MCPClient(**row))
    with true_shard.shard_factory() as session:
        assert session.scalar(select(func.count(MCPClient.id))) == 1


def test_true_shard_is_the_only_hand_rolled_shard_in_this_module():
    """Separability must go through the fixture, and be used by more than one
    test.

    The first assertion is the one that matters: a hand-rolled shard elsewhere
    in this module would skip ``build_true_shard``'s three build-time checks,
    so it could drop nothing, or drop everything, and its test would still
    pass. The needle is assembled at runtime because a literal would match
    this test's own source and make the check pass for the wrong reason.
    """

    source = Path(__file__).read_text(encoding="utf-8")
    module = ast.parse(source)
    builder = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_true_shard"
    )
    needle = "DROP" + " TABLE"
    offenders = [
        (number, line.strip())
        for number, line in enumerate(source.splitlines(), start=1)
        if needle in line.upper()
        and not (builder.lineno <= number <= builder.end_lineno)
    ]
    assert not offenders, (
        "a shard was built by hand instead of through build_true_shard, so it "
        f"skipped the fixture's build-time checks: {offenders}"
    )

    users = sorted(
        node.name
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and (
            "true_shard" in {argument.arg for argument in node.args.args}
            or any(
                isinstance(inner, ast.Name)
                and inner.id in {"true_shard", "build_true_shard"}
                for inner in ast.walk(node)
            )
        )
    )
    assert len(users) >= 3, (
        "a shared true-shard fixture used by fewer than three tests is not "
        f"shared -- the module has only {users}. Deliverable 4 asked for the "
        "generalization plus at least two converted separability tests."
    )


def test_cloud_overlay_composition_boot(tmp_path):
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'cloud.sqlite'}",
    )
    provider = RecordingProvider(settings)
    container = assemble(settings, db=provider)

    events: list[str] = []

    @asynccontextmanager
    async def overlay_lifespan(app):
        events.append("overlay-start")
        yield
        events.append("overlay-stop")

    app = create_app(
        container=container,
        extra_lifespan=overlay_lifespan,
        bootstrap_on_startup=True,
    )

    with TestClient(app):
        # (1) both lifespans ran; MCP state installed inside the overlay.
        assert events == ["overlay-start"]
        assert getattr(app.state, "mcp_session_manager", None) is not None
        # (2) the injected provider is the app's provider — not re-wrapped.
        assert app.state.container.db is provider
    assert events == ["overlay-start", "overlay-stop"]

    # (3) a workspace-scoped tool call routes its sessions through
    # session_for(workspace_id) with the resolved target workspace.
    page_service = container.get_page_service()
    with provider.session_factory.begin() as session:
        workspace_id = seed_default_workspace(session).id
    actor = _persist_pat_actor(
        (provider.session_factory,),
        workspace_roles={workspace_id: "owner"},
        default_workspace_id=workspace_id,
        scopes=("mcp:read",),
        label="cloud-composition-test",
    )
    ctx = ToolCallContext(
        actor=actor,
        db=provider,
        page_service=page_service,
        services=container.get_services(),
    )
    provider.session_for_calls.clear()
    output = dispatch(
        "guide",
        {"workspace_id": str(workspace_id)},
        tool_context=ctx,
    )

    assert output.envelope.result_status == "succeeded"
    assert workspace_id in provider.session_for_calls


def test_list_agent_page_refs_respects_bounded_limit():
    # M-05: the MCP resources/prompts LIST must advertise a bounded number of
    # per-page entry points instead of the whole catalog.
    from backend.services.mcp_surface_service import list_agent_page_refs

    engine = make_engine("sqlite+pysqlite:///:memory:")

    # make_engine() only enables foreign keys for file-backed SQLite, so turn
    # them on here to keep the AgentPage -> workspace FK enforced as before.
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        workspace = seed_default_workspace(session)
        for index in range(5):
            session.add(
                AgentPage(
                    workspace_id=workspace.id,
                    category="skill",
                    title=f"Skill {index:02d}",
                    slug=f"skill-{index:02d}",
                    sensitivity="workspace",
                    lifecycle_status="active",
                )
            )
        workspace_id = workspace.id

    with factory() as session:
        bounded = list_agent_page_refs(session, workspace_id, limit=2)
        unbounded = list_agent_page_refs(session, workspace_id)

    assert len(bounded) == 2
    assert len(unbounded) > 2


def test_protocol_only_provider_boots_without_state_backdoors(tmp_path):
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'protocol-only.sqlite'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    provider = ProtocolOnlyProvider(factory)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
    )
    container = assemble(settings, db=provider)
    app = create_app(container=container, bootstrap_on_startup=False)

    with TestClient(app) as client:
        response = client.get("/health/live")
        resolved_factory = get_session_factory_from_app(SimpleNamespace(app=client.app))

    assert response.status_code == 200
    assert resolved_factory is factory


def test_overlay_assembly_rejects_ambiguous_inputs_and_preserves_overrides(monkeypatch):
    # assemble() only wires objects, so an unconnected factory is enough.
    factory = make_session_factory(make_engine("sqlite+pysqlite:///:memory:"))
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")
    provider = ProtocolOnlyProvider(factory)
    services = ServiceRegistry()
    directory = SplitWorkspaceDirectory(workspace_id=UUID(int=1))

    container = assemble(
        settings,
        db=provider,
        directory=directory,
        services=services,
    )

    assert container.db is provider
    assert container.services is services
    assert container.services.workspace_directory is directory

    # A cold registry must not split one user's request budget between two
    # services. The second accessor reaches either the constructor (unsafe
    # cached_property) or the held initialization lock before the first is freed.
    from backend.services import page_location

    constructing = Event()
    contender = Event()
    release_first = Event()
    initialization_lock = Lock()
    instances = []

    class ObservedInitializationLock:
        def __enter__(self):
            if constructing.is_set():
                contender.set()
            initialization_lock.acquire()
            return self

        def __exit__(self, *_args):
            initialization_lock.release()

    def make_page_locations():
        instance = object()
        instances.append(instance)
        if len(instances) == 1:
            constructing.set()
            assert release_first.wait(5), "first constructor was not released"
        else:
            contender.set()
        return instance

    monkeypatch.setattr(
        services, "_page_locations_lock", ObservedInitializationLock(), raising=False
    )
    monkeypatch.setattr(page_location, "PageLocationService", make_page_locations)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(lambda: services.page_locations)
        try:
            assert constructing.wait(5), "first constructor did not start"
            second = executor.submit(lambda: services.page_locations)
            assert contender.wait(5), "second accessor did not reach initialization"
        finally:
            release_first.set()
        assert first.result(timeout=5) is second.result(timeout=5)
    assert services.page_locations is instances[0]
    override = object()
    services.__dict__["page_locations"] = override
    assert services.page_locations is override

    with pytest.raises(ValueError, match="cannot both be provided"):
        assemble(settings, db=provider, session_factory=factory)
    with pytest.raises(ValueError, match="same instance"):
        create_app(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            container=container,
            bootstrap_on_startup=False,
        )


def test_blob_store_provider_survives_composition_and_routes_by_real_workspace_id():
    """Composition preserves the injected blob provider and routes by workspace.

    A durable media installer must call ``store_for`` with its own workspace ID.
    Passing ``None`` would leave uploads on the unscoped storage route.
    """

    fake_store = object()
    provider = RecordingBlobStoreProvider(fake_store)
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")

    container = assemble(settings, blob_store_provider=provider)

    assert container.services.blob_store_provider is provider

    workspace_id = uuid4()
    installer = container.services.durable_media_installer_factory(
        object(), workspace_id
    )

    assert provider.store_for_calls == [workspace_id]
    assert installer._stager is fake_store


@pytest.mark.parametrize("precompose_services", [False, True])
def test_worker_blob_services_preserve_split_store_overlay(precompose_services):
    """Worker-owned services must stay on the injected physical-store plane.

    This covers both supported composition paths: passing a BlobStoreProvider
    directly to OneShotWorker and passing a registry an overlay has already
    composed.  The previous per-handler ``ServiceRegistry()`` construction
    ignored both and routed every worker operation through the default store.
    """

    workspace_a = uuid4()
    workspace_b = uuid4()
    store_a = object()
    store_b = object()
    provider = SplitBlobStoreProvider(
        {
            workspace_a: store_a,
            workspace_b: store_b,
        }
    )
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")
    services = ServiceRegistry()
    services.settings = settings
    if precompose_services:
        services.__dict__["blob_store_provider"] = provider
        worker = OneShotWorker(services=services)
    else:
        worker = OneShotWorker(
            settings=settings,
            services=services,
            blob_store_provider=provider,
        )

    conversion = worker._library_conversion_service()
    preview = worker._library_preview_service()
    reconciler = worker._media_install_reconciler()

    assert conversion.store_for(workspace_a) is store_a
    assert preview.store_for(workspace_b) is store_b
    assert reconciler._store_for(workspace_a) is store_a
    assert provider.store_for_calls == [workspace_a, workspace_b, workspace_a]


def test_overlay_can_own_llms_txt_route(monkeypatch):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="overlay-llms-secret",
    )
    container = assemble(settings)
    overlay = APIRouter()

    @overlay.get("/llms.txt")
    def overlay_llms_txt():
        return Response("# Overlay projection\n", media_type="text/markdown")

    def mount_overlay(app):
        app.include_router(overlay)

    def fail_core_renderer(*args, **kwargs):
        raise AssertionError("the Tier-B llms.txt renderer was registered")

    monkeypatch.setattr("backend.api.llms_txt.render_llms_txt", fail_core_renderer)
    app = create_app(
        container=container,
        include_llms_txt=False,
        extra_mount=mount_overlay,
        bootstrap_on_startup=False,
    )

    with TestClient(app) as client:
        response = client.get("/llms.txt")

    assert response.status_code == 200
    assert response.text == "# Overlay projection\n"


def test_split_provider_routes_mcp_surfaces_and_worker_to_data_db(tmp_path):
    control_engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'control.sqlite'}")
    data_engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'data.sqlite'}")
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    with data_factory.begin() as session:
        data_workspace = seed_default_workspace(
            session,
            slug="split",
            title="Data Workspace",
        )
        workspace_id = data_workspace.id
        persona = AgentPage(
            workspace_id=workspace_id,
            category="persona",
            title="Data Only Persona",
            slug="data-only-persona",
            lifecycle_status="active",
            injection_policy="default_on",
            sensitivity="workspace",
        )
        session.add(persona)
        session.flush()
        persona_version = AgentPageVersion(
            workspace_id=workspace_id,
            agent_page_id=persona.id,
            version_no=1,
            body_markdown="# Data Only Persona\n\nData DB prompt.",
            actor_type="human",
            version_status="active_snapshot",
        )
        session.add(persona_version)
        session.flush()
        persona.current_active_version_id = persona_version.id
    data_page = PageService(data_factory).write_page(
        workspace_slug="split",
        slug="data-only-context",
        title="Data Only Context",
        body_md="# Data Only Context\n\nOnly the workspace DB has this body.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    # write_page's own publish maintenance already enqueued this page's embed
    # job (page_service.py:_process_publish_maintenance) directly on the data
    # DB. Epic-2 Phase 6a routes the worker's claim there too now (Job is
    # workspace-plane data, plan §3 D-2 point 3), so this pre-existing job IS
    # the one under test below -- no separate job needs to be seeded, and a
    # second one would just compete with it for the same single claim.
    with control_factory.begin() as session:
        control_workspace = Workspace(
            id=workspace_id,
            slug="split",
            title="Control Workspace",
            name="Control Workspace",
        )
        session.add(control_workspace)
        session.flush()
    with data_factory() as session:
        job = session.scalar(
            select(Job).where(Job.workspace_id == workspace_id, Job.job_type == "embed")
        )
        assert job is not None
        job_id = job.id

    provider = SplitProvider(control_factory, data_factory)
    actor = _persist_pat_actor(
        (control_factory, data_factory),
        workspace_roles={workspace_id: "owner"},
        default_workspace_id=workspace_id,
        scopes=("mcp:read",),
        label="split-provider-test",
    )
    tool_context = ToolCallContext(
        actor=actor,
        db=provider,
        page_service=PageService(data_factory),
    )

    guide = dispatch(
        "guide",
        {"workspace_id": str(workspace_id)},
        tool_context=tool_context,
    )
    assert guide.data.workspace_summary["title"] == "Data Workspace"
    search = dispatch(
        "search",
        {
            "workspace_id": str(workspace_id),
            "query": "Only the workspace DB",
            "scope": "context_page",
            "retrieval_mode": "keyword",
            "limit": 10,
        },
        tool_context=tool_context,
    )
    assert search.envelope.result_status == "succeeded"
    assert data_page.id in {result.id for result in search.data.ranked_results}

    resources = list_dtxwiki_resources(
        actor=actor,
        session_factory=control_factory,
        tool_context=tool_context,
    )
    # E-2: the list advertises entry points only; per-page URIs stay readable
    # without being listed, and both surfaces must route to the data DB.
    resource_names = {resource.name for resource in resources}
    assert {"workspace-index", "workspace-bundle"} <= resource_names
    assert not any(name.startswith("context-page:") for name in resource_names)
    [context_content] = read_dtxwiki_resource(
        uri=AnyUrl(context_resource_uri(workspace_id, "data-only-context")),
        actor=actor,
        session_factory=control_factory,
        tool_context=tool_context,
    )
    assert "Only the workspace DB has this body." in context_content.content
    bundle_resource = next(
        resource for resource in resources if resource.name == "workspace-bundle"
    )
    [bundle_content] = read_dtxwiki_resource(
        uri=AnyUrl(str(bundle_resource.uri)),
        actor=actor,
        session_factory=control_factory,
        tool_context=tool_context,
    )
    assert "Only the workspace DB has this body." in bundle_content.content

    prompts = list_dtxwiki_prompts(
        actor=actor,
        session_factory=control_factory,
        tool_context=tool_context,
    )
    assert any(
        prompt.name == "dtxwiki.agent_page.data-only-persona" for prompt in prompts
    )
    workflow_prompt = get_dtxwiki_prompt(
        name=CONTEXT_WORKFLOW_PROMPT_NAME,
        arguments={"workspace_id": str(workspace_id)},
        actor=actor,
        session_factory=control_factory,
        tool_context=tool_context,
    )
    # Workspace identity/title is control-plane data even though the prompt's
    # Agent/Context bodies are selected from the workspace plane.
    assert "Control Workspace" in workflow_prompt.messages[0].content.text

    indexer = DataDbAssertingIndexer(provider)
    worker = OneShotWorker(
        indexer=indexer,
        database_provider=provider,
    )
    result = worker.dispatch_once_with_session_factory(control_factory)

    assert result is not None
    assert result.status == "completed"
    assert indexer.seen_title == "Data Only Context"
    # Job is workspace-plane data (Phase 6a) -- claim/complete both landed on
    # the data DB, not control, so that is where the row and its completion
    # are observable; control has neither the Job nor the ContextPage it
    # targets.
    with control_factory() as session:
        assert session.get(Job, job_id) is None
        assert session.get(ContextPage, data_page.id) is None
    with data_factory() as session:
        assert session.get(Job, job_id).status == "completed"
        assert session.get(ContextPage, data_page.id).title == "Worker wrote data DB"
    assert provider.control_calls >= 1
    assert provider.session_for_calls.count(workspace_id) >= 6


def test_split_provider_worker_failure_lands_on_data_db_not_control(tmp_path):
    """Epic-2 Phase 6a (plan §3 D-2 point 3): the two-phase dispatcher's
    failure branch must land on session_for(workspace_id), same as the
    success path already covered above -- not on control_session_factory().
    An unregistered job_type is a clean way to force the terminal-failure
    branch without needing a working handler: get_handler returns None,
    _handler_for raises ValueError (one of _TERMINAL_JOB_ERRORS), and the
    failure write goes through _mark_failed_isolated with max_retries=0.
    """
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'fail-control.sqlite'}"
    )
    data_engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'fail-data.sqlite'}")
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    with data_factory.begin() as session:
        workspace = seed_default_workspace(session, slug="split-fail")
        workspace_id = workspace.id
        job = Job(
            workspace_id=workspace_id,
            job_type="no-such-job-type",
            trigger_type="split_provider_fail_test",
            status="queued",
            target_ref={},
            idempotency_key="split-provider-fail",
        )
        session.add(job)
        session.flush()
        job_id = job.id

    with control_factory.begin() as session:
        session.add(
            Workspace(
                id=workspace_id,
                slug="split-fail",
                title="Control Workspace",
                name="Control Workspace",
            )
        )

    provider = SplitProvider(control_factory, data_factory)
    worker = OneShotWorker(database_provider=provider)

    result = worker.dispatch_once_with_session_factory(control_factory)

    assert result is not None
    assert result.status == "failed"

    with control_factory() as session:
        assert session.get(Job, job_id) is None

    with data_factory() as session:
        stored = session.get(Job, job_id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.retry_count == 1


def test_split_provider_reclaim_lands_on_data_db_not_control(tmp_path):
    """Epic-2 Phase 6a (plan §3 D-2 point 1): reclaim_stale_jobs_with_
    session_factory's roster read is the only thing allowed on the control
    plane -- the reclaim itself (and the resulting status write) must happen
    on session_for(workspace_id)."""
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'reclaim-control.sqlite'}"
    )
    data_engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'reclaim-data.sqlite'}")
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    with data_factory.begin() as session:
        workspace = seed_default_workspace(session, slug="split-reclaim")
        workspace_id = workspace.id
        job = Job(
            workspace_id=workspace_id,
            job_type="extraction",
            trigger_type="split_provider_reclaim_test",
            status="running",
            started_at=datetime.now(UTC) - timedelta(seconds=1200),
            target_ref={},
            idempotency_key="split-provider-reclaim",
        )
        session.add(job)
        session.flush()
        job_id = job.id

    with control_factory.begin() as session:
        session.add(
            Workspace(
                id=workspace_id,
                slug="split-reclaim",
                title="Control Workspace",
                name="Control Workspace",
            )
        )

    provider = SplitProvider(control_factory, data_factory)
    worker = OneShotWorker(database_provider=provider)

    reclaimed_count = worker.reclaim_stale_jobs_with_session_factory(
        session_factory=control_factory,
        lease_timeout_seconds=600,
    )

    assert reclaimed_count == 1
    with control_factory() as session:
        assert session.get(Job, job_id) is None
    with data_factory() as session:
        stored = session.get(Job, job_id)
        assert stored is not None
        assert stored.status == "queued"
        assert stored.retry_count == 1


def test_split_provider_persists_trace_and_conversation_in_data_db(tmp_path):
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'trace-control.sqlite'}"
    )
    data_engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'trace-data.sqlite'}")
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)
    with data_factory.begin() as session:
        workspace_id = seed_default_workspace(
            session,
            slug="trace-split",
            title="Trace data workspace",
        ).id
    with control_factory.begin() as session:
        session.add(
            Workspace(
                id=workspace_id,
                slug="trace-split",
                title="Trace control workspace",
                name="Trace control workspace",
            )
        )

    provider = SplitProvider(control_factory, data_factory)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
    )
    container = assemble(settings, db=provider)
    actor = _persist_pat_actor(
        (control_factory, data_factory),
        workspace_roles={workspace_id: "owner"},
        default_workspace_id=workspace_id,
        scopes=("mcp:read", "mcp:write"),
        label="trace-data-client",
    )
    page = PageService(data_factory).create_page_first(
        workspace_slug="trace-split",
        title="Trace page only",
        body_md="# Trace page only\n\nConversation routing witness.",
        actor_kind="human",
        actor_id=actor.actor_id,
        actor_provenance=actor.provenance,
    )
    container.services.__dict__["workspace_directory"] = SplitWorkspaceDirectory(
        workspace_id=workspace_id,
        page_ids=(page.id,),
    )
    ctx = ToolCallContext(
        actor=actor,
        db=provider,
        page_service=PageService(data_factory),
        services=container.get_services(),
        workspace_id=workspace_id,
    )

    async def run(tool_name="guide", token=None, arguments=None):
        arguments = dict(arguments or {})
        if tool_name == "guide":
            arguments["workspace_id"] = str(workspace_id)
        if token is not None:
            arguments["conversation_token"] = token
        return await call_tool_async(
            tool_name,
            arguments,
            actor=actor,
            page_service=ctx.page_service,
            session_factory=control_factory,
            tool_context=ctx,
        )

    first = anyio.run(run)
    token = first.structuredContent["envelope"]["conversation_token"]
    second = anyio.run(run, "guide", token)
    workspace_less = anyio.run(run, "list_my_workspaces", token)
    # upload_image is a page-scoped write (unlike read_page, its schema
    # always requires workspace_id -- resolving from page_id alone is no
    # longer reachable through any registered tool's own input contract,
    # only through the lower-level resolve_workspace_id_for_tool helper
    # tested directly in test_mcp_router.py). Exercise the still-live
    # page_id + workspace_id identifier-consistency check instead. `page`
    # is fresh from create_page_first and untouched, so it is still
    # "editing" -- no live path reopens that status once a save lands.
    page_only = anyio.run(
        run,
        "upload_image",
        token,
        {
            "workspace_id": str(workspace_id),
            "page_id": str(page.id),
            "editing_version_id": str(page.version_id),
            "filename": "page-only.png",
            "mime_type": "image/png",
            "content_base64": base64.b64encode(PNG_BYTES).decode("ascii"),
            "marker_uuid": str(uuid4()),
        },
    )

    assert second.isError is False
    assert second.structuredContent["envelope"]["conversation_token"] == token
    assert workspace_less.isError is False
    assert workspace_less.structuredContent["envelope"]["conversation_token"] == token
    assert page_only.isError is False
    assert page_only.structuredContent["envelope"]["conversation_token"] == token
    assert page_only.structuredContent["data"]["asset_id"]
    with control_factory() as session:
        assert session.scalar(select(MCPToolCall)) is None
        assert session.scalar(select(McpChatSession)) is None
    with data_factory() as session:
        assert len(session.scalars(select(MCPToolCall)).all()) == 4
        chats = session.scalars(select(McpChatSession)).all()
        assert len(chats) == 1
        assert chats[0].mcp_session_id == token


def _seed_split_disabled_oauth_client(control_factory, data_factory, *, slug):
    """Seed a workspace whose owner's OAuth grant is disabled ONLY on the
    workspace (data) plane -- exactly the reviewer's reproduction: the
    disable write lands via WorkspaceSessionFactoryDep
    (update_settings_mcp_client), so the control DB never sees the
    ``MCPClient`` row at all, let alone its ``status``.
    """
    grant_id = uuid4()
    user_id = uuid4()
    with data_factory.begin() as session:
        workspace = seed_default_workspace(session, slug=slug, title=f"{slug} data")
        workspace_id = workspace.id
        # MCPClient.issued_to_user_id FKs to users.id; mirror the
        # control-plane principal into the data DB too (same pattern as
        # test_split_provider_persists_trace_and_conversation_in_data_db).
        session.add(
            User(
                id=user_id,
                email=f"{slug}@example.com",
                password_hash="unused",
                display_name="Split disable owner",
            )
        )
        session.add(
            MCPClient(
                workspace_id=workspace_id,
                name="Split-plane disabled OAuth grant",
                client_type="oauth",
                scopes=["mcp:read"],
                status="disabled",
                issued_to_user_id=user_id,
                runtime_binding_key=f"oauth_grant:{grant_id}",
                token_metadata={"oauth_client_id": "split-disable-client"},
            )
        )
        # P10.3 GAP-2: require_workspace_role's grant-replica intersection
        # (D-01v3-b) now reads workspace_credential_grant off this SAME
        # workspace-plane session for any oauth_jwt/pat actor, so this actor
        # needs a live replica here -- a manually-constructed actor never
        # went through the real issuance+fan-out path (T4.5) that would
        # normally have created one. Also seeded into control_factory below:
        # this fixture's SECOND assertion (the "revert-fail companion")
        # deliberately points workspace_session_factory at the WRONG
        # (control) plane to isolate the MCPClient disable-check's own
        # plane-sensitivity -- it must not ALSO start failing on a missing
        # grant replica for an unrelated reason, or that assertion stops
        # demonstrating what it says it demonstrates.
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=workspace_id,
                credential_kind="oauth",
                credential_id=grant_id,
                user_id=user_id,
                granted_scopes=["mcp:read"],
                synced_revision=1,
            )
        )
    with control_factory.begin() as session:
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                title=f"{slug} control",
                name=f"{slug} control",
            )
        )
        session.add(
            User(
                id=user_id,
                email=f"{slug}@example.com",
                password_hash="unused",
                display_name="Split disable owner",
            )
        )
        session.flush()
        session.add(
            WorkspaceMembership(
                user_id=user_id, workspace_id=workspace_id, role="owner"
            )
        )
        session.add(
            OAuthClient(
                id="split-disable-client",
                client_name="Split-plane disabled OAuth grant",
                redirect_uris=["http://127.0.0.1/callback"],
                grant_types=["authorization_code"],
                response_types=["code"],
                scope="mcp:read",
            )
        )
        session.add(
            OAuthConsent(
                user_id=user_id,
                client_id="split-disable-client",
                workspace_id=workspace_id,
                scope="mcp:read",
                desired_scopes=["mcp:read"],
            )
        )
        # See the matching comment in the data_factory block above: seeded
        # here too purely so the "revert-fail companion" sub-test (which
        # deliberately reads the WRONG plane) still finds a live grant
        # replica and keeps failing/succeeding for the ONE reason it is
        # actually testing.
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=workspace_id,
                credential_kind="oauth",
                credential_id=grant_id,
                user_id=user_id,
                granted_scopes=["mcp:read"],
                synced_revision=1,
            )
        )
    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        scopes=("mcp:read", "guide", "workspace-read"),
        workspace_role="owner",
        user_id=user_id,
        workspace_id=workspace_id,
        role="owner",
        token_kind="oauth_jwt",
        token_id=grant_id,
        client_id="split-disable-client",
        token_workspace_ids=(workspace_id,),
        provenance="oauth_client",
    )
    return workspace_id, actor


def test_split_provider_mcp_disable_check_reads_the_workspace_plane_not_control(
    tmp_path,
):
    """MCP-01 plane fix: require_workspace_role's disabled-connection check
    must read MCPClient off the TARGET WORKSPACE's own plane, not whatever
    session backs the identity-plane membership check.

    Sabotage check: on the pre-fix code (the disabled-check reusing the
    caller's ``session`` argument), this test fails -- the ``session`` passed
    here is the CONTROL session, which was never given an MCPClient row (see
    ``_seed_split_disabled_oauth_client``: the disabled row exists only in
    ``data_factory``). A control-only read would find no matching row and
    return "not disabled", so ``require_workspace_role`` would return
    ``role="owner"`` instead of raising. Only reading through
    ``workspace_session_factory`` (bound to the DATA factory here) sees the
    disabled row and raises.
    """
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'disable-control.sqlite'}"
    )
    data_engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'disable-data.sqlite'}")
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    workspace_id, actor = _seed_split_disabled_oauth_client(
        control_factory, data_factory, slug="split-disable-rbac"
    )
    provider = SplitProvider(control_factory, data_factory)

    with control_factory() as session:
        with pytest.raises(
            RejectedByPolicyError, match="disabled for this workspace"
        ) as excinfo:
            require_workspace_role(
                session=session,
                actor=actor,
                workspace_id=workspace_id,
                required_role="viewer",
                workspace_session_factory=provider.session_for(workspace_id),
            )
        assert excinfo.value.error_code == "mcp_connection_disabled"

    # Revert-fail companion: pointing workspace_session_factory at the
    # CONTROL factory (the pre-fix shape) must NOT see the disable -- proving
    # the assertion above is actually exercising plane routing, not just
    # "disabled rows are honored somewhere."
    with control_factory() as session:
        access = require_workspace_role(
            session=session,
            actor=actor,
            workspace_id=workspace_id,
            required_role="viewer",
            workspace_session_factory=control_factory,
        )
    assert access.role == "owner"


def test_split_provider_resolve_target_actor_denies_disabled_mcp_client(tmp_path):
    """Second caller of the same seam (mcp.router.resolve_target_actor), not
    the context_media_access.py path the reviewer reproduced against --
    pins the fix as a property of the shared require_workspace_role seam,
    not of one endpoint. Routes a non-media MCP tool ("guide") through the
    real dispatcher entry point with a DatabaseProvider that actually
    shards, the same as production `dispatch()` passes `tool_context.db`.
    """
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'disable-router-control.sqlite'}"
    )
    data_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'disable-router-data.sqlite'}"
    )
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    workspace_id, actor = _seed_split_disabled_oauth_client(
        control_factory, data_factory, slug="split-disable-router"
    )
    provider = SplitProvider(control_factory, data_factory)

    result = resolve_target_actor(
        tool_name="guide",
        payload={"workspace_id": str(workspace_id)},
        actor=actor,
        session_factory=control_factory,
        db=provider,
    )

    assert isinstance(result, PolicyRejection)
    assert result.error_code == "mcp_connection_disabled"


def test_mcp_error_traces_use_requested_workspace_not_actor_default(
    tmp_path,
    monkeypatch,
):
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'error-trace-control.sqlite'}"
    )
    data_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'error-trace-data.sqlite'}"
    )
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)
    with data_factory.begin() as session:
        actor_workspace_id = seed_default_workspace(
            session,
            slug="actor-default",
            title="Actor default workspace",
        ).id
        target_workspace_id = seed_default_workspace(
            session,
            slug="requested-target",
            title="Requested target workspace",
        ).id
        unauthorized_workspace_id = seed_default_workspace(
            session,
            slug="unauthorized-target",
            title="Unauthorized target workspace",
        ).id
    with control_factory.begin() as session:
        session.add_all(
            [
                Workspace(
                    id=actor_workspace_id,
                    slug="actor-default",
                    title="Actor default workspace",
                    name="Actor default workspace",
                ),
                Workspace(
                    id=target_workspace_id,
                    slug="requested-target",
                    title="Requested target workspace",
                    name="Requested target workspace",
                ),
                Workspace(
                    id=unauthorized_workspace_id,
                    slug="unauthorized-target",
                    title="Unauthorized target workspace",
                    name="Unauthorized target workspace",
                ),
            ]
        )
    provider = SplitProvider(control_factory, data_factory)
    actor = _persist_pat_actor(
        (control_factory, data_factory),
        workspace_roles={
            actor_workspace_id: "owner",
            target_workspace_id: "viewer",
        },
        default_workspace_id=actor_workspace_id,
        scopes=("mcp:read",),
        label="trace-human",
    )
    page_service = PageService(data_factory)
    ctx = ToolCallContext(
        actor=actor,
        db=provider,
        page_service=page_service,
        services=SimpleNamespace(workspace_directory=None),
        workspace_id=target_workspace_id,
    )

    def _dispatch_boom(*args, **kwargs):
        raise RuntimeError("handler exploded")

    monkeypatch.setattr("backend.mcp.protocol.runner.dispatch", _dispatch_boom)

    async def invoke(conversation_token=None, workspace_id=target_workspace_id):
        arguments = {"workspace_id": str(workspace_id)}
        if conversation_token is not None:
            arguments["conversation_token"] = conversation_token
        return await call_tool_async(
            "guide",
            arguments,
            actor=actor,
            page_service=page_service,
            session_factory=control_factory,
            tool_context=ctx,
        )

    malformed = anyio.run(invoke, 123)
    handler_failure = anyio.run(invoke)
    unauthorized = anyio.run(invoke, 123, unauthorized_workspace_id)

    # workspace_id is audit/telemetry, not a field a model acts on: it stays
    # off the wire (see runner.py _slim_envelope_for_wire) and is asserted on
    # the trace rows below.
    for result in (malformed, handler_failure, unauthorized):
        assert result.isError is True
        assert "workspace_id" not in result.structuredContent["envelope"]
        assert "meta" not in result.structuredContent["envelope"]
    with control_factory() as session:
        assert session.scalar(select(MCPToolCall)) is None
    with data_factory() as session:
        rows = session.scalars(
            select(MCPToolCall).order_by(MCPToolCall.started_at)
        ).all()
        assert len(rows) == 3
        assert [row.workspace_id for row in rows].count(target_workspace_id) == 2
        assert [row.workspace_id for row in rows].count(actor_workspace_id) == 1
        assert unauthorized_workspace_id not in {row.workspace_id for row in rows}
        assert {row.error_code for row in rows} == {
            "conversation_token_malformed",
            "internal_error",
        }
    assert provider.session_for_calls.count(target_workspace_id) >= 4
    assert unauthorized_workspace_id not in provider.session_for_calls


def test_conversation_token_missing_workspace_never_uses_injected_directory(tmp_path):
    """A malformed current-schema call is opaque to split-plane directory data."""
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'dir-control.sqlite'}"
    )
    Base.metadata.create_all(control_engine)
    control_factory = make_session_factory(control_engine)

    workspace_id = uuid4()
    existing_page_id = uuid4()
    absent_page_id = uuid4()

    class RecordingDirectory(SplitWorkspaceDirectory):
        def __init__(self):
            super().__init__(
                workspace_id=workspace_id,
                page_ids=(existing_page_id,),
            )
            self.calls: list[tuple[str, object]] = []

        def workspace_id_by_context_page(self, session, page_id):
            self.calls.append(("page", page_id))
            return super().workspace_id_by_context_page(session, page_id)

        def workspace_id_by_slug(self, session, workspace_slug):
            self.calls.append(("slug", workspace_slug))
            return super().workspace_id_by_slug(session, workspace_slug)

    directory = RecordingDirectory()
    provider = SplitProvider(control_factory, control_factory)
    page_service = PageService(control_factory)
    actor = ActorContext(
        actor_kind="mcp_client",
        actor_id="missing-workspace-cloud-test",
        token_kind="mcp_client",
        scopes=("draft-write",),
    )
    ctx = ToolCallContext(
        actor=actor,
        db=provider,
        page_service=page_service,
        services=SimpleNamespace(workspace_directory=directory),
    )

    async def invoke(page_id):
        return await call_tool_async(
            "upload_image",
            {
                "page_id": str(page_id),
                "workspace_slug": "split",
                "conversation_token": "new",
            },
            actor=actor,
            page_service=page_service,
            session_factory=control_factory,
            tool_context=ctx,
        )

    existing = anyio.run(invoke, existing_page_id)
    absent = anyio.run(invoke, absent_page_id)

    for result in (existing, absent):
        assert result.isError is True
        assert result.structuredContent["envelope"]["result_status"] == "failed"
        assert result.structuredContent["envelope"]["error_code"] == "validation_failed"
    assert directory.calls == []


def test_split_provider_routes_rest_workspace_reads_to_data_db(tmp_path):
    """R5' web hot path (Sprint-3 C1-T7): REST /workspaces/{id}/* opens its
    session via DatabaseProvider.session_for(workspace_id) — auth stays on
    the control plane, domain reads hit the workspace data plane."""

    from backend.models.user import User
    from backend.models.workspace import WorkspaceMembership
    from backend.tests.conftest import authenticate_test_client

    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'rest-control.sqlite'}"
    )
    data_engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'rest-data.sqlite'}")
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    with data_factory.begin() as session:
        data_workspace = seed_default_workspace(
            session, slug="split", title="Data Workspace"
        )
        workspace_id = data_workspace.id
        sensitive_agent = session.scalar(
            select(AgentPage).where(
                AgentPage.workspace_id == workspace_id,
                AgentPage.category == "agent_guide",
            )
        )
        assert sensitive_agent is not None
        sensitive_agent_id = sensitive_agent.id
        sensitive_active_version_id = sensitive_agent.current_active_version_id
        assert sensitive_active_version_id is not None
    data_page = PageService(data_factory).write_page(
        workspace_slug="split",
        slug="rest-data-page",
        title="REST Data Only Page",
        body_md="# REST Data Only Page\n\nData plane body.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000011",
        actor_provenance="browser_session",
    )
    with control_factory.begin() as session:
        # control plane mirrors workspace identity but holds no domain rows
        session.add(
            Workspace(
                id=workspace_id,
                slug="split",
                title="Control Workspace",
                name="Control Workspace",
            )
        )

    provider = SplitProvider(control_factory, data_factory)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
    )
    container = assemble(
        settings,
        db=provider,
        directory=SplitWorkspaceDirectory(
            workspace_id=workspace_id,
            page_ids=(data_page.id,),
        ),
    )
    app = create_app(
        settings=settings,
        page_service=PageService(control_factory),
        container=container,
        bootstrap_on_startup=False,
    )
    client = TestClient(app)
    authenticate_test_client(client, email="split-rest@example.com")
    with control_factory.begin() as session:
        user_id = session.scalar(
            select(User.id).where(User.email == "split-rest@example.com")
        )
        session.add(
            WorkspaceMembership(
                user_id=user_id, workspace_id=workspace_id, role="owner"
            )
        )

    from backend.schemas.context_write import (
        ReviewDecisionRequest,
        WriteDraftRequest,
    )
    from backend.services.context_write_service import ContextWriteService
    from backend.services.review_service import ReviewService

    with data_factory.begin() as session:
        draft = ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Control role review",
                page_type="overview",
                body_markdown="# Control role review",
                reason="split role regression",
            ),
            actor_kind="human",
            actor_id=str(user_id),
            actor_role="owner",
        )
        ReviewService().apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=draft.review_item_id,
            request=ReviewDecisionRequest(
                decision_type="request_changes",
                decision_reason="Revise this",
                audience_kind="workspace_editors",
            ),
            actor_kind="human",
            actor_id=str(user_id),
            actor_user_id=user_id,
            actor_role="owner",
            actor_provenance="browser_session",
        )

    provider.session_for_calls.clear()
    tree = client.get(f"/api/v1/workspaces/{workspace_id}/tree?section=context")
    assert tree.status_code == 200, tree.text
    titles = {node["title"] for node in tree.json()["data"]["nodes"]}
    # the page exists only in the data DB — serving it proves session_for
    assert "REST Data Only Page" in titles

    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    page_list = client.get(pages_url)
    assert page_list.status_code == 200, page_list.text
    assert str(data_page.id) in {page["id"] for page in page_list.json()["data"]}
    page_detail = client.get(f"{pages_url}/{data_page.id}")
    assert page_detail.status_code == 200, page_detail.text
    # The directory finds this id without a control-plane page row; its
    # location still requires the target's live control-plane membership.
    location_url = f"/api/v1/context/pages/{data_page.id}/location"
    page_location = client.get(location_url)
    assert page_location.status_code == 200, page_location.text
    assert page_location.json()["data"] == {
        "workspace_id": str(workspace_id),
        "page_id": str(data_page.id),
    }
    created = client.post(
        pages_url,
        json={"title": "Legacy data write", "body_md": "# Legacy data write"},
    )
    assert created.status_code == 201, created.text
    created_id = UUID(created.json()["data"]["id"])
    with control_factory() as session:
        assert session.get(ContextPage, created_id) is None
    with data_factory() as session:
        assert session.get(ContextPage, created_id) is not None

    tree_write = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={
            "section": "context",
            "title": "Control authorized folder",
            "slug": "control-authorized-folder",
        },
    )
    assert tree_write.status_code == 201, tree_write.text
    mcp_chats = client.get(f"/api/v1/workspaces/{workspace_id}/mcp-chats")
    assert mcp_chats.status_code == 200, mcp_chats.text
    reviews = client.get(
        f"/api/v1/workspaces/{workspace_id}/reviews",
        params={"scope": "to_me", "status": "changes_requested"},
    )
    assert reviews.status_code == 200, reviews.text
    assert [item["id"] for item in reviews.json()["data"]["items"]] == [
        str(draft.review_item_id)
    ]

    sensitive_draft = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{sensitive_agent_id}/draft",
        json={
            "category": "agent_guide",
            "title": "Default System Prompt",
            "slug": "default-system-prompt",
            "body_markdown": "# Split sensitive draft\n\nOwner only.",
            "base_version_id": str(sensitive_active_version_id),
            "reason": "exercise sensitive split-provider reads",
        },
    )
    assert sensitive_draft.status_code == 200, sensitive_draft.text
    sensitive_draft_version_id = sensitive_draft.json()["data"]["draft_version_id"]
    sensitive_read = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/"
        f"{sensitive_agent_id}?version=draft"
    )
    assert sensitive_read.status_code == 200, sensitive_read.text
    sensitive_diff = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{sensitive_agent_id}/diff",
        params={
            "from_version_id": str(sensitive_active_version_id),
            "to_version_id": sensitive_draft_version_id,
        },
    )
    assert sensitive_diff.status_code == 200, sensitive_diff.text

    with control_factory.begin() as session:
        membership = session.get(WorkspaceMembership, (user_id, workspace_id))
        membership.role = "viewer"
    denied_tree_write = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={
            "section": "context",
            "title": "Viewer denied folder",
            "slug": "viewer-denied-folder",
        },
    )
    assert denied_tree_write.status_code == 403
    assert denied_tree_write.json()["error_code"] == "workspace_role_insufficient"
    denied_sensitive_read = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/"
        f"{sensitive_agent_id}?version=draft"
    )
    assert denied_sensitive_read.status_code == 403
    denied_sensitive_diff = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{sensitive_agent_id}/diff",
        params={
            "from_version_id": str(sensitive_active_version_id),
            "to_version_id": sensitive_draft_version_id,
        },
    )
    assert denied_sensitive_diff.status_code == 403
    assert workspace_id in provider.session_for_calls

    with control_factory.begin() as session:
        membership = session.get(WorkspaceMembership, (user_id, workspace_id))
        assert membership is not None
        session.delete(membership)
    denied_location = client.get(location_url)
    assert denied_location.status_code == 404, denied_location.text
    assert denied_location.json()["error_code"] == "not_found"
    assert denied_location.json()["message"] == "page not found"


def _split_upload_surface(tmp_path):
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'upload-control.sqlite'}"
    )
    data_engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'upload-data.sqlite'}")
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    with control_factory.begin() as session:
        control_workspace = seed_default_workspace(
            session,
            slug="split",
            title="Split Upload Control",
        )
        workspace_id = control_workspace.id
    with data_factory.begin() as session:
        session.add(
            Workspace(
                id=workspace_id,
                slug="split",
                title="Split Upload Data",
                name="Split Upload Data",
            )
        )
        session.flush()
        seed_default_workspace(session, slug="split", title="Split Upload Data")
    # ContextPage is a workspace-plane domain table. Media authorization
    # reads it via session_for(workspace_id), so it
    # must live in the data DB, not the control DB.
    page = PageService(data_factory).write_page(
        workspace_slug="split",
        slug="split-media-page",
        title="Split Media Page",
        body_md="# Split Media Page",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000021",
        actor_provenance="browser_session",
    )
    # Upload registration is routed to session_for(workspace_id): the media session,
    # its assets, and their blobs land in the same data DB as the page they
    # are claimed against, so no control-DB mirror of the page is needed
    # anymore -- ContextMediaSession's FK to ContextPage now resolves within
    # one database.

    provider = SplitProvider(control_factory, data_factory)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="split-upload-test-secret",
        library_blob_root=str(tmp_path / "split-upload-blobs"),
    )
    directory = SplitWorkspaceDirectory(
        workspace_id=workspace_id,
        page_ids=(page.id,),
    )
    container = assemble(
        settings,
        db=provider,
        directory=directory,
    )
    app = create_app(
        settings=settings,
        page_service=PageService(control_factory),
        container=container,
        bootstrap_on_startup=False,
    )
    client = TestClient(app)
    email = "split-upload@example.com"
    authenticate_test_client(client, email=email)
    with control_factory.begin() as session:
        user_id = session.scalar(select(User.id).where(User.email == email))
        assert user_id is not None
        session.add(
            WorkspaceMembership(
                user_id=user_id,
                workspace_id=workspace_id,
                role="owner",
            )
        )
    # A second, page-first page owned by the real authenticated user: no live
    # path reopens a fresh "editing" session on an already-existing page (the
    # deleted begin_editing bridge had no real caller), so a test that needs
    # one must get it from page-first creation directly rather than from
    # `page` above -- and it must be owned by whichever actor will upload
    # against it (the editing-version opacity check in
    # context_media/adapters/sqlalchemy.py requires an exact actor match).
    editing_page = PageService(data_factory).create_page_first(
        workspace_slug="split",
        actor_kind="human",
        actor_id=str(user_id),
        actor_provenance="browser_session",
    )
    directory.page_ids.add(editing_page.id)
    return SimpleNamespace(
        client=client,
        control_factory=control_factory,
        data_factory=data_factory,
        provider=provider,
        workspace_id=workspace_id,
        page_id=page.id,
        editing_page_id=editing_page.id,
        editing_version_id=editing_page.version_id,
        editing_content_hash=editing_page.content_hash,
    )


def test_split_provider_library_upload_installs_on_workspace_data_db(tmp_path):
    surface = _split_upload_surface(tmp_path)
    surface.provider.session_for_calls.clear()

    response = surface.client.post(
        f"/api/v1/workspaces/{surface.workspace_id}/library/items",
        files={
            "file": (
                "split.json",
                b'{"workspace_plane":"data"}',
                "application/json",
            )
        },
    )

    assert response.status_code == 201, response.text
    with surface.data_factory() as session:
        version = session.scalar(
            select(LibraryItemVersion).where(
                LibraryItemVersion.source_blob_workspace_id == surface.workspace_id
            )
        )
        blob = session.scalar(
            select(StoredBlobRow).where(
                StoredBlobRow.workspace_id == surface.workspace_id
            )
        )
        install = session.scalar(
            select(MediaInstallRequest).where(
                MediaInstallRequest.workspace_id == surface.workspace_id
            )
        )
    assert version is not None
    assert blob is not None
    assert install is not None and install.state == "available"
    with surface.control_factory() as session:
        assert (
            session.scalar(
                select(LibraryItemVersion).where(
                    LibraryItemVersion.source_blob_workspace_id == surface.workspace_id
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(StoredBlobRow).where(
                    StoredBlobRow.workspace_id == surface.workspace_id
                )
            )
            is None
        )
    assert surface.workspace_id in surface.provider.session_for_calls


def test_split_provider_context_media_lifecycle_state_control_auth_workspace(
    tmp_path,
):
    """Media authorization and lifecycle state use the same workspace database.

    The control-plane directory resolves the workspace; page-bound uploads,
    media access checks and lifecycle writes use ``session_for(workspace_id)``.
    """
    surface = _split_upload_surface(tmp_path)
    surface.provider.session_for_calls.clear()
    first_marker = uuid4()
    second_marker = uuid4()

    # surface.editing_page_id is fresh page-first, untouched, still
    # "editing" -- no live path reopens that status once a save lands.
    version_id = str(surface.editing_version_id)

    first_response = surface.client.post(
        f"/api/v1/workspaces/{surface.workspace_id}/context/pages/{surface.editing_page_id}/media",
        data={"marker_uuid": str(first_marker), "editing_version_id": version_id},
        files={"file": ("first.png", PNG_BYTES, "image/png")},
    )
    second_response = surface.client.post(
        f"/api/v1/workspaces/{surface.workspace_id}/context/pages/{surface.editing_page_id}/media",
        data={"marker_uuid": str(second_marker), "editing_version_id": version_id},
        files={"file": ("second.png", PNG_BYTES, "image/png")},
    )

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text
    listed = surface.client.get(
        f"/api/v1/workspaces/{surface.workspace_id}/context/pages/{surface.editing_page_id}/media",
        params={"usage": "unused"},
    )
    assert listed.status_code == 200, listed.text
    assert {item["id"] for item in listed.json()["data"]} == {
        str(first_marker),
        str(second_marker),
    }
    usage_token = next(
        item["usage_token"]
        for item in listed.json()["data"]
        if item["id"] == str(first_marker)
    )

    first_read = surface.client.get(
        f"/api/v1/workspaces/{surface.workspace_id}/context/pages/{surface.editing_page_id}/versions/{version_id}/media/"
        f"{first_marker}/content"
    )
    second_read = surface.client.get(
        f"/api/v1/workspaces/{surface.workspace_id}/context/pages/{surface.editing_page_id}/versions/{version_id}/media/"
        f"{second_marker}/content"
    )
    assert first_read.status_code == 200 and first_read.content == PNG_BYTES
    assert second_read.status_code == 200 and second_read.content == PNG_BYTES

    deleted = surface.client.delete(
        f"/api/v1/workspaces/{surface.workspace_id}/context/media/assets/"
        f"{first_marker}",
        headers={"If-Match": usage_token},
    )
    assert deleted.status_code == 200, deleted.text

    with surface.data_factory() as session:
        assets = session.scalars(
            select(ContextMediaAsset).where(
                ContextMediaAsset.workspace_id == surface.workspace_id
            )
        ).all()
        blob = session.scalar(
            select(StoredBlobRow).where(
                StoredBlobRow.workspace_id == surface.workspace_id
            )
        )
    assert {asset.id for asset in assets} == {first_marker, second_marker}
    assert {asset.id: asset.association_state for asset in assets} == {
        first_marker: "tombstoned",
        second_marker: "active",
    }
    assert blob is not None
    with surface.control_factory() as session:
        assert (
            session.scalar(
                select(ContextMediaAsset).where(
                    ContextMediaAsset.workspace_id == surface.workspace_id
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(StoredBlobRow).where(
                    StoredBlobRow.workspace_id == surface.workspace_id
                )
            )
            is None
        )
    # Revert-fail: reverting the D-7 workspace-routed factory back to a
    # single control-plane session_factory would make this list empty.
    assert surface.workspace_id in surface.provider.session_for_calls
    assert set(surface.provider.session_for_calls) <= {surface.workspace_id}


def test_split_provider_resolve_public_context_media_scope_reads_workspace_plane(
    tmp_path,
):
    """resolve_public_context_media_scope's single JOIN across Workspace
    (identity-plane) and ContextPage/ContextPageVersion (workspace-plane)
    cannot survive a plane split -- Workspace lives only in control_factory
    here, ContextPage/ContextPageVersion only in data_factory. Reproduces
    the reviewer's finding directly against the function the fix changed,
    the same way the F1 require_workspace_role test above does.
    """
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'public-scope-control.sqlite'}"
    )
    data_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'public-scope-data.sqlite'}"
    )
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    with data_factory.begin() as session:
        data_workspace = seed_default_workspace(
            session, slug="split-scope", title="Split Scope Data"
        )
        workspace_id = data_workspace.id
    page = PageService(data_factory).write_page(
        workspace_slug="split-scope",
        slug="split-scope-page",
        title="Split Scope Page",
        body_md="# Split Scope Page\n\nOnly the workspace DB has this.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000041",
        actor_provenance="browser_session",
    )
    with data_factory.begin() as session:
        page_row = session.get(ContextPage, page.id)
        grant_public_page_exposure(
            page_row,
            scope="unlisted_web",
            version_id=page_row.current_published_version_id,
        )
        exposed_version_id = page_row.public_exposed_version_id
    with control_factory.begin() as session:
        session.add(
            Workspace(
                id=workspace_id,
                slug="split-scope",
                title="Split Scope Control",
                name="Split Scope Control",
            )
        )

    provider = SplitProvider(control_factory, data_factory)

    scope = resolve_public_context_media_scope(
        provider, workspace_slug="split-scope", page_slug="split-scope-page"
    )
    assert scope is not None
    assert scope.workspace_id == workspace_id
    assert scope.page_id == page.id
    assert scope.page_version_id == exposed_version_id
    assert scope.exposure_scope == "unlisted_web"

    # Negative: a workspace slug that resolves on the control plane but a
    # page_slug with no matching ContextPage in the data plane must be None,
    # not an error and not the workspace-only partial result.
    assert (
        resolve_public_context_media_scope(
            provider, workspace_slug="split-scope", page_slug="does-not-exist"
        )
        is None
    )


def test_split_provider_public_llms_txt_lists_only_workspace_plane_exposed_pages(
    tmp_path,
):
    """backend/api/public.py:read_public_workspace_llms_txt ->
    render_scoped_llms_txt must keep working once ContextPage/
    ContextPageVersion move to the workspace's own database. Pre-fix, this
    read ContextPage/ContextPageVersion on the control session; once split,
    control holds none, so the page list would silently render EMPTY
    instead of 404ing or erroring -- the reviewer's exact finding. The
    hidden (non-exposed) page pins that the split read still filters
    correctly, not just that the endpoint responds.
    """
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'public-llms-control.sqlite'}"
    )
    data_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'public-llms-data.sqlite'}"
    )
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    with data_factory.begin() as session:
        data_workspace = seed_default_workspace(
            session, slug="split-llms", title="Split LLMS Data"
        )
        workspace_id = data_workspace.id
    exposed = PageService(data_factory).write_page(
        workspace_slug="split-llms",
        slug="split-llms-exposed",
        title="Split LLMS Exposed",
        body_md="# Split LLMS Exposed\n\nVisible.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000051",
        actor_provenance="browser_session",
    )
    hidden = PageService(data_factory).write_page(
        workspace_slug="split-llms",
        slug="split-llms-hidden",
        title="Split LLMS Hidden",
        body_md="# Split LLMS Hidden\n\nNever indexed.",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000051",
        actor_provenance="browser_session",
    )
    with data_factory.begin() as session:
        page_row = session.get(ContextPage, exposed.id)
        grant_public_page_exposure(
            page_row,
            scope="indexed_web",
            version_id=page_row.current_published_version_id,
        )
    with control_factory.begin() as session:
        session.add(
            Workspace(
                id=workspace_id,
                slug="split-llms",
                title="Split LLMS Control",
                name="Split LLMS Control",
                visibility="public",
            )
        )

    provider = SplitProvider(control_factory, data_factory)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="split-llms-secret",
    )
    container = assemble(
        settings,
        db=provider,
        directory=SplitWorkspaceDirectory(
            workspace_id=workspace_id, page_ids=(exposed.id, hidden.id)
        ),
    )
    app = create_app(
        settings=settings,
        page_service=PageService(control_factory),
        container=container,
        bootstrap_on_startup=False,
    )
    client = TestClient(app)

    response = client.get("/api/v1/public/w/split-llms/llms.txt")
    assert response.status_code == 200, response.text
    assert "Split LLMS Exposed" in response.text
    assert "Split LLMS Hidden" not in response.text
    assert "/api/v1/public/w/split-llms/pages/split-llms-exposed" in response.text

    # Negative: a workspace that is NOT public must still 404 -- even though
    # its ContextPage rows and public grant live in the (reachable) data
    # plane, the control-plane visibility gate must still be the one that
    # decides.
    with control_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        workspace.visibility = "team"
    not_public = client.get("/api/v1/public/w/split-llms/llms.txt")
    assert not_public.status_code == 404


def test_split_provider_public_context_media_serves_from_workspace_plane(tmp_path):
    """backend/api/public.py:read_public_context_media ->
    resolve_public_context_media_scope must keep serving bytes once
    ContextPage/ContextPageVersion move to the workspace's own database --
    the reviewer's second reproduction of the same finding, through the
    actual byte-serving HTTP path (not just the resolver function tested
    above). Negative first: before any public grant exists, the identical
    request must 404, not leak the asset.
    """
    surface = _split_upload_surface(tmp_path)

    # surface.editing_page_id is a page-first page, already registered in
    # SplitWorkspaceDirectory and owned by the authenticated surface.client
    # actor -- fresh, untouched, still "editing". No live path reopens that
    # status once a save lands (the deleted begin_editing bridge had no real
    # caller), so this must be the page-first page, not surface.page_id.
    page = {
        "id": str(surface.editing_page_id),
        "version_id": str(surface.editing_version_id),
    }

    uploaded = surface.client.post(
        f"/api/v1/workspaces/{surface.workspace_id}/context/pages/{page['id']}/media",
        data={
            "marker_uuid": str(uuid4()),
            "editing_version_id": page["version_id"],
        },
        files={"file": ("public-split.png", PNG_BYTES, "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    asset = uploaded.json()["data"]

    body_md = f"# Split Media Page\n\n![diagram]({asset['logical_ref']})"
    # P8.1: an "editing" row stays "editing" through any number of
    # autosaves (author-private) -- an explicit submit-for-review call is
    # required before publish is reachable.
    saved = surface.client.post(
        f"/api/v1/workspaces/{surface.workspace_id}/context/pages/{page['id']}/autosave",
        json={
            "idempotency_key": "t-1",
            "body_md": body_md,
            "base_version_id": page["version_id"],
            "base_content_hash": surface.editing_content_hash,
            # `page` above is hand-built from the fresh, never-saved
            # page-first editing row (see the comment on `page = {...}`) --
            # its editing_revision is always the schema's initial value.
            "expected_editing_revision": 1,
        },
    )
    assert saved.status_code == 200, saved.text
    saved_data = saved.json()["data"]
    submitted = surface.client.post(
        f"/api/v1/workspaces/{surface.workspace_id}/context/pages/{page['id']}/submit-for-review",
        json={
            "idempotency_key": "t-2",
            "version_id": saved_data["version_id"],
            "expected_editing_revision": saved_data["editing_revision"],
        },
    )
    assert submitted.status_code == 200, submitted.text
    saved_data = submitted.json()["data"]
    if saved_data.get("review_item_id") is not None:
        decision = surface.client.post(
            f"/api/v1/workspaces/{surface.workspace_id}/reviews/"
            f"{saved_data['review_item_id']}/decision",
            json={"decision_type": "approve"},
        )
        assert decision.status_code == 200, decision.text
    published = surface.client.post(
        f"/api/v1/workspaces/{surface.workspace_id}/context/pages/{page['id']}/versions/{saved_data['version_id']}/publish",
        json={
            "reason": "media integration fixture",
            "publish_despite_findings": False,
            "expected_content_hash": saved_data["content_hash"],
        },
    )
    assert published.status_code == 200, published.text

    anonymous = TestClient(surface.client.app)
    public_url = (
        f"/api/v1/public/w/split/pages/{saved_data['slug']}/media/{asset['asset_id']}"
    )
    assert anonymous.get(public_url).status_code == 404

    with surface.data_factory.begin() as session:
        page_row = session.get(ContextPage, surface.editing_page_id)
        assert page_row.current_published_version_id is not None
        grant_public_page_exposure(
            page_row,
            scope="unlisted_web",
            version_id=page_row.current_published_version_id,
        )

    public_read = anonymous.get(public_url)
    assert public_read.status_code == 200, public_read.text
    assert public_read.content == PNG_BYTES
    assert public_read.headers["X-Robots-Tag"] == "noindex, nofollow"


def test_split_provider_restricted_library_gate_resolves_on_the_workspace_plane(
    tmp_path,
):
    """TEN-01D pin: ``resolve_restricted_library_target`` must resolve against
    the workspace plane, not the control plane.

    ``_restricted_library_policy_error`` (backend/api/mcp_http.py) guards both
    the streamable-http and SSE POST paths. It used to hand the CONTROL session
    to the resolver, which reads LibraryItem / LibraryItemVersion /
    LibraryArtifact -- all workspace-plane. Commit 64592c9d fixed it to resolve
    through ``db.session_for(workspace_id)``.

    This test selects the provider session by hand and calls the resolver
    directly, so it pins resolver behavior against the data and control
    databases only. It does NOT exercise HTTP gate routing: the choice of
    session inside the gate is not covered here. It passes on today's code by
    design. The companion assertion keeps it meaningful: resolving on the
    control session (the pre-fix shape) must find nothing, because the
    restricted item exists only in the data DB.
    """
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'restricted-control.sqlite'}"
    )
    data_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'restricted-data.sqlite'}"
    )
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    slug = "split-restricted-library"
    # The Library rows live ONLY on the data plane, which is what a real shard
    # looks like: the control DB never holds workspace-plane domain rows.
    with data_factory.begin() as session:
        workspace_id = seed_default_workspace(
            session, slug=slug, title=f"{slug} data"
        ).id
        item = LibraryItem(
            workspace_id=workspace_id,
            connector_type="upload",
            format_hint="pdf",
            uri="https://example.com/restricted-split.pdf",
            title="Restricted split-plane source",
            trust_level="restricted",
            lifecycle_status="registered",
        )
        session.add(item)
        session.flush()
        version = LibraryItemVersion(
            workspace_id=workspace_id,
            library_item_id=item.id,
            version_no=1,
            content_hash="split-restricted-h1",
            raw_storage_ref="inline://split-restricted",
        )
        session.add(version)
        session.flush()
        version_id = version.id

    provider = SplitProvider(control_factory, data_factory)

    with provider.session_for(workspace_id)() as session:
        resolved = resolve_restricted_library_target(
            session=session,
            workspace_id=workspace_id,
            target_type="library_item_version",
            target_id=version_id,
        )
    assert resolved == (workspace_id, True), (
        "the workspace plane holds the restricted item, so the gate must see it"
    )

    # Revert-fail companion: the pre-fix shape (control session) must resolve
    # nothing. If this ever returns a tuple, the Library read has drifted back
    # onto the control plane -- or the test fixture stopped being split.
    with provider.control_session_factory()() as session:
        assert (
            resolve_restricted_library_target(
                session=session,
                workspace_id=workspace_id,
                target_type="library_item_version",
                target_id=version_id,
            )
            is None
        ), (
            "the control plane must not be able to resolve workspace-plane "
            "Library rows; a hit here means the gate is reading the wrong plane"
        )


def test_split_provider_consent_cleanup_refuses_to_delete_on_an_unreadable_plane(
    tmp_path,
):
    """TEN-01C: consent cleanup DELETES, and its eligibility test is
    workspace-plane data.

    ``cleanup_user_connections`` decides what to delete from
    ``derive_oauth_connection_state``, and that state depends on
    ``physical_status`` -- whether the consent's MCPClient rows are still
    active. MCPClient is workspace-plane. Read on a control session it is
    simply absent, so ``physical_status`` falls through to "active", the state
    resolves to "expired" on an expired refresh token, and a live connection is
    deleted along with its grant families.

    The disabled client here is the discriminator: on the workspace plane it
    forces "disabled", which is not in {expired, revoked}, so nothing is
    eligible. On the control plane it is invisible and the same consent becomes
    eligible for deletion. The second assertion is the pre-fix behavior, kept
    so this test fails if the fixture ever stops being split.
    """
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'consent-control.sqlite'}"
    )
    data_engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'consent-data.sqlite'}")
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    workspace_id, actor = _seed_split_disabled_oauth_client(
        control_factory, data_factory, slug="split-consent-cleanup"
    )
    user_id = actor.user_id
    client_id = "split-disable-client"

    # The OAuth grant itself is control-plane, and its refresh token is
    # expired -- which is what makes the consent look collectable once the
    # disabled MCPClient is invisible.
    expired = datetime(2026, 1, 1, tzinfo=UTC)
    with control_factory.begin() as session:
        # _seed_split_disabled_oauth_client() already inserted an OAuthClient
        # row with this id; update it in place instead of inserting a second
        # row, which would violate the primary-key uniqueness constraint.
        client = session.get(OAuthClient, client_id)
        client.client_name = "Split cleanup client"
        client.redirect_uris = ["https://client.example/callback"]
        client.scope = "mcp:read"
        client.grant_types = ["authorization_code", "refresh_token"]
        client.response_types = ["code"]
        client.last_used_at = expired
        # Likewise, the helper already inserted the (user, client, workspace)
        # OAuthConsent row -- that triple is its primary key -- so update the
        # existing row's grant timestamp instead of inserting a duplicate.
        consent = session.get(OAuthConsent, (user_id, client_id, workspace_id))
        consent.scope = "mcp:read"
        consent.desired_scopes = ["mcp:read"]
        consent.granted_at = expired

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="split-consent-test-secret",
    )

    routed = OAuthService(
        session_factory=control_factory,
        settings=settings,
        public_base_url="http://testserver",
        database_provider=SplitProvider(control_factory, data_factory),
    )
    preview = routed.cleanup_user_connections(user_id=user_id, dry_run=True)
    assert preview["eligible_consent_count"] == 0, (
        "the workspace plane holds a disabled MCPClient for this consent, so "
        "it is not a dead connection and must not be collected"
    )

    # Revert-fail companion: the pre-fix shape reads MCPClient off the control
    # session, cannot see the disabled row, and marks the consent collectable.
    unrouted = OAuthService(
        session_factory=control_factory,
        settings=settings,
        public_base_url="http://testserver",
        database_provider=DatabaseProvider(None, session_factory=control_factory),
    )
    assert (
        unrouted.cleanup_user_connections(user_id=user_id, dry_run=True)[
            "eligible_consent_count"
        ]
        == 1
    ), (
        "if the control plane also refuses, this fixture is no longer split "
        "and the test cannot distinguish the two planes"
    )


def _seed_split_settings_mcp_fixture(tmp_path, *, slug):
    """One OAuth-backed MCP client whose two halves live in different databases.

    The workspace row, the OAuth client, its consent and its live refresh token
    are control-plane; the ``MCPClient`` row is workspace-plane. Settings has to
    read both to answer a single listing, so this fixture is the smallest shape
    that can tell the two planes apart.
    """
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'settings-mcp-control.sqlite'}"
    )
    data_path = tmp_path / "settings-mcp-data.sqlite"
    data_engine = make_engine(f"sqlite+pysqlite:///{data_path}")
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)
    # A real shard has no control-plane parent rows to point at, so seeding one
    # needs SQLite's foreign keys off -- make_engine turns them ON, which only
    # a single-file fixture could satisfy. Everything the service reads still
    # goes through the pragma-configured engines above.
    seed_factory = make_session_factory(
        create_engine(f"sqlite+pysqlite:///{data_path}", future=True)
    )

    workspace_id = uuid4()
    user_id = uuid4()
    grant_id = uuid4()
    oauth_client_id = "split-settings-client"
    orphan_oauth_client_id = "split-settings-orphan-client"
    with control_factory.begin() as session:
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                title=f"{slug} control",
                name=f"{slug} control",
            )
        )
        session.add(
            User(
                id=user_id,
                email=f"{slug}@example.com",
                password_hash="unused",
                display_name="Split settings owner",
            )
        )
        session.add(
            OAuthClient(
                id=oauth_client_id,
                client_name="Split Settings Client",
                redirect_uris=["https://client.example/callback"],
                scope="mcp:read",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )
        )
        session.flush()
        session.add(
            WorkspaceMembership(
                user_id=user_id, workspace_id=workspace_id, role="owner"
            )
        )
        session.add(
            OAuthConsent(
                user_id=user_id,
                client_id=oauth_client_id,
                workspace_id=workspace_id,
                scope="mcp:read",
                granted_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        session.add(
            OAuthRefreshToken(
                grant_id=grant_id,
                token_lookup_hash="sha256:split-settings-live",
                user_id=user_id,
                client_id=oauth_client_id,
                scope="mcp:read",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            )
        )
        # A second registered OAuth app with NO consent in this workspace. Its
        # connection is revoked, but it still has a name to render -- and that
        # name is reachable only by looking the id up on the control plane,
        # because the workspace-wide consent scan never sees this client.
        session.add(
            OAuthClient(
                id=orphan_oauth_client_id,
                client_name="Orphaned Grant App",
                redirect_uris=["https://orphan.example/callback"],
                scope="mcp:read",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )
        )

    with seed_factory.begin() as session:
        session.add(
            MCPClient(
                workspace_id=workspace_id,
                name="Split settings connection",
                client_type="oauth",
                scopes=["mcp:read"],
                status="active",
                issued_to_user_id=user_id,
                runtime_binding_key=f"oauth_grant:{grant_id}",
                token_metadata={"oauth_client_id": oauth_client_id},
            )
        )
        session.add(
            MCPClient(
                workspace_id=workspace_id,
                name="Split settings orphaned grant",
                client_type="oauth",
                scopes=["mcp:read"],
                status="active",
                issued_to_user_id=user_id,
                token_metadata={"oauth_client_id": orphan_oauth_client_id},
            )
        )

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        workspace_id=workspace_id,
        role="owner",
        token_kind="session",
        provenance="browser_session",
    )
    return control_factory, data_factory, workspace_id, actor


def test_split_provider_settings_mcp_client_listing_spans_both_planes(tmp_path):
    """TEN-01A: the Settings MCP listing reads a workspace table and correlates
    it against three control tables, so it needs one session per plane."""

    control_factory, data_factory, workspace_id, actor = (
        _seed_split_settings_mcp_fixture(tmp_path, slug="split-settings-mcp")
    )

    with control_factory() as control_session, data_factory() as workspace_session:
        page = SettingsService().list_mcp_clients(
            control_session=control_session,
            workspace_session=workspace_session,
            workspace_id=workspace_id,
            actor=actor,
            cursor_secret="split-settings-secret",
        )
    # The 404 gate saw the control-plane workspace row, the counts saw the
    # workspace-plane MCPClient row, and the Active facet plus the rendered
    # state saw the control-plane consent/token/client trio -- one answer built
    # from two databases.
    assert page.total_count == 2
    assert (page.status_counts.active, page.status_counts.disabled) == (1, 1)
    assert page.filtered_count == 1
    assert [row.connection_state for row in page.data] == ["active"]
    assert [row.client_name for row in page.data] == ["Split Settings Client"]

    # The orphaned grant is only visible under status="all". Its OAuth client
    # is registered but consented in no workspace, so the workspace-wide
    # control scan never names it: rendering its client_name at all takes the
    # extra primary-key lookup this page triggers for its own rows.
    with control_factory() as control_session, data_factory() as workspace_session:
        everything = SettingsService().list_mcp_clients(
            control_session=control_session,
            workspace_session=workspace_session,
            workspace_id=workspace_id,
            actor=actor,
            status="all",
            cursor_secret="split-settings-secret",
        )
    rendered = {row.name: row for row in everything.data}
    assert everything.filtered_count == 2
    assert set(rendered) == {
        "Split settings connection",
        "Split settings orphaned grant",
    }
    orphan = rendered["Split settings orphaned grant"]
    assert orphan.connection_state == "unknown"
    assert orphan.client_name == "Orphaned Grant App"

    # Searching by OAuth client name is the one filter arm that has no
    # MCPClient column behind it at all: "settings client" appears only in
    # OAuthClient.client_name ("Split Settings Client"), never in the client's
    # own name ("Split settings connection"), its type ("oauth") or its
    # hyphenated oauth_client_id. It used to be an outer join; it is now an IN
    # over ids the control plane resolved, and it has to match the same rows.
    with control_factory() as control_session, data_factory() as workspace_session:
        searched = SettingsService().list_mcp_clients(
            control_session=control_session,
            workspace_session=workspace_session,
            workspace_id=workspace_id,
            actor=actor,
            query="Settings Client",
            cursor_secret="split-settings-secret",
        )
    assert searched.filtered_count == 1
    assert [row.name for row in searched.data] == ["Split settings connection"]

    with control_factory() as control_session, data_factory() as workspace_session:
        missed = SettingsService().list_mcp_clients(
            control_session=control_session,
            workspace_session=workspace_session,
            workspace_id=workspace_id,
            actor=actor,
            query="no such client anywhere",
            cursor_secret="split-settings-secret",
        )
    assert missed.filtered_count == 0, (
        "a term matching nothing on either plane must still filter everything "
        "out -- otherwise the assertion above proves nothing about the search"
    )

    # Revert-fail companion 1: the pre-fix shape read everything off the
    # workspace session, where the Workspace row does not exist, so the
    # existence gate wrongly reports the workspace missing.
    with data_factory() as workspace_session:
        with pytest.raises(ReadNotFoundError):
            SettingsService().list_mcp_clients(
                control_session=workspace_session,
                workspace_session=workspace_session,
                workspace_id=workspace_id,
                actor=actor,
                cursor_secret="split-settings-secret",
            )

    # Revert-fail companion 2: reading both planes off CONTROL clears the gate
    # and then finds no clients at all. Base.metadata.create_all builds every
    # table on both engines, so the wrong plane answers ZERO ROWS instead of
    # raising -- if these ever agree with the split answer above, the fixture
    # has stopped being split and this test proves nothing.
    with control_factory() as control_session:
        control_only = SettingsService().list_mcp_clients(
            control_session=control_session,
            workspace_session=control_session,
            workspace_id=workspace_id,
            actor=actor,
            cursor_secret="split-settings-secret",
        )
    assert control_only.total_count == 0
    assert (
        control_only.status_counts.active,
        control_only.status_counts.disabled,
    ) == (0, 0)
    assert control_only.data == []


def test_split_provider_settings_policy_cards_read_the_control_plane(tmp_path):
    """TEN-01A: every Settings policy value is a ``Workspace`` column, so the
    cards come off CONTROL while the MCP list under them stays workspace-plane.
    """

    control_factory, data_factory, workspace_id, _actor = (
        _seed_split_settings_mcp_fixture(tmp_path, slug="split-settings-policies")
    )

    with control_factory() as control_session, data_factory() as workspace_session:
        policies = SettingsService().get_policies(
            control_session=control_session,
            workspace_session=workspace_session,
            workspace_id=workspace_id,
        )
    assert policies.workspace.policy["slug"] == "split-settings-policies"
    cards = {row.name: row for row in policies.mcp_clients}
    assert cards["Split settings connection"].client_name == "Split Settings Client"
    assert cards["Split settings connection"].connection_state == "active"
    assert cards["Split settings orphaned grant"].client_name == "Orphaned Grant App"
    assert cards["Split settings orphaned grant"].connection_state == "unknown"

    # Revert-fail companion: the pre-fix shape read the cards off the workspace
    # session, which holds no Workspace row.
    with data_factory() as workspace_session:
        with pytest.raises(ReadNotFoundError):
            SettingsService().get_policies(
                control_session=workspace_session,
                workspace_session=workspace_session,
                workspace_id=workspace_id,
            )


def test_workspace_write_intent_locks_a_shard_with_no_control_tables(true_shard):
    """TEN-01A: a workspace-plane write cannot serialize itself on ``workspace``.

    ``acquire_write_intent`` takes SQLite's RESERVED lock with a zero-row
    ``UPDATE workspace``. On a real shard that table is not there at all, so
    the lock statement raises instead of locking, and the settings mutations
    that call it on their workspace session would fail outright. The
    workspace-scoped alternative has to still take a real lock on the same
    database, which is asserted here by contending for it.

    Converted to the shared fixture. This test dropped ONE table by hand --
    ``workspace`` -- from an otherwise complete database, while its own
    docstring claimed "a shard is exactly the workspace-plane tables, and none
    of the control-plane ones" and its name claimed "no control tables". Every
    other control-plane table was still present, so the shape under test was
    not the shape the name described. ``build_true_shard`` drops the manifest's
    whole control-plane set and verifies none survived, so the name is now a
    measured property of the fixture rather than a description of it.
    """

    with true_shard.shard_factory.begin() as session:
        with true_shard.expect_missing_table("workspace"):
            acquire_write_intent(session)

    with true_shard.shard_factory.begin() as session:
        acquire_workspace_write_intent(session)
        contender = sqlite3.connect(true_shard.shard_path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()


class _RecordingMailProvider:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def send(self, message) -> None:
        self.messages.append(message)


def test_split_password_reset_queues_no_workspace_plane_delivery_job(tmp_path):
    """MAIL-02: the email_delivery Job could never be claimed under a split.

    ``EmailOutbox`` is identity-plane (no workspace_id column, FKs to users
    and password_reset_tokens only) while ``Job`` is workspace-sharded, and
    the pre-fix ``queue_token_email`` wrote BOTH on the one control session.
    The worker only ever claims through ``session_for(workspace_id)``, so
    that Job landed on a plane nobody claims from -- and had it been claimed,
    its handler would have called ``deliver()`` with the WORKSPACE session and
    raised "email outbox row not found". The Job path is deleted: the
    control-plane sweep is the only delivery route now, so no Job is created
    on either plane and the mail still goes out.
    """
    control_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'split-mail-control.sqlite'}"
    )
    data_engine = make_engine(
        f"sqlite+pysqlite:///{tmp_path / 'split-mail-data.sqlite'}"
    )
    Base.metadata.create_all(control_engine)
    Base.metadata.create_all(data_engine)
    control_factory = make_session_factory(control_engine)
    data_factory = make_session_factory(data_engine)

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        public_base_url="https://wiki.example.com",
        session_secret="split-mail-delivery-test-secret",
        mail_provider="smtp",
        mail_from_address="wiki@example.com",
        smtp_host="smtp.example.com",
    )
    provider = _RecordingMailProvider()
    delivery = EmailDeliveryService(settings=settings, provider=provider)

    with control_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace_id = workspace.id
        session.add(
            User(
                email="split-mail@example.com",
                password_hash=CredentialHasher().hash_password("original-password"),
                display_name="Split mail",
                status="active",
                email_verified_at=datetime.now(UTC),
            )
        )

    issued = PasswordRecoveryService(
        session_factory=control_factory,
        credential_hasher=CredentialHasher(),
        public_base_url=settings.public_base_url,
        mail_delivery_service=delivery,
    ).issue_password_reset(email="split-mail@example.com", delivery="email")
    assert issued.outbox_id is not None

    for plane, factory in (("control", control_factory), ("data", data_factory)):
        with factory() as session:
            job_count = session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.job_type == "email_delivery")
            )
        assert job_count == 0, f"an email_delivery Job was created on the {plane} plane"

    delivered = delivery.sweep_due(session_factory=control_factory)

    assert delivered == 1
    assert len(provider.messages) == 1
    with control_factory() as session:
        outbox = session.get(EmailOutbox, issued.outbox_id)
        assert outbox is not None and outbox.status == "sent"

    # Revert-fail companion: a Job written on the control plane is invisible
    # from the data plane, which is the plane every claim path reaches through
    # session_for(workspace_id). Without this the first assertion could pass
    # against a fixture that is not actually split.
    with control_factory.begin() as session:
        JobQueueService().enqueue(
            session=session,
            workspace_id=workspace_id,
            job_type="email_delivery",
            trigger_type="password_reset",
            idempotency_key="split-mail-unclaimable",
        )
    with data_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0, (
            "the two planes share storage; this fixture cannot distinguish them"
        )


def test_split_provider_workspace_row_routes_read_the_control_plane(true_shard):
    """TEN-01A follow-up: ``Workspace`` columns come off CONTROL on every route.

    ``GET /workspaces/{id}`` and ``GET /workspaces/{id}/library/capabilities``
    both resolve a ``Workspace`` row -- a control-plane table by the tenancy
    manifest -- and both used to do it on ``WorkspaceSessionFactoryDep``. That
    is the same defect the Settings routes were split to fix, in the same file,
    and it left ``library_policy`` served from the control plane by
    ``get_policies`` and from the workspace plane two routes away.

    Converted from a routing test to a separability test. It used to give both
    engines the full schema and seed a DIFFERENT ``Workspace`` row on each, so
    the response named which plane answered. That is a real check, but it is
    a choice between two successes: both planes could answer, and nothing in
    the fixture made the wrong one impossible. Here the workspace plane does
    not carry ``workspace`` at all, so a route that reached for it cannot
    return 200 by any path through the code -- and the decoy values are kept
    on the control row so the assertions still name the plane rather than
    merely observing that the request did not blow up.
    """

    control_factory = true_shard.control_factory
    shard_factory = true_shard.shard_factory

    workspace_id = uuid4()
    with control_factory.begin() as session:
        control_policy = dict(default_library_policy())
        control_policy["max_upload_bytes"] = 1_234_567
        session.add(
            Workspace(
                id=workspace_id,
                slug="ws-row-split",
                title="Control Plane Title",
                name="Control Plane Title",
                goal="control plane goal",
                library_policy=control_policy,
            )
        )

    provider = SplitProvider(control_factory, shard_factory)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
    )
    container = assemble(settings, db=provider)
    app = create_app(
        settings=settings,
        page_service=PageService(control_factory),
        container=container,
        bootstrap_on_startup=False,
    )
    client = TestClient(app)
    authenticate_test_client(client, email="ws-row-split@example.com")
    with control_factory.begin() as session:
        user_id = session.scalar(
            select(User.id).where(User.email == "ws-row-split@example.com")
        )
        session.add(
            WorkspaceMembership(
                user_id=user_id, workspace_id=workspace_id, role="owner"
            )
        )

    detail = client.get(f"/api/v1/workspaces/{workspace_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["title"] == "Control Plane Title"
    assert detail.json()["data"]["goal"] == "control plane goal"

    capabilities = client.get(f"/api/v1/workspaces/{workspace_id}/library/capabilities")
    assert capabilities.status_code == 200, capabilities.text
    limits = {
        item["max_upload_bytes"] for item in capabilities.json()["data"]["formats"]
    }
    assert limits == {1_234_567}, (
        "library_policy did not come from the control-plane workspace row; "
        "get_policies already serves the same column from control, so the two "
        f"routes would disagree: {sorted(limits)}"
    )

    # Revert-fail companion: the pre-fix shape resolved the same row on the
    # workspace session. Both routes funnel through this one read, and on a
    # shard it cannot even reach its own not-found branch -- the table is
    # gone, so the failure is a missing table rather than a missing row. That
    # is the whole difference between routing and separability: with the full
    # schema on both engines this call returns ReadNotFoundError, which a
    # route could plausibly translate into a 404 and look survivable.
    with shard_factory() as workspace_session:
        with true_shard.expect_missing_table("workspace"):
            WorkspaceReadService().get_workspace(workspace_session, workspace_id)


def test_settings_mcp_client_writes_still_fail_on_a_real_shard(true_shard):
    """FINDING, pinned rather than fixed: the two Settings WRITE routes do not
    work on a physically separate shard, and the plane split did not change
    that.

    ``MCPClient`` is workspace-plane, so ``update_mcp_client_status`` and
    ``delete_mcp_client`` mutate it on the workspace session and emit their
    audit row on that SAME session -- which is what every other write in this
    codebase does, and what keeps the mutation and its audit atomic.
    ``audit_event`` is MIXED in the tenancy manifest but carries
    ``ForeignKey("workspace.id")`` (backend/models/audit.py), and a shard has
    no ``workspace`` table, so the audit INSERT raises before the mutation can
    commit.

    This is NOT a regression: before the split the same two routes died one
    statement earlier, on ``acquire_write_intent``'s ``UPDATE workspace``. It
    is also not fixed by moving the audit row to the control session -- that
    buys a working route at the cost of a genuine distributed transaction:
    the ``mcp_client`` mutation and its audit row would commit against two
    databases with no shared boundary, so a crash between them either loses
    the audit trail for a completed change or records a change that never
    happened. That trade needs a decision above this lane, so the current
    behaviour is pinned here instead of quietly claimed to work.

    The READ path IS split-clean and is asserted alongside, so this test says
    exactly which half works.

    This test is where ``build_true_shard`` came from: the shard construction
    that used to be inline here is now the shared fixture, unchanged in shape
    and with its post-drop state verified, so a second test asserting
    separability costs one argument instead of a copy of this preamble.
    """

    control_factory = true_shard.control_factory
    shard_factory = true_shard.shard_factory
    seed_factory = true_shard.seed_factory

    workspace_id = uuid4()
    user_id = uuid4()
    grant_id = uuid4()
    oauth_client_id = "shard-write-client"
    with control_factory.begin() as session:
        session.add(
            Workspace(
                id=workspace_id,
                slug="shard-write",
                title="Shard write",
                name="Shard write",
            )
        )
        session.add(
            User(
                id=user_id,
                email="shard-write@example.com",
                password_hash="unused",
                display_name="Shard write owner",
            )
        )
        session.add(
            OAuthClient(
                id=oauth_client_id,
                client_name="Shard Write App",
                redirect_uris=["https://client.example/callback"],
                scope="mcp:read",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )
        )
        session.flush()
        session.add(
            WorkspaceMembership(
                user_id=user_id, workspace_id=workspace_id, role="owner"
            )
        )
        session.add(
            OAuthConsent(
                user_id=user_id,
                client_id=oauth_client_id,
                workspace_id=workspace_id,
                scope="mcp:read",
                granted_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        session.add(
            OAuthRefreshToken(
                grant_id=grant_id,
                token_lookup_hash="sha256:shard-write-live",
                user_id=user_id,
                client_id=oauth_client_id,
                scope="mcp:read",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            )
        )

    active_id = uuid4()
    disabled_id = uuid4()
    with seed_factory.begin() as session:
        session.add(
            MCPClient(
                id=active_id,
                workspace_id=workspace_id,
                name="Shard connection",
                client_type="oauth",
                scopes=["mcp:read"],
                status="active",
                issued_to_user_id=user_id,
                runtime_binding_key=f"oauth_grant:{grant_id}",
                token_metadata={"oauth_client_id": oauth_client_id},
            )
        )
        session.add(
            MCPClient(
                id=disabled_id,
                workspace_id=workspace_id,
                name="Shard connection off",
                client_type="oauth",
                scopes=["mcp:read"],
                status="disabled",
                issued_to_user_id=user_id,
                token_metadata={"oauth_client_id": oauth_client_id},
            )
        )

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        user_id=user_id,
        workspace_id=workspace_id,
        role="owner",
        token_kind="session",
        provenance="browser_session",
    )
    service = SettingsService()

    # The READ half is split-clean and works on a shard today.
    with control_factory() as control_session, shard_factory() as workspace_session:
        page = service.list_mcp_clients(
            control_session=control_session,
            workspace_session=workspace_session,
            workspace_id=workspace_id,
            actor=actor,
            status="all",
            cursor_secret="shard-write-secret",
        )
    assert {row.name for row in page.data} == {
        "Shard connection",
        "Shard connection off",
    }
    assert page.status_counts.active == 1

    with control_factory() as control_session, shard_factory() as workspace_session:
        policies = service.get_policies(
            control_session=control_session,
            workspace_session=workspace_session,
            workspace_id=workspace_id,
        )
    assert policies.workspace.policy["slug"] == "shard-write"
    assert {row.name for row in policies.mcp_clients} == {
        "Shard connection",
        "Shard connection off",
    }

    # The WRITE half does not. Both die on the audit row's FK to `workspace`.
    with control_factory() as control_session, shard_factory.begin() as ws_session:
        with true_shard.expect_missing_table("workspace"):
            service.update_mcp_client_status(
                control_session=control_session,
                workspace_session=ws_session,
                workspace_id=workspace_id,
                actor=actor,
                client_id=active_id,
                status="disabled",
            )
    with control_factory() as control_session, shard_factory.begin() as ws_session:
        with true_shard.expect_missing_table("workspace"):
            service.delete_mcp_client(
                control_session=control_session,
                workspace_session=ws_session,
                workspace_id=workspace_id,
                actor=actor,
                client_id=disabled_id,
            )

    # The reason, asserted rather than described: a MIXED table with a hard FK
    # into a control-plane one cannot be written from a shard.
    from backend.models.audit import AuditEvent

    assert "audit_event" in MIXED_PLANE_TABLES
    audit_workspace_fks = {
        str(fk.column) for fk in AuditEvent.__table__.c.workspace_id.foreign_keys
    }
    assert audit_workspace_fks == {"workspace.id"}, (
        "audit_event no longer points at workspace.id -- if a shard can now "
        "carry its own audit rows, this test and the FINDING it pins in "
        "tests/static/test_workspace_plane_boundary.py are stale and the two "
        "Settings write routes should be re-measured against a real shard."
    )
