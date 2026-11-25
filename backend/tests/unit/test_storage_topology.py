"""Storage-topology capability, its startup refusal, and the operator surface.

Three properties are pinned here, each with a mutation recorded in the review
report that makes exactly one of these tests fail:

1. The topology is a DECLARED value on the provider, never inferred by
   comparing the objects ``control_session_factory()`` and ``session_for()``
   return.
2. Declaring per-workspace PHYSICAL databases refuses startup while the
   physical-split compatibility gate is red -- and the refusal is keyed on the
   live foreign-key census, so it lifts by itself if the edges ever go away.
3. ``/health/storage-topology`` says "unsupported" in the same breath, from
   the same census, and reports trace-persistence failures as a degradation
   without flipping readiness.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.container import (
    PER_WORKSPACE_DATABASE_TOPOLOGY,
    SHIPPED_STORAGE_TOPOLOGY,
    SINGLE_DATABASE_TOPOLOGY,
    DatabaseProvider,
    StorageTopologyUnsupported,
    assemble,
    check_storage_topology,
    declared_storage_topology,
    physical_split_compatibility,
    storage_topology_report,
)
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.mcp.protocol.runner import (
    _persist_trace,
    reset_trace_persistence_health,
    trace_persistence_health,
)
from backend.policy.auth import ActorContext
from backend.tenancy_manifest import workspace_to_control_fk_edges


def _settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
    )


class _SplitLookingProvider(DatabaseProvider):
    """Two DISTINCT factory objects, one physical database.

    The anti-pattern this whole capability exists to replace is inferring
    split support from ``control_session_factory() is not session_for(...)``.
    This provider makes that inference wrong: the objects differ, the database
    does not. It still declares ``single_database`` and must still start.
    """

    def session_for(self, workspace_id):  # see class docstring
        from sqlalchemy.orm import sessionmaker

        return sessionmaker(bind=self.session_factory.kw["bind"])


class _PerWorkspaceProvider(DatabaseProvider):
    storage_topology = PER_WORKSPACE_DATABASE_TOPOLOGY


class _TypoProvider(DatabaseProvider):
    storage_topology = "per-workspace-database"


# --- 1. declared, not inferred -----------------------------------------


def test_the_shipped_provider_declares_single_database() -> None:
    provider = DatabaseProvider(_settings())

    assert provider.storage_topology == SINGLE_DATABASE_TOPOLOGY
    assert declared_storage_topology(provider) == SHIPPED_STORAGE_TOPOLOGY


def test_two_distinct_factories_do_not_make_a_split_deployment() -> None:
    """The declaration wins over the sessionmaker comparison."""

    provider = _SplitLookingProvider(_settings())

    assert provider.session_for(uuid4()) is not provider.control_session_factory()
    assert declared_storage_topology(provider) == SINGLE_DATABASE_TOPOLOGY
    # No refusal: the objects differ, the topology does not.
    check_storage_topology(provider)


def test_a_provider_that_declares_nothing_reports_undeclared() -> None:
    """A foreign provider must not be shown as having affirmed the topology."""

    class _Foreign:
        pass

    assert declared_storage_topology(_Foreign()) == "undeclared"
    # Nothing was claimed, so there is nothing to refuse.
    check_storage_topology(_Foreign())
    assert (
        storage_topology_report(_Foreign())["storage_topology"] == "undeclared"
    )


# --- 2. the census and the refusal it is keyed on ----------------------


def test_the_gate_consumes_the_shared_census_including_its_mixed_plane_edge() -> None:
    """The gate keeps no inventory of its own.

    ``backend/tenancy_manifest.py`` owns the census and computes it from
    ``Base.metadata``; this asserts the gate's numbers ARE that census rather
    than a second list that can drift from it. The ``audit_event`` edge is
    named explicitly because ``audit_event`` is mixed-plane, so a
    workspace-plane-only census drops it and would report a smaller gap.
    """

    edges = workspace_to_control_fk_edges()
    gate = physical_split_compatibility()

    assert gate["workspace_to_control_fk_edges"] == len(edges)
    assert gate["blocking_parent_tables"] == sorted({e.parent for e in edges})
    assert [
        edge.label
        for edge in edges
        if edge.child == "audit_event"
    ] == ["audit_event.workspace_id -> workspace"]


def test_the_compatibility_gate_is_red_and_names_both_blocking_parents() -> None:
    gate = physical_split_compatibility()

    assert gate["compatible"] is False
    assert gate["blocking_parent_tables"] == ["users", "workspace"]
    # The refusal text an operator reads is the census's own measured lines,
    # not a restatement of them.
    assert gate["blockers"]


def test_declaring_per_workspace_databases_refuses_startup() -> None:
    settings = _settings()
    container = assemble(settings, db=_PerWorkspaceProvider(settings))

    with pytest.raises(StorageTopologyUnsupported) as excinfo:
        create_app(settings=settings, container=container)

    message = str(excinfo.value)
    assert PER_WORKSPACE_DATABASE_TOPOLOGY in message
    # The operator is told which parents block it, not just that something did.
    assert "workspace" in message and "users" in message


def test_an_unrecognized_topology_refuses_startup() -> None:
    settings = _settings()
    container = assemble(settings, db=_TypoProvider(settings))

    with pytest.raises(StorageTopologyUnsupported, match="unknown storage topology"):
        create_app(settings=settings, container=container)


def test_the_refusal_lifts_when_the_census_goes_empty(monkeypatch) -> None:
    """The guard is keyed on the census, not on a hardcoded verdict.

    Someone who actually removes every workspace-to-control foreign key must
    not also have to remember to delete this guard, and a reviewer must not
    have to trust a comment that says so.
    """

    monkeypatch.setattr(
        "backend.tenancy_manifest.workspace_to_control_fk_edges",
        lambda: (),
    )
    monkeypatch.setattr(
        "backend.tenancy_manifest.physical_split_blockers",
        lambda: (),
    )
    assert physical_split_compatibility()["compatible"] is True

    settings = _settings()
    provider = _PerWorkspaceProvider(settings)
    check_storage_topology(provider)
    assert (
        storage_topology_report(provider)["per_workspace_physical_databases"]
        == "supported"
    )


def test_the_shipped_app_starts_and_declares_single_database() -> None:
    settings = _settings()
    app = create_app(settings=settings)

    assert app.state.container.db.storage_topology == SINGLE_DATABASE_TOPOLOGY


# --- 3. the operator surface -------------------------------------------


def test_health_endpoint_states_per_workspace_databases_are_unsupported() -> None:
    settings = _settings()
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/health/storage-topology")

    assert response.status_code == 200
    body = response.json()
    assert body["storage_topology"] == SINGLE_DATABASE_TOPOLOGY
    assert body["shipped_storage_topology"] == SINGLE_DATABASE_TOPOLOGY
    assert body["per_workspace_physical_databases"] == "unsupported"
    assert body["physical_split_compatible"] is False
    # Reported, not asserted at a literal: the count is whatever the mapped
    # metadata currently says, and it must be non-zero for the verdict above
    # to be the honest one.
    assert body["workspace_to_control_fk_edges"] == len(
        workspace_to_control_fk_edges()
    )
    assert body["workspace_to_control_fk_edges"] > 0


class _ExplodingTraceFactory:
    """A trace session factory on an unreachable plane."""

    @contextmanager
    def begin(self):
        raise RuntimeError("shard unreachable")
        yield  # pragma: no cover - unreachable, keeps this a generator


def _trace_kwargs(workspace_id):
    return {
        "session_factory": _ExplodingTraceFactory(),
        "actor": ActorContext(
            actor_kind="mcp_client",
            actor_id="storage-topology-test",
            workspace_id=workspace_id,
        ),
        "name": "read_page",
        "arguments": {"workspace_id": str(workspace_id)},
        "payload": {"envelope": {"result_status": "succeeded"}},
        "mcp_session_id": str(uuid4()),
        "started_at": datetime.now(UTC),
        "latency_ms": 1,
        "trace_workspace_id": workspace_id,
    }


@pytest.fixture(autouse=True)
def _clean_trace_counters():
    reset_trace_persistence_health()
    yield
    reset_trace_persistence_health()


def test_a_swallowed_trace_failure_still_degrades_the_health_signal() -> None:
    """The swallow stays; the silence does not."""

    workspace_id = uuid4()
    assert trace_persistence_health() == {
        "status": "ok",
        "failures_total": 0,
        "last_failure_at": None,
    }

    # Best-effort telemetry: no raise reaches the caller.
    _persist_trace(**_trace_kwargs(workspace_id), fail_closed=False)

    health = trace_persistence_health()
    assert health["status"] == "degraded"
    assert health["failures_total"] == 1
    assert health["last_failure_at"] is not None

    settings = _settings()
    with TestClient(create_app(settings=settings)) as client:
        body = client.get("/health/storage-topology").json()
        ready = client.get("/health/ready")

    assert body["trace_persistence"]["status"] == "degraded"
    assert body["trace_persistence"]["failures_total"] == 1
    # Deliberately NOT a readiness failure: a telemetry write must not pull
    # the process out of its load balancer. Readiness answers on its own
    # components only.
    assert "trace_persistence" not in ready.json()["components"]


def test_a_fail_closed_trace_failure_is_counted_before_it_reraises() -> None:
    workspace_id = uuid4()

    with pytest.raises(RuntimeError, match="shard unreachable"):
        _persist_trace(**_trace_kwargs(workspace_id), fail_closed=True)

    assert trace_persistence_health()["failures_total"] == 1
