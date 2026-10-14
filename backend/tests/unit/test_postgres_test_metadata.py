"""PostgreSQL fixture DDL must not weaken later SQLite test schemas."""

import json
import subprocess
import sys
from contextlib import contextmanager

import pytest
from sqlalchemy import create_mock_engine
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.schema import AddConstraint, CreateIndex, CreateTable

from backend.models.base import Base
from backend.tests.utils import postgres as postgres_helpers
from backend.tests.utils.postgres import create_isolated_postgres_metadata

READY_OWNER_FK = "fk_library_item_current_ready_version_id_workspace_scoped"


def _sqlite_table_definitions():
    return {
        name: str(CreateTable(table).compile(dialect=sqlite_dialect()))
        for name, table in Base.metadata.tables.items()
    }


def _constraint_rules():
    return {
        id(constraint): (
            constraint._create_rule,
            getattr(constraint, "use_alter", None),
        )
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
    }


def test_shared_postgres_creation_reproduces_missing_sqlite_fk_in_child_process():
    # The violating control lives in its own interpreter: deliberately poisoning
    # the real mapped constraints here would contaminate this test's own worker.
    script = """
import json
import backend.models
from sqlalchemy import create_mock_engine
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateTable
from backend.models.base import Base
from backend.models.library import LibraryItem
name = 'fk_library_item_current_ready_version_id_workspace_scoped'
before = str(CreateTable(LibraryItem.__table__).compile(dialect=dialect()))
statements = []
engine = create_mock_engine(
    'postgresql+pg8000://',
    lambda statement, *args, **kwargs: statements.append(type(statement).__name__),
)
Base.metadata.create_all(engine)
after = str(CreateTable(LibraryItem.__table__).compile(dialect=dialect()))
print(json.dumps({
    'before': name in before,
    'after': name in after,
    'alter_count': statements.count('AddConstraint'),
}))
"""
    before = _sqlite_table_definitions()
    child = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result = json.loads(child.stdout)
    assert result["before"] is True
    assert result["after"] is False
    assert result["alter_count"] > 0
    assert _sqlite_table_definitions() == before


def test_postgres_creation_uses_fresh_complete_copies_and_preserves_sqlite_ddl():
    before = _sqlite_table_definitions()
    original_rules = _constraint_rules()
    assert READY_OWNER_FK in before["library_item"]
    cloned_metadata = []

    for _ in range(2):
        statements = []
        engine = create_mock_engine(
            "postgresql+pg8000://",
            lambda statement, *args, _record=statements, **kwargs: _record.append(
                statement
            ),
        )
        create_isolated_postgres_metadata(engine)

        tables = [stmt.element for stmt in statements if isinstance(stmt, CreateTable)]
        altered = [
            stmt.element for stmt in statements if isinstance(stmt, AddConstraint)
        ]
        assert len(tables) == len(Base.metadata.tables)
        assert altered, "the probe must reach cyclic ALTER TABLE constraints"
        clone = tables[0].metadata
        cloned_metadata.append(clone)
        assert clone is not Base.metadata
        assert clone.schema == Base.metadata.schema
        assert clone.naming_convention == Base.metadata.naming_convention
        assert set(clone.tables) == set(Base.metadata.tables)
        assert {
            (
                statement.element.table.fullname,
                str(statement.element.name),
                tuple(str(expression) for expression in statement.element.expressions),
                statement.element.unique,
            )
            for statement in statements
            if isinstance(statement, CreateIndex)
        } == {
            (
                index.table.fullname,
                str(index.name),
                tuple(str(expression) for expression in index.expressions),
                index.unique,
            )
            for table in Base.metadata.tables.values()
            for index in table.indexes
        }

        for name, original in Base.metadata.tables.items():
            copied = clone.tables[name]
            assert copied is not original
            assert len(copied.constraints) == len(original.constraints)
            assert len(copied.indexes) == len(original.indexes)
            for constraint in copied.foreign_key_constraints:
                for foreign_key in constraint.elements:
                    assert foreign_key.column.table.metadata is clone
        ready_owner = next(c for c in altered if c.name == READY_OWNER_FK)
        assert ready_owner._create_rule is not None
        assert ready_owner.table.metadata is clone
        assert _constraint_rules() == original_rules
        assert _sqlite_table_definitions() == before

    assert cloned_metadata[0] is not cloned_metadata[1]
    first = cloned_metadata[0].tables["library_item"].foreign_key_constraints
    second = cloned_metadata[1].tables["library_item"].foreign_key_constraints
    assert {id(constraint) for constraint in first}.isdisjoint(
        id(constraint) for constraint in second
    )


