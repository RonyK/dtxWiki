"""Cross-plane FK census, and the gate on ENABLING a per-workspace database.

Two things live here, and the second is keyed on the first:

1. The census (``backend.tenancy_manifest.workspace_to_control_fk_edges``)
   pinned edge-by-edge, so the recorded inventory cannot drift silently.
2. The enable gate: if a per-workspace physical session factory is ever
   enabled while that census is non-empty, ``test_the_enable_gate_...``
   fails. It reads the census rather than a hand-maintained list, so it goes
   red on its own the day someone wires a real sharding overlay.

Why the census is keyed on ``workspace`` and not on ``users``
-------------------------------------------------------------
An earlier recorded framing of this gap named the three ``-> users`` edges.
Against the measured census that understates the edges by ~19x (57 against 3)
and the unwritable tables by 27x (54 against 2). Measured on this tree:

- 82 shard-resident tables (81 WORKSPACE_PLANE + 1 MIXED_PLANE), carrying
  57 FK constraints into control-plane parents. All 57 are single-column.
- By parent: ``workspace`` 54 edges, ``users`` 3 edges.
- 54 of those 82 tables are BLOCKED (at least one control-plane FK parent):
  ``workspace`` blocks 54, ``users`` blocks 2. Only 28 are insertable.
- 4 of the 57 edges have all-nullable local columns
  (``audit_event.workspace_id``, ``mcp_chat_session.owner_user_id``,
  ``mcp_client.created_by``, ``mcp_client.issued_to_user_id``).

A neighbouring count of 55 for the same schema is not a contradiction: it
scopes the children to WORKSPACE_PLANE_TABLES only and so omits the MIXED
child ``audit_event.workspace_id -> workspace``. This census includes it,
because a manifest-pure shard is expected to carry ``audit_event`` and that
row is exactly what the recorded Settings-write failure trips over.

Why nullability is NOT a mitigation
-----------------------------------
It is tempting to read those 4 nullable edges as the soft ones, and to
conclude that nulling a column or mirroring a parent row would make a shard
writable. Both conclusions are wrong, and the reason is not a referential
check at all: on a shard the parent TABLE is absent, and SQLite resolves the
parent table before it considers the value. The insert fails outright.
``test_a_null_value_in_a_nullable_edge_still_fails_when_the_parent_is_absent``
proves that on this project's own emitted DDL, as a controlled pair -- the
identical INSERT succeeds as soon as a bare parent table exists.

So a census that counted only NOT NULL edges, or that ranked nullable edges
as a lesser problem, would be measuring the wrong property.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.schema import CreateTable

import backend.models  # noqa: F401 -- populate Base.registry/Base.metadata
from backend.app.container import (
    PER_WORKSPACE_DATABASE_TOPOLOGY,
    SINGLE_DATABASE_TOPOLOGY,
    UNDECLARED_STORAGE_TOPOLOGY,
    assemble,
    declared_storage_topology,
)
from backend.app.settings import Settings
from backend.models.base import Base
from backend.tenancy_manifest import (
    MIXED_PLANE_TABLES,
    WORKSPACE_PLANE_TABLES,
    physical_split_blockers,
    shard_blocked_tables,
    workspace_to_control_fk_edges,
)
from tests.static._python_ast import ROOT, parse_python_file
from tests.static.test_architecture_boundaries import iter_python_files, write_fixture

# The recorded inventory, one entry per FK constraint, as
# ``<child>.<local columns> -> <control-plane parent>``. Regenerate by
# printing ``edge.label`` for ``workspace_to_control_fk_edges()``; do not
# hand-edit an entry to make a failure go away, because every entry here is
# a table a per-workspace physical database cannot write.
EXPECTED_WORKSPACE_TO_CONTROL_FK_EDGES: frozenset[str] = frozenset(
    {
        "agent_input_bundle.workspace_id -> workspace",
        "agent_input_bundle_item.workspace_id -> workspace",
        "agent_page.workspace_id -> workspace",
        "agent_page_version.workspace_id -> workspace",
        "ask_session.workspace_id -> workspace",
        "ask_turn.workspace_id -> workspace",
        "ask_turn_citation.workspace_id -> workspace",
        "audit_event.workspace_id -> workspace",
        "change_operation.workspace_id -> workspace",
        "change_set.workspace_id -> workspace",
        "chunk_embedding.workspace_id -> workspace",
        "client_delegated_task.workspace_id -> workspace",
        "content_chunk.workspace_id -> workspace",
        "context_citation.workspace_id -> workspace",
        "context_claim.workspace_id -> workspace",
        "context_ingress_proposal.workspace_id -> workspace",
        "context_page.workspace_id -> workspace",
        "context_page_version.workspace_id -> workspace",
        "edge_projection.workspace_id -> workspace",
        "feedback_item.workspace_id -> workspace",
        "job.workspace_id -> workspace",
        "knowledge_coverage_gap.workspace_id -> workspace",
        "knowledge_node_stat.workspace_id -> workspace",
        "library_artifact.workspace_id -> workspace",
        "library_attempt.workspace_id -> workspace",
        "library_discovery_candidate.workspace_id -> workspace",
        "library_discovery_run.workspace_id -> workspace",
        "library_item.workspace_id -> workspace",
        "library_item_tombstone.workspace_id -> workspace",
        "library_item_version.workspace_id -> workspace",
        "library_projection_outbox.workspace_id -> workspace",
        "library_run.workspace_id -> workspace",
        "library_semantic_overlay.workspace_id -> workspace",
        "library_sync_run.workspace_id -> workspace",
        "lint_finding.workspace_id -> workspace",
        "lint_run.workspace_id -> workspace",
        "mcp_chat_artifact_link.workspace_id -> workspace",
        "mcp_chat_session.owner_user_id -> users",
        "mcp_chat_session.workspace_id -> workspace",
        "mcp_chat_tool_call_artifact.workspace_id -> workspace",
        "mcp_client.created_by -> users",
        "mcp_client.issued_to_user_id -> users",
        "mcp_client.workspace_id -> workspace",
        "mcp_tool_call.workspace_id -> workspace",
        "mcp_write_idempotency.workspace_id -> workspace",
        "page_links.workspace_id -> workspace",
        "page_name_index.workspace_id -> workspace",
        "page_provenance.workspace_id -> workspace",
        "proposal_source_item.workspace_id -> workspace",
        "review_decision.workspace_id -> workspace",
        "review_item.workspace_id -> workspace",
        "tree_policy.workspace_id -> workspace",
        "upload_asset.workspace_id -> workspace",
        "upload_asset_binding.workspace_id -> workspace",
        "workspace_credential_grant.workspace_id -> workspace",
        "workspace_media_write_intent.workspace_id -> workspace",
        "workspace_purge_grant.workspace_id -> workspace",
        "workspace_tree_node.workspace_id -> workspace",
    }
)

# The nullable subset, called out because reading it as "the soft edges" is
# the specific mistake this file exists to prevent. See the module docstring.
EXPECTED_ALL_NULLABLE_EDGES: frozenset[str] = frozenset(
    {
        "audit_event.workspace_id -> workspace",
        "mcp_chat_session.owner_user_id -> users",
        "mcp_client.created_by -> users",
        "mcp_client.issued_to_user_id -> users",
    }
)


def test_the_workspace_to_control_fk_census_is_exactly_what_is_recorded():
    """Pin the census edge-by-edge: 58 edges, 55 of 86 tables blocked.

    Measured here, not copied: ``workspace`` 55 edges / 55 blocked tables,
    ``users`` 3 edges / 2 blocked tables, 31 of the 86 shard-resident tables
    insertable, 4 edges all-nullable. Adding, removing or re-pointing any
    cross-plane FK changes ``labels`` and fails this test, which is the point
    -- the inventory is not allowed to move without a reviewer seeing it.

    A-1 folded
    library_conversion_run/attempt and library_preview_run/attempt/artifact
    into the unified library_run/library_attempt (plus six 1:1 detail
    tables with no direct FK into ``workspace``), replacing 5 prior
    ``-> workspace`` edges with 2 and adding 3 net shard-resident tables
    (82 = 79 + 8 new - 5 retired). P5.3 then retired
    ``context_media_legacy_cutover`` (its only FK pointed at
    ``workspace_media_write_intent``, itself workspace-plane, so it never
    contributed a ``-> control-plane`` edge here; only the resident count
    moves): 81 = 82 - 1. P10.3 T1 (D-01v3) added the new workspace-plane
    replica table ``workspace_credential_grant``, with its own FK into
    ``workspace``: 82 = 81 + 1, 57 = 56 + 1. PURGE_AUTHZ_PLAN S-6 added
    ``workspace_purge_grant``, another workspace-plane table with a single FK
    into ``workspace`` and no other cross-plane edge: 83 = 82 + 1,
    58 = 57 + 1, and since its parent is ``workspace`` (not ``users``), that
    parent's own edge and blocked-table counts both move too: 55 = 54 + 1.
    The insertable count is unchanged (83 - 55 = 28 = 82 - 54) because the new
    table is itself blocked, not newly reachable.

    TreeRail personal favorites add one workspace-plane table without any
    cross-plane FK: 84 = 83 + 1. The blocked inventory stays at 55 tables /
    58 edges, while insertable tables increase from 28 to 29.

    The per-user Inbox adds ``workspace_inbox_entry`` the same way (bare user
    and workspace UUIDs, no cross-plane FK): 85 = 84 + 1, insertable 30 = 29 + 1.
    Its recipient lifecycle table also uses bare UUIDs: 86 = 85 + 1 and
    insertable 31 = 30 + 1, without changing any control-plane FK edge.
    """

    edges = workspace_to_control_fk_edges()
    labels = frozenset(edge.label for edge in edges)
    assert labels == EXPECTED_WORKSPACE_TO_CONTROL_FK_EDGES
    # One label per constraint: no two constraints may collapse into one entry.
    assert len(edges) == len(labels) == 58
    assert all(len(edge.columns) == 1 for edge in edges)
    # Reconciles this census with the workspace-plane-only scoping recorded
    # elsewhere in the tree as 57: the difference is the one MIXED child.
    assert len([edge for edge in edges if edge.child in WORKSPACE_PLANE_TABLES]) == 57
    assert [edge.label for edge in edges if edge.child in MIXED_PLANE_TABLES] == [
        "audit_event.workspace_id -> workspace"
    ]

    resident = WORKSPACE_PLANE_TABLES | MIXED_PLANE_TABLES
    assert len(resident) == 86

    by_parent = {
        parent: [edge for edge in edges if edge.parent == parent]
        for parent in {edge.parent for edge in edges}
    }
    assert {parent: len(found) for parent, found in by_parent.items()} == {
        "workspace": 55,
        "users": 3,
    }
    assert {
        parent: len({edge.child for edge in found})
        for parent, found in by_parent.items()
    } == {"workspace": 55, "users": 2}

    blocked = shard_blocked_tables()
    assert set(blocked) <= resident
    assert len(blocked) == 55
    assert len(resident) - len(blocked) == 31

    assert (
        frozenset(edge.label for edge in edges if edge.all_columns_nullable)
        == EXPECTED_ALL_NULLABLE_EDGES
    )
    assert len(EXPECTED_ALL_NULLABLE_EDGES) == 4


def test_the_blocker_summary_counts_the_live_census_rather_than_a_literal():
    """``physical_split_blockers()`` must recount, not recite.

    A blocker line that hard-coded a number would be the exact defect this
    census is guarding against, so the numbers it prints are checked against
    the census computed in the same call.
    """

    edges = workspace_to_control_fk_edges()
    blocked = shard_blocked_tables()
    resident = WORKSPACE_PLANE_TABLES | MIXED_PLANE_TABLES
    blockers = physical_split_blockers()

    assert blockers
    assert f"{len(blocked)} of {len(resident)} shard-resident tables" in blockers[0]
    assert f"{len(edges)} foreign-key edges" in blockers[0]
    for parent in sorted({edge.parent for edge in edges}):
        parent_edges = [edge for edge in edges if edge.parent == parent]
        children = {edge.child for edge in parent_edges}
        assert (
            f"  {parent}: {len(parent_edges)} edge(s) blocking {len(children)} table(s)"
        ) in blockers


@pytest.mark.parametrize("child_name", sorted(EXPECTED_ALL_NULLABLE_EDGES))
def test_every_nullable_edge_is_still_a_hard_blocker(child_name: str):
    """A nullable local column does not make the edge softer, only quieter.

    The blocked-table census counts a table once ANY parent is control-plane
    regardless of nullability; this pins that the four nullable edges are all
    counted rather than discounted.
    """

    child = child_name.split(".", 1)[0]
    assert child in shard_blocked_tables()


def test_a_null_value_in_a_nullable_edge_still_fails_when_the_parent_is_absent():
    """The controlled pair behind "nullability is irrelevant on a shard".

    Same emitted DDL, same INSERT, ``PRAGMA foreign_keys=ON``, a NULL in the
    nullable FK column ``audit_event.workspace_id``:

    * with no ``workspace`` table at all (the manifest-pure shard shape) the
      statement raises ``OperationalError: no such table: main.workspace``;
    * with a bare ``workspace`` table present it succeeds.

    So the failure is the ABSENT PARENT TABLE, not an unsatisfied reference.
    Nulling the column cannot fix a shard, and neither can a census that
    treats nullable edges as a lesser class.
    """

    table = Base.metadata.tables["audit_event"]
    assert table.c.workspace_id.nullable is True
    ddl = str(CreateTable(table).compile(dialect=sqlite_dialect.dialect()))
    assert "REFERENCES workspace (id)" in ddl

    supplied = {
        column.name: _value_satisfying_checks(table, column.name)
        for column in table.c
        if not column.nullable
    }
    statement = (
        f"INSERT INTO audit_event ({', '.join(supplied)}, workspace_id) "
        f"VALUES ({', '.join(['?'] * len(supplied))}, NULL)"
    )
    parameters = tuple(supplied.values())

    without_parent = _shard_connection(ddl)
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        without_parent.execute(statement, parameters)
    assert "no such table: main.workspace" in str(excinfo.value)

    with_parent = _shard_connection(ddl)
    with_parent.execute("CREATE TABLE workspace (id CHAR(32) NOT NULL PRIMARY KEY)")
    with_parent.execute(statement, parameters)
    assert with_parent.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == 1


def _shard_connection(ddl: str) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    # The app's own engine does the same (backend/app/db.py), so a shard runs
    # with enforcement live; assert it took effect rather than assume it.
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    connection.executescript(ddl)
    return connection


def _value_satisfying_checks(table, column_name: str) -> str:
    """Return a literal for a NOT NULL column, honouring an ``IN`` CHECK.

    Derived from the table rather than hard-coded so that a new NOT NULL
    column does not silently turn the INSERT above into a NOT NULL failure
    that looks like the FK result being asserted.
    """

    for constraint in table.constraints:
        if not isinstance(constraint, CheckConstraint):
            continue
        match = re.search(
            rf"\b{re.escape(column_name)}\s+IN\s+\(\s*'([^']+)'",
            str(constraint.sqltext),
        )
        if match:
            return match.group(1)
    return "x"


# ---------------------------------------------------------------------------
# The enable gate
# ---------------------------------------------------------------------------


def physical_split_enable_violations(
    enablement_signals: Sequence[str],
    *,
    blockers: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Refuse an enabled per-workspace physical topology while edges remain.

    ``enablement_signals`` is every observation that a per-workspace physical
    database is in play; ``blockers`` defaults to the live census, which is
    what makes this gate self-updating: it returns violations exactly when
    something enables the split AND the census is non-empty. Empty census and
    the same signals produce no violation, so the gate lifts by fixing the
    schema, never by editing this file.
    """

    signals = tuple(enablement_signals)
    if not signals:
        return ()
    reasons = tuple(physical_split_blockers() if blockers is None else blockers)
    if not reasons:
        return ()
    return (
        *(f"per-workspace physical database enabled: {signal}" for signal in signals),
        *reasons,
    )


