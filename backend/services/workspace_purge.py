"""Destructive purge of every row a workspace owns.

Why this exists
---------------
``WorkspaceWriteService.delete_workspace`` refuses to delete a workspace that
still holds content or operational rows, and reports the counts as blockers.
Refusing by default is right -- deletion is irreversible -- but on its own it
made deletion *unreachable*: a workspace accumulates rows its owner has no UI
to clear (``job`` alone reached five figures in the report that prompted this
module), so "remove these items, then try again" was advice nobody could act
on. This module is the second half of that flow: the owner is shown exactly
what is retained, confirms again, and the purge then removes it.

The plan is derived, not hand-maintained
----------------------------------------
Every domain table carries ``workspace_id`` after the Epic-3 tenancy work, so
scoping is uniform and the only hard problem is ordering. ``_build_plan``
derives that order from the live metadata on first use:

* Every foreign key, including nullable ones, constrains the delete order. A
  nullable key is not automatically safe to clear: it can be required by a
  CHECK constraint or name a source that must remain intact until its owner is
  deleted.
* The planner finds strongly connected components and clears only explicitly
  declared nullable edges that break a real cycle. A declaration can carry the
  companion values needed to preserve the row's invariants while the cycle is
  open. The public-page grant, for example, must clear its flag, scope, and
  version together.
* The remaining edges form a DAG. Tables are topologically sorted and emitted
  children-first.

Deriving beats a frozen list because a table added later is ordered
automatically instead of being silently skipped, and because an unbreakable
cycle raises ``WorkspacePurgePlanError`` at plan time -- loudly, in every test
that touches deletion -- rather than leaving half a workspace behind.

Nothing here relies on ``ON DELETE CASCADE``. Declared cascades only fire where
the engine enforces foreign keys, and this codebase's own fixtures run without
``PRAGMA foreign_keys=ON`` (backend/tests/conftest.py), so a purge that leaned
on them would pass its tests and strand rows in production, or the reverse.
``_delete_seed_workspace`` already made that call for the seed scaffolding;
this module keeps the same rule for content.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

from sqlalchemy import MetaData, delete, update
from sqlalchemy.orm import Session

from backend.models.base import Base

# The only hand-maintained list: tables that belong to no workspace. Anything
# else must carry workspace_id, which tests/static/test_workspace_purge_
# coverage.py enforces -- a new table without it fails there rather than
# leaking rows here.
#
# The four auth tables are listed for a reason that is easy to get wrong:
# api_tokens, oauth_codes, oauth_refresh_tokens and oauth_access_token_jti used
# to be workspace-scoped and are not any more. OAuth's family unit is
# (user, client), not any single workspace, so deleting a workspace must not
# revoke a token that still authorises the user elsewhere. oauth_consents keeps
# its per-(user, client, workspace) row and IS purged.
GLOBAL_TABLES = frozenset(
    {
        "api_tokens",
        "app_metadata",
        "auth_login_transactions",
        "email_outbox",
        "oauth_access_token_jti",
        "oauth_clients",
        "oauth_codes",
        "oauth_refresh_tokens",
        "password_recovery_rate_limit_slots",
        "password_reset_tokens",
        "server_admins",
        "users",
        "user_identities",
        "workspace",
    }
)

# Workspace-scoped but deliberately kept. audit_event.workspace_id is
# ON DELETE SET NULL: the append-only record of who did what outlives the
# workspace it describes, and the delete dialog tells the owner so. Counting or
# deleting these rows would make deletion unreachable again for exactly the
# reason WS-02 removed them from the blocker set.
PRESERVED_TABLES = frozenset({"audit_event"})

# Owned by the terminal teardown that runs immediately after this purge:
# WorkspaceWriteService._delete_seed_workspace and _delete_empty_media_scope.
# The media rows in particular must survive the purge -- _delete_empty_media_
# scope re-checks the write-intent fence it took before the purge ran, and
# deleting the fence row here would strand that check on a detached object.
TERMINAL_TEARDOWN_TABLES = frozenset(
    {
        "context_media_logical_quota_usage",
        "library_logical_quota_usage",
        "tree_policy",
        "workspace_media_quota_usage",
        "workspace_media_write_intent",
        "workspace_memberships",
        "workspace_tree_node",
    }
)


class WorkspacePurgePlanError(RuntimeError):
    """The schema no longer admits a safe workspace-purge order."""


PurgeValue = bool | None


@dataclass(frozen=True)
class PurgeUpdate:
    """One atomic pre-delete update for rows in a workspace."""

    table_name: str
    values: tuple[tuple[str, PurgeValue], ...]

    def as_values(self) -> dict[str, PurgeValue]:
        return dict(self.values)


@dataclass(frozen=True)
class PurgeCycleBreak:
    """A semantically safe way to remove one nullable cyclic FK edge."""

    table_name: str
    parent_table_name: str
    nullable_columns: tuple[str, ...]
    companion_values: tuple[tuple[str, PurgeValue], ...] = ()

    @property
    def key(self) -> tuple[str, str, tuple[str, ...]]:
        return (
            self.table_name,
            self.parent_table_name,
            self.nullable_columns,
        )

    def update_values(self) -> dict[str, PurgeValue]:
        values: dict[str, PurgeValue] = {
            column: None for column in self.nullable_columns
        }
        values.update(self.companion_values)
        return values


@dataclass(frozen=True)
class _ForeignKeyEdge:
    """One metadata FK that can constrain the workspace delete order."""

    table_name: str
    parent_table_name: str
    nullable_columns: tuple[str, ...]

    @property
    def policy_key(self) -> tuple[str, str, tuple[str, ...]]:
        return (
            self.table_name,
            self.parent_table_name,
            self.nullable_columns,
        )


# These are lifecycle policies, not an ordering list.  The schema determines
# which ones are needed for a particular purge; a policy merely proves that
# setting its nullable columns to NULL is a valid intermediate state.
#
# The page public-exposure grant is deliberately a single update. Its CHECK
# constraint requires the flag, scope, and exposed-version pointer to change
# together, so clearing just the foreign-key column would be invalid.
CYCLE_BREAK_POLICIES = (
    PurgeCycleBreak(
        "agent_page",
        "agent_page_version",
        ("current_active_version_id",),
    ),
    PurgeCycleBreak(
        "agent_page",
        "agent_page_version",
        ("latest_draft_version_id",),
    ),
    PurgeCycleBreak(
        "context_page",
        "context_page_version",
        ("current_published_version_id",),
    ),
    PurgeCycleBreak(
        "context_page",
        "context_page_version",
        ("public_exposed_version_id",),
        companion_values=(
            ("is_public", False),
            ("public_exposure_scope", None),
        ),
    ),
    PurgeCycleBreak(
        "context_page",
        "context_page_version",
        ("latest_draft_version_id",),
    ),
    PurgeCycleBreak(
        "context_page_version",
        "context_page_version",
        ("parent_version_id",),
    ),
    PurgeCycleBreak(
        "context_page_version",
        "context_page_version",
        ("merged_into_version_id",),
    ),
    PurgeCycleBreak(
        "library_attempt",
        "library_attempt",
        ("parent_attempt_id",),
    ),
    PurgeCycleBreak(
        "library_item",
        "library_item_version",
        ("current_preview_version_id",),
    ),
    PurgeCycleBreak(
        "library_item",
        "library_item_version",
        ("current_ready_version_id",),
    ),
    PurgeCycleBreak(
        "library_item",
        "library_item_version",
        ("current_version_id",),
    ),
    PurgeCycleBreak(
        "library_item_version",
        "library_attempt",
        ("selected_conversion_attempt_id",),
    ),
    PurgeCycleBreak(
        "library_item_version",
        "library_attempt",
        ("selected_preview_attempt_id",),
    ),
    PurgeCycleBreak(
        "library_item_version",
        "library_semantic_overlay",
        ("selected_semantic_overlay_id",),
    ),
    PurgeCycleBreak(
        "library_item_version",
        "library_item_version",
        ("previous_version_id",),
    ),
    PurgeCycleBreak(
        "workspace_tree_node",
        "workspace_tree_node",
        ("parent_id",),
    ),
)


def purgeable_tables() -> frozenset[str]:
    """Workspace-scoped tables this module deletes."""

    return frozenset(
        name
        for name in Base.metadata.tables
        if name not in GLOBAL_TABLES
        and name not in PRESERVED_TABLES
        and name not in TERMINAL_TEARDOWN_TABLES
    )


def unscoped_tables(metadata: MetaData | None = None) -> set[str]:
    """Tables the purge cannot reach: no workspace_id, not declared global.

    The scoping gate in tests/static/test_workspace_purge_coverage.py calls this
    rather than re-deriving the rule, so the gate and the purge can never
    disagree about what "workspace-scoped" means.
    """

    metadata = Base.metadata if metadata is None else metadata
    return {
        name
        for name, table in metadata.tables.items()
        if "workspace_id" not in table.c and name not in GLOBAL_TABLES
    }


@lru_cache(maxsize=1)
def _build_plan() -> tuple[tuple[PurgeUpdate, ...], tuple[str, ...]]:
    """The plan for the live schema, computed once."""

    return build_purge_plan(Base.metadata)


def _foreign_key_edges(
    metadata: MetaData,
    scoped: set[str],
) -> tuple[_ForeignKeyEdge, ...]:
    edges: list[_ForeignKeyEdge] = []
    for table_name in sorted(scoped):
        table = metadata.tables[table_name]
        for constraint in table.foreign_key_constraints:
            parent_table_name = constraint.referred_table.name
            if parent_table_name not in scoped:
                # A global parent (such as workspace or users) survives this
                # purge, so it never constrains an order among owned rows.
                continue
            nullable_columns = tuple(
                column.name for column in constraint.columns if column.nullable
            )
            edges.append(
                _ForeignKeyEdge(
                    table_name=table_name,
                    parent_table_name=parent_table_name,
                    nullable_columns=nullable_columns,
                )
            )
    return tuple(edges)


def _cyclic_components(
    nodes: set[str],
    edges: Iterable[_ForeignKeyEdge],
) -> tuple[frozenset[str], ...]:
    """Return SCCs that still contain a directed foreign-key cycle."""

    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    for edge in edges:
        adjacency[edge.table_name].add(edge.parent_table_name)

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    active: set[str] = set()
    components: list[frozenset[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        active.add(node)

        for parent in sorted(adjacency[node]):
            if parent not in indices:
                visit(parent)
                lowlinks[node] = min(lowlinks[node], lowlinks[parent])
            elif parent in active:
                lowlinks[node] = min(lowlinks[node], indices[parent])

        if lowlinks[node] != indices[node]:
            return
        component: set[str] = set()
        while True:
            member = stack.pop()
            active.remove(member)
            component.add(member)
            if member == node:
                break
        frozen = frozenset(component)
        if len(frozen) > 1 or any(member in adjacency[member] for member in frozen):
            components.append(frozen)

    for node in sorted(nodes):
        if node not in indices:
            visit(node)
    return tuple(components)


def _policy_map(
    cycle_breaks: Iterable[PurgeCycleBreak],
) -> dict[tuple[str, str, tuple[str, ...]], PurgeCycleBreak]:
    policies: dict[tuple[str, str, tuple[str, ...]], PurgeCycleBreak] = {}
    for policy in cycle_breaks:
        if not policy.nullable_columns:
            raise WorkspacePurgePlanError(
                "workspace purge cycle-break policy has no nullable columns: "
                f"{policy.table_name} -> {policy.parent_table_name}"
            )
        if policy.key in policies:
            raise WorkspacePurgePlanError(
                "workspace purge has duplicate cycle-break policies for "
                f"{policy.table_name} -> {policy.parent_table_name}"
            )
        policies[policy.key] = policy
    return policies


def _parents_first_order(
    scoped: set[str],
    edges: Iterable[_ForeignKeyEdge],
) -> list[str]:
    parents: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        parents[edge.table_name].add(edge.parent_table_name)

    ordered: list[str] = []
    remaining = set(scoped)
    while remaining:
        ready = sorted(name for name in remaining if not (parents[name] & remaining))
        if not ready:
            raise WorkspacePurgePlanError(
                "workspace purge cannot order these tables after applying "
                "cycle-break policies: " + ", ".join(sorted(remaining))
            )
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def build_purge_plan(
    metadata: MetaData,
    *,
    cycle_breaks: Iterable[PurgeCycleBreak] = CYCLE_BREAK_POLICIES,
) -> tuple[tuple[PurgeUpdate, ...], tuple[str, ...]]:
    """Return (pre-delete updates, delete order) for the supplied schema.

    Every scoped foreign key participates in ordering. Only a nullable edge in
    a real SCC may be removed, and then only when its lifecycle policy says the
    resulting row state is valid. Tests can supply a small synthetic policy set
    to prove both the accepted and rejected cycle cases.
    """

    # Scoped for ordering purposes includes the terminal-teardown tables: they
    # are still referenced by rows this purge deletes, so they have to take
    # part in the sort even though the purge itself leaves them alone.
    scoped = {
        name
        for name in metadata.tables
        if name not in GLOBAL_TABLES and name not in PRESERVED_TABLES
    }
    policies = _policy_map(cycle_breaks)
    remaining_edges = list(_foreign_key_edges(metadata, scoped))
    update_values: dict[str, dict[str, PurgeValue]] = defaultdict(dict)
    consumed_policy_keys: set[tuple[str, str, tuple[str, ...]]] = set()

    while components := _cyclic_components(scoped, remaining_edges):
        breakable: list[_ForeignKeyEdge] = []
        for edge in remaining_edges:
            if not edge.nullable_columns:
                continue
            if not any(
                edge.table_name in component
                and edge.parent_table_name in component
                for component in components
            ):
                continue
            if edge.policy_key in policies:
                breakable.append(edge)

        if not breakable:
            tables = sorted({name for component in components for name in component})
            raise WorkspacePurgePlanError(
                "workspace purge cannot break a foreign-key cycle without an "
                "explicit teardown policy: " + ", ".join(tables)
            )

        for edge in breakable:
            policy = policies[edge.policy_key]
            consumed_policy_keys.add(edge.policy_key)
            values = update_values[edge.table_name]
            for column, value in policy.update_values().items():
                if column in values and values[column] != value:
                    raise WorkspacePurgePlanError(
                        "workspace purge cycle-break policies disagree about "
                        f"{edge.table_name}.{column}"
                    )
                values[column] = value
        broken = set(breakable)
        remaining_edges = [edge for edge in remaining_edges if edge not in broken]

    # A policy is only ever consulted through `edge.policy_key in policies`,
    # so one whose table, parent, or nullable-column tuple no longer matches a
    # live foreign key is simply never looked up -- it goes dead in silence.
    # That is the failure this module's docstring argues against for the table
    # order ("deriving beats a frozen list because a table added later is
    # ordered automatically instead of being silently skipped"); a frozen
    # policy list earns the same treatment. Concretely: rename or drop the
    # column behind the public-exposure policy and the companion values that
    # keep `context_page_public_exposure_check` satisfiable while its cycle is
    # open stop being applied -- and the purge starts failing that CHECK
    # mid-delete instead of failing here, at plan time, where every test can
    # see it.
    unmatched = sorted(set(policies) - consumed_policy_keys)
    if unmatched:
        raise WorkspacePurgePlanError(
            "workspace purge cycle-break policies no longer match any foreign "
            "key (schema changed under them?): "
            + ", ".join(
                f"{table}.{'+'.join(columns)} -> {parent}"
                for table, parent, columns in unmatched
            )
        )

    parents_first = _parents_first_order(scoped, remaining_edges)

    skipped = GLOBAL_TABLES | PRESERVED_TABLES | TERMINAL_TEARDOWN_TABLES
    order = tuple(name for name in reversed(parents_first) if name not in skipped)
    updates = tuple(
        PurgeUpdate(
            table_name=table_name,
            values=tuple(sorted(values.items())),
        )
        for table_name, values in sorted(update_values.items())
        if table_name not in skipped
    )
    return updates, order


def purge_workspace_data(session: Session, workspace_id: UUID) -> dict[str, int]:
    """Delete every content and operational row the workspace owns.

    Returns per-table deleted row counts, which the caller records in the audit
    trail. Seed scaffolding (tree, memberships, media quota fence) is left to
    the terminal teardown that runs next and already owns it.
    """

    metadata = Base.metadata
    updates, order = _build_plan()
    for planned_update in updates:
        table = metadata.tables[planned_update.table_name]
        session.execute(
            update(table)
            .where(table.c.workspace_id == workspace_id)
            .values(planned_update.as_values())
        )
    session.flush()

    deleted: dict[str, int] = {}
    for table_name in order:
        table = metadata.tables[table_name]
        result = session.execute(
            delete(table).where(table.c.workspace_id == workspace_id)
        )
        count = result.rowcount or 0
        if count:
            deleted[table_name] = count
    session.flush()
    return deleted
