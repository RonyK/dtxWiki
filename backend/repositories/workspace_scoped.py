"""Workspace-scoped lookup for one supported workspace-plane model shape.

This helper is deliberately narrower than "any model with an ``id`` and a
``workspace_id`` column". The table must be explicitly classified on the
workspace plane, its workspace scope must be required, and its exact primary
key must be the single ``id`` column. Workspace-plane composite identities and
control- or mixed-plane tables need purpose-specific lookup APIs.
"""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import Select, inspect, select
from sqlalchemy.orm import Session

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


def _validate_workspace_model(model: type, *, operation: str) -> None:
    mapper = inspect(model)
    table = mapper.local_table
    if table.name not in WORKSPACE_PLANE_TABLES:
        raise TypeError(
            f"{operation} requires a table classified in "
            f"WORKSPACE_PLANE_TABLES; {table.name!r} is not workspace-plane"
        )

    if "workspace_id" not in table.c or table.c.workspace_id.nullable:
        raise TypeError(f"{operation} requires a non-null workspace_id column")

    primary_key_names = tuple(column.name for column in mapper.primary_key)
    if primary_key_names != ("id",):
        raise TypeError(f"{operation} requires the exact primary key (id)")