def declared_topology_signals(provider: object | None = None) -> tuple[str, ...]:
    """The primary signal: what the provider DECLARES it implements.

    A declaration beats an inference, and backend/app/container.py says why
    at length: two distinct sessionmakers can still address one physical
    database, and one memoized factory can hide a genuinely split one. So a
    provider declaring ``per_workspace_database`` is an enablement signal
    even if its two factories compare equal, and a provider declaring
    ``single_database`` is not one even if they do not.

    ``undeclared`` (a foreign provider carrying no such attribute) is NOT
    treated as an enablement: nothing was claimed, so there is nothing to
    refuse. It is also not treated as a reassurance -- the two signals below
    still run.
    """

    subject = provider if provider is not None else _assemble_shipped_provider()
    declared = declared_storage_topology(subject)
    if declared in {SINGLE_DATABASE_TOPOLOGY, UNDECLARED_STORAGE_TOPOLOGY}:
        return ()
    return (f"the provider declares storage_topology={declared!r}",)


def shipped_composition_signals() -> tuple[str, ...]:
    """Observe whether the assembled container routes the two planes apart.

    A backstop for the declaration above, not a substitute for it: an overlay
    can reroute ``session_for`` and forget to update its declared topology,
    and that combination is exactly the accident this gate exists for. The
    default ``DatabaseProvider`` returns one ``sessionmaker`` from both
    ``control_session_factory()`` and ``session_for()``
    (backend/app/container.py), so distinct objects here mean something wired
    a real overlay into the shipped composition.

    Object identity is deliberately NOT used to decide whether the split is
    supported -- only whether something has started routing. The support
    question is answered by the census.
    """

    provider = _assemble_shipped_provider()
    control = provider.control_session_factory()
    workspace = provider.session_for(uuid4())
    if workspace is control:
        return ()
    return (
        "backend.app.container.assemble(...).db.session_for(workspace_id) "
        "returned a different factory than control_session_factory()",
    )


