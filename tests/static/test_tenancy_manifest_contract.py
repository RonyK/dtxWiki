"""Executable Epic-3 table-plane and tenant-edge contract."""

from __future__ import annotations

import re

from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import AddConstraint, CreateIndex

import backend.models  # noqa: F401 -- populate Base.registry/Base.metadata
from backend.models.base import Base
from backend.models.search_index import PAGE_NAME_TRGM_TABLE, SEARCH_DOC_FTS_TABLE
from backend.tenancy_manifest import (
    ALL_CLASSIFIED_TABLES,
    CONTROL_PLANE_TABLES,
    MIXED_PLANE_TABLES,
    WORKSPACE_PLANE_TABLES,
    WORKSPACE_RAW_SQL_TABLES,
)


def test_every_mapped_table_has_exactly_one_explicit_plane():
    mapped = frozenset(Base.metadata.tables)
    assert ALL_CLASSIFIED_TABLES == mapped
    assert CONTROL_PLANE_TABLES.isdisjoint(MIXED_PLANE_TABLES)
    assert CONTROL_PLANE_TABLES.isdisjoint(WORKSPACE_PLANE_TABLES)
    assert MIXED_PLANE_TABLES.isdisjoint(WORKSPACE_PLANE_TABLES)


def test_workspace_plane_tables_have_materialized_non_null_workspace_id():
    violations = []
    for table_name in sorted(WORKSPACE_PLANE_TABLES):
        table = Base.metadata.tables[table_name]
        if "workspace_id" not in table.c or table.c.workspace_id.nullable:
            violations.append(table_name)
    assert violations == []


def test_raw_sql_workspace_storage_objects_are_explicitly_classified():
    assert WORKSPACE_RAW_SQL_TABLES == {
        PAGE_NAME_TRGM_TABLE,
        SEARCH_DOC_FTS_TABLE,
    }
    assert WORKSPACE_RAW_SQL_TABLES.isdisjoint(Base.metadata.tables)


def test_all_named_schema_objects_compile_for_postgres_identifier_limits():
    dialect = postgresql.dialect()
    compiled = []
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if constraint.name is not None:
                # isolate_from_table=False: by default AddConstraint marks the
                # shared constraint as created separately, so every later
                # create_all in this process would omit it from CREATE TABLE.
                statement = AddConstraint(constraint, isolate_from_table=False)
                compiled.append(str(statement.compile(dialect=dialect)))
        for index in table.indexes:
            if index.name is not None:
                compiled.append(str(CreateIndex(index).compile(dialect=dialect)))
    assert compiled


def test_every_workspace_plane_fk_edge_enforces_same_workspace():
    """Tenant-scope check for edges BETWEEN shard-resident tables only.

    The ``continue`` below skips control-plane parents deliberately, and the
    reason is structural rather than a judgement call: ``_edge_is_workspace_
    scoped`` can only answer "same workspace?" by matching a ``workspace_id``
    on both ends, and neither ``workspace`` nor ``users`` HAS a
    ``workspace_id`` column (asserted below, so this paragraph cannot quietly
    stop being true). For an edge into ``workspace`` the parent's own ``id``
    is the workspace identity, so the question is malformed, not unanswered.

    Those skipped edges are not unexamined, and this test is not the record
    of them: they are inventoried edge-by-edge, counted, and tied to the gate
    on enabling a per-workspace physical database in
    tests/static/test_workspace_physical_split_census.py. Widening this test
    to cover them would report every one of them as a violation of a property
    they cannot have; read the census instead.
    """

    for parent_name in ("workspace", "users"):
        assert "workspace_id" not in Base.metadata.tables[parent_name].c

    violations = []
    tenant_parents = WORKSPACE_PLANE_TABLES | MIXED_PLANE_TABLES
    for child_name in sorted(WORKSPACE_PLANE_TABLES):
        child = Base.metadata.tables[child_name]
        for foreign_key in child.foreign_key_constraints:
            if foreign_key.referred_table.name not in tenant_parents:
                continue
            if not _edge_is_workspace_scoped(child, foreign_key):
                violations.append(
                    f"{child_name}.{foreign_key.name or '<unnamed>'} -> "
                    f"{foreign_key.referred_table.name}"
                )
    assert violations == []


