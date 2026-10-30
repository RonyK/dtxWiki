"""Workspace-scoped reads and mutations for workspace-plane models.

The table must be explicitly classified on the workspace plane and its
workspace scope must be required. Identity-based lookups require the exact
single-column primary key ``id``. Scoped statements and batch mutations also
support composite primary keys because their required workspace predicate
represents the scope independently of each row's identity. Control- or
mixed-plane tables need purpose-specific APIs.
"""

from collections.abc import Collection, Iterable, Mapping
from uuid import UUID

from sqlalchemy import Delete, Select, Update, delete, inspect, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from backend.tenancy_manifest import WORKSPACE_PLANE_TABLES


def get_in_workspace[Model](
    session: Session,
    model: type[Model],
    *,
    workspace_id: UUID,
    resource_id: UUID,
) -> Model | None:
    """Return the ``model`` row identified by ``resource_id`` if -- and only
    if -- it belongs to ``workspace_id``. ``None`` otherwise, whether the row
    doesn't exist at all or exists under a different workspace.

    The explicit plane and shape checks prevent Phase E from mechanically
    routing an incompatible model through an interface that cannot represent
    its real identity or placement.
    """

    _validate_workspace_model(model, operation="get_in_workspace")

    return session.scalar(
        select(model).where(
            model.id == resource_id,  # type: ignore[attr-defined]
            model.workspace_id == workspace_id,  # type: ignore[attr-defined]
        )
    )


def select_in_workspace_by_ids[Model](
    model: type[Model], *, workspace_id: UUID, resource_ids: Collection[UUID]
) -> Select[tuple[Model]]:
    """A scoped batch statement; callers may restrict the selected columns."""
    _validate_workspace_model(model, operation="select_in_workspace_by_ids")
    return select(model).where(
        model.workspace_id == workspace_id,  # type: ignore[attr-defined]
        model.id.in_(resource_ids),  # type: ignore[attr-defined]
    )


def select_in_workspace[Model](
    model: type[Model],
    *,
    workspace_id: UUID,
    criteria: Iterable[ColumnElement[bool]] = (),
    conditions: Iterable[ColumnElement[bool]] = (),
) -> Select[tuple[Model]]:
    """Build a query whose workspace predicate cannot be omitted by its caller.

    Both keyword forms narrow the required scope; even a contradictory
    workspace criterion cannot widen it. This statement does not select by
    identity, so composite primary keys are supported. Existing dev callers
    use conditions and review callers use criteria on this one validated path.
    """
    _validate_workspace_scope(model, operation="select_in_workspace")
    return select(model).where(
        model.workspace_id == workspace_id,  # type: ignore[attr-defined]
        *criteria,
        *conditions,
    )


def _validate_workspace_model(model: type, *, operation: str) -> None:
    _validate_workspace_scope(model, operation=operation)
    mapper = inspect(model)
    primary_key_names = tuple(column.name for column in mapper.primary_key)
    if primary_key_names != ("id",):
        raise TypeError(f"{operation} requires the exact primary key (id)")


def _validate_workspace_scope(model: type, *, operation: str) -> None:
    mapper = inspect(model)
    table = mapper.local_table
    if table.name not in WORKSPACE_PLANE_TABLES:
        raise TypeError(
            f"{operation} requires a table classified in "
            f"WORKSPACE_PLANE_TABLES; {table.name!r} is not workspace-plane"
        )

    if "workspace_id" not in table.c or table.c.workspace_id.nullable:
        raise TypeError(f"{operation} requires a non-null workspace_id column")


def list_in_workspace[Model](
    session: Session,
    model: type[Model],
    *,
    workspace_id: UUID,
    conditions: Iterable[ColumnElement[bool]] = (),
) -> list[Model]:
    """Load one explicitly workspace-bound batch, including foreign-ID misses."""
    return list(
        session.scalars(
            select_in_workspace(model, workspace_id=workspace_id, conditions=conditions)
        )
    )


def where_in_workspace[Model](
    statement: Select,
    model: type[Model],
    *,
    workspace_id: UUID,
    conditions: Iterable[ColumnElement[bool]] = (),
) -> Select:
    """Narrow an existing model projection without dropping its workspace gate.

    The first selected value must be the full ORM model being scoped, so an
    unrelated entity or scalar column cannot remain the unbounded result root.
    Joined entities retain their existing caller-owned relation/visibility
    policy; this helper scopes the first model only.
    """
    _validate_workspace_model(model, operation="where_in_workspace")
    columns = statement.column_descriptions
    if (
        not columns
        or columns[0].get("entity") is not model
        or columns[0].get("expr") is not model
    ):
        raise TypeError(
            "where_in_workspace requires the first selected workspace model"
        )
    return statement.where(model.workspace_id == workspace_id, *conditions)


def join_in_workspace(
    statement: Select,
    root_model: type,
    joined_model: type,
    *,
    workspace_id: UUID,
    on: ColumnElement[bool],
    isouter: bool = False,
) -> Select:
    """Join supported models inside one Workspace, preserving outer joins.

    Scope the selected root before projection. Both the explicit scope and root
    equality belong in the ON clause for the joined model, so a missing or
    foreign joined row cannot remove a valid outer-join root. ORM aliases are
    accepted when their underlying model satisfies the workspace contract.
    """
    statement = where_in_workspace(
        statement, root_model, workspace_id=workspace_id
    )
    joined = inspect(joined_model)
    _validate_workspace_model(
        joined.mapper.class_ if joined.is_aliased_class else joined_model,
        operation="join_in_workspace",
    )
    return statement.join(
        joined_model,
        on
        & (joined_model.workspace_id == workspace_id)
        & (joined_model.workspace_id == root_model.workspace_id),
        isouter=isouter,
    )


def update_in_workspace[Model](
    model: type[Model],
    *,
    workspace_id: UUID,
    values: Mapping[str, object],
    conditions: Iterable[ColumnElement[bool]] = (),
) -> Update:
    """Build an update constrained to one explicit workspace.

    Composite identities are supported, but moving rows between workspaces is
    deliberately outside this API. Additional selectors can only narrow scope.
    """

    _validate_workspace_scope(model, operation="update_in_workspace")
    if "workspace_id" in values:
        raise ValueError("update_in_workspace cannot change workspace_id")
    return (
        update(model)
        .where(model.workspace_id == workspace_id, *conditions)
        .values(dict(values))
    )


def delete_in_workspace[Model](
    model: type[Model],
    *,
    workspace_id: UUID,
    conditions: Iterable[ColumnElement[bool]] = (),
) -> Delete:
    """Build a deletion constrained to one explicit workspace, for any PK."""

    _validate_workspace_scope(model, operation="delete_in_workspace")
    return delete(model).where(model.workspace_id == workspace_id, *conditions)
