"""Publish a parent's cycle-breaking unique key in the form its dialect needs.

``context_page`` <-> ``context_page_version``, ``agent_page`` <->
``agent_page_version`` and ``library_item`` <-> ``library_item_version`` are
reciprocal foreign-key pairs: each child's workspace-scoped FK needs the
parent's composite unique key to exist before the parent's own table batch has
run. The two supported databases need opposite things here, and getting it
wrong is invisible on one of them:

* **SQLite** cannot ``ALTER TABLE ... ADD CONSTRAINT`` at all outside a batch
  rebuild (alembic raises ``NotImplementedError: No support for ALTER of
  constraints in SQLite dialect``), so the parent key has to be published as a
  standalone UNIQUE INDEX and dropped again once the durable constraint has
  been created inside the parent's batch. SQLite records no dependency from an
  FK onto an index, so that drop always succeeds.
* **PostgreSQL** binds each foreign key to the *index* backing the parent key
  it references, as a hard catalog dependency. Publishing a throwaway index
  therefore makes every child FK depend on it, and the later
  ``DROP INDEX`` aborts with SQLSTATE 2BP01 -- ``if_exists=True`` does not
  help, because the object exists and it is the dependency that blocks. The
  parent key must be created up front as the *durable* named UNIQUE
  CONSTRAINT, and the parent's own batch must then not create it a second
  time.

The same asymmetry runs in reverse on downgrade: SQLite drops the parent key
inside the parent's one table rebuild (a second standalone batch would violate
the one-rebuild-per-table invariant pinned by
``test_epic1_rebuilds_each_sqlite_table_at_most_once_per_direction``), while
PostgreSQL must defer that drop until every child FK referencing it is gone.

Every helper here is stateless: callers pass what they know. Module-level
bookkeeping would survive an upgrade/downgrade round trip inside one process,
which is exactly what ``test_postgres_migration.py`` does.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


def uses_temporary_unique_indexes() -> bool:
    """True on the dialect that wants a throwaway index (SQLite)."""

    return op.get_bind().dialect.name == "sqlite"


def publish_parent_unique_key(
    table_name: str,
    constraint_name: str,
    columns: Sequence[str],
    temp_index_name: str,
) -> None:
    """Make the parent key visible to child FKs before the parent's batch."""

    if uses_temporary_unique_indexes():
        op.create_index(temp_index_name, table_name, list(columns), unique=True)
    else:
        op.create_unique_constraint(constraint_name, table_name, list(columns))


def retire_parent_unique_key(table_name: str, temp_index_name: str) -> None:
    """Drop the scaffolding index. A no-op where nothing temporary was made."""

    if uses_temporary_unique_indexes():
        op.drop_index(temp_index_name, table_name=table_name, if_exists=True)


def create_unique_constraint_once(
    batch_op,
    constraint_name: str,
    columns: Sequence[str],
    *,
    already_published: bool,
) -> None:
    """Create the durable key unless the prepass already created it."""

    if already_published and not uses_temporary_unique_indexes():
        return
    batch_op.create_unique_constraint(constraint_name, list(columns))


def drop_unique_constraint_once(
    batch_op,
    constraint_name: str,
    *,
    defer_to_retract: bool,
) -> None:
    """Drop the parent key inline, unless this dialect must defer it.

    ``defer_to_retract`` says the caller will call
    :func:`retract_parent_unique_key` for this constraint after every batch
    that could still hold a dependent child FK has run.
    """

    if defer_to_retract and not uses_temporary_unique_indexes():
        return
    batch_op.drop_constraint(constraint_name, type_="unique")


def retract_parent_unique_key(table_name: str, constraint_name: str) -> None:
    """Drop a deferred parent key. A no-op where the batch already dropped it."""

    if uses_temporary_unique_indexes():
        return
    op.drop_constraint(constraint_name, table_name, type_="unique")


def drop_column_once(
    batch_op,
    column_name: str,
    *,
    defer_to_retract: bool,
) -> None:
    """Drop a routing column inline, unless a child FK still references it.

    PostgreSQL refuses ``ALTER TABLE parent DROP COLUMN workspace_id`` while
    another table's foreign key references that column, with the same
    SQLSTATE 2BP01 it raises for the unique key itself.
    """

    if defer_to_retract and not uses_temporary_unique_indexes():
        return
    batch_op.drop_column(column_name)


def retract_column(table_name: str, column_name: str) -> None:
    """Drop a deferred routing column. A no-op on the inline dialect."""

    if uses_temporary_unique_indexes():
        return
    op.drop_column(table_name, column_name)