def _assemble_shipped_provider():
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")
    return assemble(settings).db


def in_tree_overlay_signals(*, root: Path = ROOT) -> tuple[str, ...]:
    """Find a ``DatabaseProvider`` subclass under ``backend/`` routing by hand.

    ``shipped_composition_signals`` only exercises the DEFAULT provider, so a
    subclass wired in at startup (``create_app(container=...)``,
    ``assemble(db=...)``) would not move it. Any in-tree subclass that
    overrides ``session_for`` is that shape. Named-node AST match only, the
    same tractability line tests/static/test_workspace_plane_boundary.py
    draws: this cannot see a provider defined outside this repository, which
    is why the census -- not this scan -- is the gate's authority.
    """

    signals: list[str] = []
    for path in iter_python_files("backend", root=root):
        relative = path.relative_to(root).as_posix()
        if relative.startswith("backend/tests/"):
            continue
        for node in ast.walk(parse_python_file(path, root=root)):
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(_is_database_provider_base(base) for base in node.bases):
                continue
            overrides = [
                member.name
                for member in node.body
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
                and member.name == "session_for"
            ]
            if overrides:
                signals.append(f"{relative}::{node.name} overrides session_for")
    return tuple(sorted(signals))


def _is_database_provider_base(base: ast.expr) -> bool:
    if isinstance(base, ast.Name):
        return base.id.endswith("DatabaseProvider")
    if isinstance(base, ast.Attribute):
        return base.attr.endswith("DatabaseProvider")
    return False


