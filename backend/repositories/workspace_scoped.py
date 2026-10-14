"""Mandatory workspace scope for supported workspace-plane reads.

Tables must be explicitly classified on the workspace plane with required
workspace scope. Selection supports composite keys; single-resource lookup
additionally requires the exact primary key ``(id)``. Control/mixed-plane
tables require their own placement and lookup contracts.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Select, inspect, select
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.elements import ColumnElement

from backend.tenancy_manifest import WORKSPACE_PLANE_TABLES


def select_in_workspace[Model](
    model: type[Model],
    *,
    workspace_id: UUID,
    columns: Sequence[InstrumentedAttribute] = (),
    predicates: Sequence[ColumnElement[bool]] = (),
) -> Select:
    """Build a row/column read whose mandatory scope cannot be omitted.

    This is a selection API, not a single-resource identity lookup. Composite
    primary keys are supported, but control/mixed-plane models and columns
    from another model are not. Additional predicates only narrow the scope;
    they cannot replace the required workspace predicate.
    """

    mapper = inspect(model)
    table = mapper.local_table
    if table.name not in WORKSPACE_PLANE_TABLES:
        raise TypeError(
            "select_in_workspace requires a table classified in "
            f"WORKSPACE_PLANE_TABLES; {table.name!r} is not workspace-plane"
        )
    if "workspace_id" not in table.c or table.c.workspace_id.nullable:
        raise TypeError("select_in_workspace requires a non-null workspace_id column")
    if not isinstance(workspace_id, UUID):
        raise TypeError("select_in_workspace requires a workspace UUID")
    if any(
        not isinstance(column, InstrumentedAttribute)
        or column.class_ is not model
        or column.key not in mapper.column_attrs
        or any(
            getattr(mapped, "table", None) is not table
            for mapped in column.property.columns
        )
        for column in columns
    ):
        raise TypeError("select_in_workspace columns must belong to the scoped model")
    return select(*(columns or (model,))).where(
        model.workspace_id == workspace_id,  # type: ignore[attr-defined]
        *predicates,
    )


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

    mapper = inspect(model)
    table = mapper.local_table
    if table.name not in WORKSPACE_PLANE_TABLES:
        raise TypeError(
            "get_in_workspace requires a table classified in "
            f"WORKSPACE_PLANE_TABLES; {table.name!r} is not workspace-plane"
        )

    if "workspace_id" not in table.c or table.c.workspace_id.nullable:
        raise TypeError("get_in_workspace requires a non-null workspace_id column")

    primary_key_names = tuple(column.name for column in mapper.primary_key)
    if primary_key_names != ("id",):
        raise TypeError("get_in_workspace requires the exact primary key (id)")

    return session.scalar(
        select(model).where(
            model.id == resource_id,  # type: ignore[attr-defined]
            model.workspace_id == workspace_id,  # type: ignore[attr-defined]
        )
    )
