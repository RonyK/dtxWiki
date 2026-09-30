"""Every table must be accounted for by the workspace purge.

The purge derives its delete order from the live metadata, so a table added
later is ordered automatically -- but only if it is *scoped*. A new table
without ``workspace_id`` would be skipped in silence, leaving rows behind after
an owner asked for the workspace and its contents to be destroyed. These
assertions turn that into a failing gate at the moment the table is added.
"""

import importlib
import pkgutil

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table


@pytest.fixture(scope="module")
def metadata():
    # Import the app and every model module before reading the metadata.
    # Importing a subset yields a partial table set, which silently understates
    # coverage -- reading a short list and calling it complete is exactly the
    # failure this gate exists to prevent.
    importlib.import_module("backend.app.main")
    models_pkg = importlib.import_module("backend.models")
    for module in pkgutil.iter_modules(models_pkg.__path__):
        importlib.import_module(f"backend.models.{module.name}")
    return importlib.import_module("backend.models.base").Base.metadata


@pytest.fixture(scope="module")
def purge(metadata):
    return importlib.import_module("backend.services.workspace_purge")


def test_every_table_is_either_workspace_scoped_or_declared_global(metadata, purge):
    # Calls the production classifier rather than re-deriving the rule here: a
    # gate that reimplements what it checks passes whatever the shipped copy
    # does, which is the defect class the counterexample registry exists for.
    unscoped = purge.unscoped_tables(metadata)
    assert not unscoped, (
        "these tables carry no workspace_id and are not declared global, so a "
        "workspace purge cannot reach their rows: " + ", ".join(sorted(unscoped))
    )


def test_global_table_list_has_no_stale_entries(metadata, purge):
    unknown = purge.GLOBAL_TABLES - set(metadata.tables)
    assert not unknown, (
        "GLOBAL_TABLES names tables that no longer exist: " + ", ".join(sorted(unknown))
    )


def test_declared_global_tables_really_are_unscoped(metadata, purge):
    # A table that gains workspace_id later must be purged, not skipped. Without
    # this the entry would keep exempting it and the rows would survive.
    scoped_but_exempt = {
        name
        for name in purge.GLOBAL_TABLES
        if name in metadata.tables and "workspace_id" in metadata.tables[name].c
    }
    assert not scoped_but_exempt, (
        "these tables now carry workspace_id and must be removed from "
        "GLOBAL_TABLES so the purge deletes them: "
        + ", ".join(sorted(scoped_but_exempt))
    )


def test_preserved_and_terminal_tables_exist_and_are_scoped(metadata, purge):
    declared = purge.PRESERVED_TABLES | purge.TERMINAL_TEARDOWN_TABLES
    missing = declared - set(metadata.tables)
    assert not missing, "unknown table names: " + ", ".join(sorted(missing))
    unscoped = {
        name for name in declared if "workspace_id" not in metadata.tables[name].c
    }
    assert not unscoped, (
        "these are handled as workspace-scoped but have no workspace_id: "
        + ", ".join(sorted(unscoped))
    )


def test_purge_plan_orders_every_purgeable_table(purge):
    # Also the guard against a NOT NULL foreign-key cycle: _build_plan raises
    # WorkspacePurgePlanError rather than emitting a partial order.
    _nulling, order = purge._build_plan()
    assert len(set(order)) == len(order), "a table is deleted twice"
    assert set(order) == purge.purgeable_tables()


def test_purge_updates_set_none_only_on_nullable_columns(metadata, purge):
    updates, _order = purge._build_plan()
    not_nullable = [
        f"{planned_update.table_name}.{column}"
        for planned_update in updates
        for column, value in planned_update.values
        if value is None
        and not metadata.tables[planned_update.table_name].c[column].nullable
    ]
    assert not not_nullable, (
        "the purge would try to NULL a NOT NULL column: " + ", ".join(not_nullable)
    )


def test_cycle_break_policies_match_live_foreign_keys(metadata, purge):
    foreign_key_groups = {
        (
            table_name,
            constraint.referred_table.name,
            tuple(column.name for column in constraint.columns if column.nullable),
        )
        for table_name, table in metadata.tables.items()
        for constraint in table.foreign_key_constraints
    }
    configured = {policy.key for policy in purge.CYCLE_BREAK_POLICIES}
    assert configured <= foreign_key_groups


@pytest.mark.counterexample(
    "build_purge_plan raises when a declared cycle-break policy matched no "
    "foreign key during planning -- the whole point is that a policy which "
    "silently stops applying is worse than one that never existed"
)
def test_build_purge_plan_rejects_a_policy_that_matched_nothing(metadata, purge):
    """A policy naming a column the schema no longer has must fail at plan time.

    test_cycle_break_policies_match_live_foreign_keys already pins that every
    declared policy corresponds to a live FK. This covers the other half: a
    policy is only ever consulted through `edge.policy_key in policies`, so one
    that matches no edge the planner actually walks is never looked up and goes
    dead in silence -- taking its companion values with it. For the public-page
    grant those companion values are what keep
    `context_page_public_exposure_check` satisfiable while its cycle is open,
    so losing them turns a plan-time failure into a CHECK violation part-way
    through a live delete.
    """

    stale = purge.PurgeCycleBreak(
        table_name="context_page",
        parent_table_name="context_page_version",
        nullable_columns=("column_that_no_longer_exists",),
    )
    with pytest.raises(purge.WorkspacePurgePlanError) as excinfo:
        purge.build_purge_plan(
            metadata,
            cycle_breaks=(*purge.CYCLE_BREAK_POLICIES, stale),
        )
    assert "no longer match any foreign key" in str(excinfo.value)
    assert "column_that_no_longer_exists" in str(excinfo.value)