def test_the_enable_gate_is_green_only_because_nothing_enables_the_split():
    """The shipped composition must not enable a per-workspace database.

    All three signals are empty today -- the shipped provider declares
    ``single_database``, returns one factory for both planes, and no in-tree
    subclass overrides ``session_for`` -- while the census is NOT empty. If
    any signal fires while the census stands,
    ``physical_split_enable_violations`` reports it and this test fails.

    Note the asymmetry that makes this a gate rather than a description: the
    blockers alone are not a failure. The recorded limitation is the shipped
    state. Only enabling the split on top of it is.
    """

    assert physical_split_blockers()  # the gate is armed, i.e. edges remain
    signals = (
        *declared_topology_signals(),
        *shipped_composition_signals(),
        *in_tree_overlay_signals(),
    )
    assert physical_split_enable_violations(signals) == ()


def test_the_enable_gate_fails_for_a_provider_that_declares_the_split():
    """Mutation proof, primary signal: declare it and the gate must go red.

    This is the shape an overlay actually takes -- backend/app/container.py
    tells one to override ``storage_topology`` -- so the declared value has
    to be enough on its own, without the object comparison agreeing.
    """

    from backend.app.container import DatabaseProvider

    class _DeclaredPerWorkspaceProvider(DatabaseProvider):
        storage_topology = PER_WORKSPACE_DATABASE_TOPOLOGY

    provider = _DeclaredPerWorkspaceProvider(
        Settings(database_url="sqlite+pysqlite:///:memory:")
    )
    # Its two factories are still identical: only the declaration moved.
    assert provider.session_for(uuid4()) is provider.control_session_factory()

    signals = declared_topology_signals(provider)
    assert signals == (
        f"the provider declares storage_topology={PER_WORKSPACE_DATABASE_TOPOLOGY!r}",
    )
    assert physical_split_enable_violations(signals)