def _assert_pointer_actions_precede_ownership(constraints):
    for pointer in (
        "current_ready_version_id",
        "current_preview_version_id",
        "current_version_id",
    ):
        action_index = next(
            index
            for index, constraint in enumerate(constraints)
            if constraint.table.name == "library_item"
            and list(constraint.column_keys) == [pointer]
            and constraint.ondelete == "SET NULL"
        )
        ownership_index = next(
            index
            for index, constraint in enumerate(constraints)
            if constraint.name == f"fk_library_item_{pointer}_workspace_scoped"
        )
        assert action_index < ownership_index, pointer


def test_postgres_cyclic_fk_order_survives_adversarial_sort_input(monkeypatch):
    """Force the old failure order without relying on allocation or hash seeds."""

    original_sort = postgres_helpers.sort_tables_and_constraints
    bad_orders = []

    def ownership_first(tables):
        plan = original_sort(tables)
        reordered = []
        for table, constraints in plan:
            if table is None:
                constraints = sorted(
                    constraints,
                    key=lambda constraint: (
                        constraint.ondelete == "SET NULL",
                        constraint.table.fullname,
                        tuple(constraint.column_keys),
                    ),
                )
                bad_orders.append(constraints)
            reordered.append((table, constraints))
        return reordered

    # Patch our helper's imported sorting seam, not installed SQLAlchemy or
    # mapped metadata. The input now deterministically has ownership first.
    monkeypatch.setattr(
        postgres_helpers, "sort_tables_and_constraints", ownership_first
    )
    before = _sqlite_table_definitions()
    original_rules = _constraint_rules()
    emissions = []
    for _ in range(2):
        statements = []
        engine = create_mock_engine(
            "postgresql+pg8000://",
            lambda statement, *args, _record=statements, **kwargs: _record.append(
                statement
            ),
        )
        create_isolated_postgres_metadata(engine)
        constraints = [
            statement.element
            for statement in statements
            if isinstance(statement, AddConstraint)
        ]
        _assert_pointer_actions_precede_ownership(constraints)
        emissions.append(
            [
                (
                    constraint.table.fullname,
                    tuple(constraint.column_keys),
                    str(constraint.name),
                    constraint.ondelete,
                )
                for constraint in constraints
            ]
        )
    for bad_order in bad_orders:
        with pytest.raises(AssertionError):
            _assert_pointer_actions_precede_ownership(bad_order)
    assert emissions[0] == emissions[1]
    assert _constraint_rules() == original_rules
    assert _sqlite_table_definitions() == before


@pytest.mark.parametrize("missing_table", [None, "library_item"])
def test_postgres_checkfirst_does_not_readd_existing_table_constraints(
    monkeypatch, missing_table
):
    statements = []
    connection = create_mock_engine(
        "postgresql+pg8000://",
        lambda statement, *args, **kwargs: statements.append(statement),
    )
    lifecycle = []

    class EngineProbe:
        dialect = connection.dialect

        @contextmanager
        def begin(self):
            lifecycle.append("begin")
            yield connection
            lifecycle.append("commit")

    class InspectorProbe:
        def has_table(self, name, schema=None):
            return name != missing_table

    monkeypatch.setattr(postgres_helpers, "inspect", lambda _bind: InspectorProbe())
    create_isolated_postgres_metadata(EngineProbe())
    assert lifecycle == ["begin", "commit"]
    assert {
        statement.element.name
        for statement in statements
        if isinstance(statement, CreateTable)
    } == ({missing_table} if missing_table is not None else set())
    constraints = [
        statement.element
        for statement in statements
        if isinstance(statement, AddConstraint)
    ]
    assert {constraint.table.name for constraint in constraints} == (
        {missing_table} if missing_table is not None else set()
    )
    if missing_table is not None:
        assert len(constraints) == len(
            Base.metadata.tables[missing_table].foreign_key_constraints
        )
        _assert_pointer_actions_precede_ownership(constraints)


def test_postgres_metadata_helper_refuses_sqlite_without_executing_ddl():
    statements = []
    engine = create_mock_engine(
        "sqlite://", lambda statement, *args, **kwargs: statements.append(statement)
    )
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        create_isolated_postgres_metadata(engine)
    assert statements == []