@pytest.mark.counterexample(
    "unscoped_tables reports a table that carries no workspace_id and is not "
    "declared global, and stays silent for one that carries it -- both halves "
    "die if the workspace_id membership test is dropped"
)
def test_unscoped_tables_reports_a_table_the_purge_could_not_reach(purge):
    """Hands the production classifier a schema that violates the rule."""

    violating = MetaData()
    Table(
        "scoped_thing",
        violating,
        Column("id", Integer, primary_key=True),
        Column("workspace_id", Integer, nullable=False),
    )
    Table(
        "orphan_thing",
        violating,
        Column("id", Integer, primary_key=True),
    )

    reported = purge.unscoped_tables(violating)

    assert reported == {"orphan_thing"}


@pytest.mark.counterexample(
    "build_purge_plan raises WorkspacePurgePlanError on a NOT NULL foreign-key "
    "cycle instead of returning a partial order that would strand rows"
)
def test_build_purge_plan_refuses_an_unbreakable_cycle(purge):
    cyclic = MetaData()
    Table(
        "left_side",
        cyclic,
        Column("id", Integer, primary_key=True),
        Column("workspace_id", Integer, nullable=False),
        Column("right_id", Integer, ForeignKey("right_side.id"), nullable=False),
    )
    Table(
        "right_side",
        cyclic,
        Column("id", Integer, primary_key=True),
        Column("workspace_id", Integer, nullable=False),
        Column("left_id", Integer, ForeignKey("left_side.id"), nullable=False),
    )

    with pytest.raises(purge.WorkspacePurgePlanError) as excinfo:
        purge.build_purge_plan(cyclic, cycle_breaks=())

    # The message has to name the tables, or the failure is unactionable.
    assert "left_side" in str(excinfo.value)
    assert "right_side" in str(excinfo.value)


def _nullable_cycle_metadata():
    breakable = MetaData()
    Table(
        "parent_row",
        breakable,
        Column("id", Integer, primary_key=True),
        Column("workspace_id", Integer, nullable=False),
        Column("current_child_id", Integer, ForeignKey("child_row.id"), nullable=True),
    )
    Table(
        "child_row",
        breakable,
        Column("id", Integer, primary_key=True),
        Column("workspace_id", Integer, nullable=False),
        Column("parent_id", Integer, ForeignKey("parent_row.id"), nullable=False),
    )
    return breakable


@pytest.mark.counterexample(
    "an acyclic nullable FK must remain a delete-order edge, so clearing every "
    "nullable FK cannot silently corrupt a CHECK-constrained child row"
)
def test_build_purge_plan_keeps_an_acyclic_nullable_foreign_key(purge):
    acyclic = MetaData()
    Table(
        "parent_row",
        acyclic,
        Column("id", Integer, primary_key=True),
        Column("workspace_id", Integer, nullable=False),
    )
    Table(
        "child_row",
        acyclic,
        Column("id", Integer, primary_key=True),
        Column("workspace_id", Integer, nullable=False),
        Column("parent_id", Integer, ForeignKey("parent_row.id"), nullable=True),
    )

    updates, order = purge.build_purge_plan(acyclic, cycle_breaks=())

    assert updates == ()
    assert order.index("child_row") < order.index("parent_row")


@pytest.mark.counterexample(
    "a nullable cycle cannot be cleared merely because its column accepts NULL; "
    "the planner must require an explicit lifecycle policy"
)
def test_build_purge_plan_rejects_an_unknown_nullable_cycle(purge):
    with pytest.raises(purge.WorkspacePurgePlanError) as excinfo:
        purge.build_purge_plan(_nullable_cycle_metadata(), cycle_breaks=())

    assert "parent_row" in str(excinfo.value)
    assert "child_row" in str(excinfo.value)


@pytest.mark.counterexample(
    "a registered nullable cycle break must emit its one planned update before "
    "the child-first delete order is calculated"
)
def test_build_purge_plan_breaks_a_cycle_through_its_declared_policy(purge):
    breakable = _nullable_cycle_metadata()

    updates, order = purge.build_purge_plan(
        breakable,
        cycle_breaks=(
            purge.PurgeCycleBreak(
                "parent_row",
                "child_row",
                ("current_child_id",),
            ),
        ),
    )

    assert len(updates) == 1
    assert updates[0].table_name == "parent_row"
    assert updates[0].as_values() == {"current_child_id": None}
    assert order.index("child_row") < order.index("parent_row")


def test_build_purge_plan_refuses_a_not_null_self_reference(purge):
    cyclic = MetaData()
    Table(
        "self_referential_row",
        cyclic,
        Column("id", Integer, primary_key=True),
        Column("workspace_id", Integer, nullable=False),
        Column(
            "parent_id",
            Integer,
            ForeignKey("self_referential_row.id"),
            nullable=False,
        ),
    )

    with pytest.raises(purge.WorkspacePurgePlanError) as excinfo:
        purge.build_purge_plan(cyclic, cycle_breaks=())

    assert "self_referential_row" in str(excinfo.value)


def test_public_page_cycle_update_clears_the_complete_exposure_grant(purge):
    updates, _order = purge._build_plan()
    context_page_update = next(
        planned_update
        for planned_update in updates
        if planned_update.table_name == "context_page"
    )
    values = context_page_update.as_values()

    assert {
        "is_public": values["is_public"],
        "public_exposure_scope": values["public_exposure_scope"],
        "public_exposed_version_id": values["public_exposed_version_id"],
    } == {
        "is_public": False,
        "public_exposure_scope": None,
        "public_exposed_version_id": None,
    }