def test_an_undeclared_provider_is_neither_an_enablement_nor_a_reassurance():
    """``undeclared`` must not silently read as the shipped topology.

    A foreign provider that carries no ``storage_topology`` has claimed
    nothing; the gate leans on the other two signals for it rather than
    counting it as either safe or split.
    """

    class _ForeignProvider:
        pass

    assert declared_storage_topology(_ForeignProvider()) == (
        UNDECLARED_STORAGE_TOPOLOGY
    )
    assert declared_topology_signals(_ForeignProvider()) == ()
    # The provider this release actually ships does make the claim.
    assert declared_storage_topology(_assemble_shipped_provider()) == (
        SINGLE_DATABASE_TOPOLOGY
    )


def test_the_enable_gate_fails_for_a_provider_that_routes_workspaces_apart():
    """Mutation proof, backstop signal: reroute without redeclaring.

    A provider that hands back a per-workspace factory is what an overlay
    does; forgetting to move its declaration with it is the accident. The
    violation must name both the enablement and the census, because "a
    factory was swapped" alone does not tell the reader why it is refused.
    """

    from backend.app.container import DatabaseProvider

    class _PerWorkspacePhysicalProvider(DatabaseProvider):
        def session_for(self, workspace_id):
            return object()

    provider = _PerWorkspacePhysicalProvider(
        Settings(database_url="sqlite+pysqlite:///:memory:")
    )
    assert provider.session_for(uuid4()) is not provider.control_session_factory()
    # The stale declaration is exactly why the backstop has to exist.
    assert declared_topology_signals(provider) == ()

    violations = physical_split_enable_violations(("a test overlay provider",))
    assert violations
    assert violations[0].startswith("per-workspace physical database enabled:")
    assert any(
        "shard-resident tables cannot be INSERTed" in line for line in violations
    )
    assert any("workspace:" in line for line in violations)
    assert any("users:" in line for line in violations)