def _edge_is_workspace_scoped(child, foreign_key) -> bool:
    pairs = _column_pairs(foreign_key)
    if ("workspace_id", "workspace_id") in pairs:
        return True

    # A legacy single-column FK is harmless when a stronger composite FK on
    # the same columns and parent subsumes it (Library ready/preview/current
    # pointers retain their old SET NULL FK for compatibility).
    for candidate in child.foreign_key_constraints:
        if candidate is foreign_key:
            continue
        if candidate.referred_table is not foreign_key.referred_table:
            continue
        candidate_pairs = _column_pairs(candidate)
        if ("workspace_id", "workspace_id") not in candidate_pairs:
            continue
        if pairs.issubset(candidate_pairs):
            return True

    # Blob locators retain a separately named workspace column because the
    # tuple is part of their durable external identity. A CHECK tying that
    # locator workspace to the owning row is equivalent to using the owning
    # workspace_id directly in the FK.
    aliases = {
        local
        for local, remote in pairs
        if remote == "workspace_id" and local != "workspace_id"
    }
    if aliases and all(
        _has_workspace_equality_check(child, alias) for alias in aliases
    ):
        return True

    # A-1: each of the six Library "detail" 1:1 extension tables carries an
    # extra narrow FK back to its own parent purely to prove `stage`
    # agreement (e.g. library_artifact_conversion_detail's
    # [artifact_id, stage] -> [library_artifact.id, library_artifact.stage]).
    # That FK is harmless even though it omits workspace_id: it shares its
    # own local `*_id` column with a SIBLING FK to the SAME parent that IS
    # workspace-scoped, and that shared column maps to the parent's primary
    # key column in both FKs. A primary key uniquely identifies one row, so
    # the two FKs are necessarily talking about the exact same parent row --
    # the stage FK cannot possibly resolve to a different (and therefore
    # differently-workspaced) row than the one the workspace-scoped sibling
    # already pins down.
    parent_pk_columns = {c.name for c in foreign_key.referred_table.primary_key}
    shared_pk_pairs = {
        (local, remote) for local, remote in pairs if remote in parent_pk_columns
    }
    if shared_pk_pairs:
        for candidate in child.foreign_key_constraints:
            if candidate is foreign_key:
                continue
            if candidate.referred_table is not foreign_key.referred_table:
                continue
            candidate_pairs = _column_pairs(candidate)
            if ("workspace_id", "workspace_id") not in candidate_pairs:
                continue
            if shared_pk_pairs & candidate_pairs:
                return True

    # A-1: library_run_preview_detail.fk_library_run_preview_detail_expected_
    # attempt is the one exception that isn't the same-parent stage-proof
    # shape above -- it points at library_attempt while this table's own
    # workspace-scoped FK points at library_run instead. It is still safe,
    # transitively, through the shared `run_id` value: this table's own
    # workspace-scoped FK proves `run_id` names a library_run row in this
    # row's workspace, and library_attempt's OWN
    # fk_library_attempt_run_id_workspace_scoped FK (asserted below, not
    # assumed) proves any library_attempt row with that same `run_id`
    # necessarily has a matching workspace_id. So the expected-attempt
    # pointer cannot land in a different workspace than this row's own,
    # even though no single FK on this table says so directly.
    if foreign_key.name == "fk_library_run_preview_detail_expected_attempt":
        uses_run_id = any(local == "run_id" for local, _remote in pairs)
        shares_run_id_with_workspace_scoped_sibling = any(
            candidate is not foreign_key
            and ("workspace_id", "workspace_id") in _column_pairs(candidate)
            and any(
                local == "run_id" for local, _remote in _column_pairs(candidate)
            )
            for candidate in child.foreign_key_constraints
        )
        attempt_table = foreign_key.referred_table
        attempt_proves_workspace_from_run_id = any(
            ("workspace_id", "workspace_id") in _column_pairs(attempt_fk)
            and any(local == "run_id" for local, _remote in _column_pairs(attempt_fk))
            for attempt_fk in attempt_table.foreign_key_constraints
        )
        if uses_run_id and shares_run_id_with_workspace_scoped_sibling and (
            attempt_proves_workspace_from_run_id
        ):
            return True

    return False


def _column_pairs(foreign_key) -> frozenset[tuple[str, str]]:
    return frozenset(
        (element.parent.name, element.column.name) for element in foreign_key.elements
    )


def _has_workspace_equality_check(table, alias: str) -> bool:
    for constraint in table.constraints:
        if not isinstance(constraint, CheckConstraint):
            continue
        sql = re.sub(r"[\s()]", "", str(constraint.sqltext).lower())
        if f"{alias.lower()}=workspace_id" in sql:
            return True
        if f"workspace_id={alias.lower()}" in sql:
            return True
    return False