def test_the_enable_gate_is_keyed_on_the_census_not_on_a_maintained_list():
    """Same enablement signal, empty census: permitted.

    This is what "keyed on the census" has to mean -- the gate lifts when the
    cross-plane edges are gone and not one moment earlier, and no entry in
    this file has to be edited for that to happen.
    """

    signals = ("a test overlay provider",)
    assert physical_split_enable_violations(signals, blockers=()) == ()
    assert physical_split_enable_violations(signals, blockers=("one edge left",)) == (
        "per-workspace physical database enabled: a test overlay provider",
        "one edge left",
    )
    # And with no enablement, blockers alone are not a violation: the recorded
    # limitation is the shipped state, not a failure.
    assert physical_split_enable_violations((), blockers=("one edge left",)) == ()


def test_the_overlay_scan_detects_a_provider_subclass_wired_in_backend(
    tmp_path: Path,
):
    """Mutation proof for the structural signal.

    Without this, ``in_tree_overlay_signals`` returning ``()`` on a clean tree
    would be indistinguishable from a scan that can never fire.
    """

    write_fixture(
        tmp_path,
        "backend/overlay/sharded.py",
        "from backend.app.container import DatabaseProvider\n"
        "\n"
        "\n"
        "class ShardedDatabaseProvider(DatabaseProvider):\n"
        "    def session_for(self, workspace_id):\n"
        "        return self._shards[workspace_id]\n",
    )
    signals = in_tree_overlay_signals(root=tmp_path)
    assert signals == (
        "backend/overlay/sharded.py::ShardedDatabaseProvider overrides session_for",
    )
    assert physical_split_enable_violations(signals)


def test_the_overlay_scan_ignores_a_subclass_that_does_not_reroute(tmp_path: Path):
    """A subclass that leaves routing alone is not an enablement signal.

    The signal is overriding ``session_for``, not subclassing; otherwise the
    gate would fire on any provider that only adds, say, a health probe.
    """

    write_fixture(
        tmp_path,
        "backend/overlay/probe.py",
        "from backend.app.container import DatabaseProvider\n"
        "\n"
        "\n"
        "class ProbedDatabaseProvider(DatabaseProvider):\n"
        "    def ping(self):\n"
        "        return True\n",
    )
    assert in_tree_overlay_signals(root=tmp_path) == ()
