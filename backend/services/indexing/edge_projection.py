"""Feature-neutral values and ownership-checked edge reconciliation.

Producers own queries, locks, observations, policy and revision changes. This
module only compares their scoped desired rows with the rows they already own.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Any, TypedDict
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.context import EdgeProjection
from backend.services.entity_ref import EntityKind

type EdgeIdentity = tuple[str, UUID, str, UUID, str]


class EdgeProjectionRow(TypedDict):
    workspace_id: UUID
    source_kind: str
    source_id: UUID
    target_kind: str
    target_id: UUID
    edge_kind: str
    provenance: str
    weight: float
    occurred_at: datetime | None
    support_session_count: int | None
    first_observed_at: datetime | None
    source_ref: dict[str, Any]


@dataclass(frozen=True)
class EdgeProjectionScope:
    workspace_id: UUID
    edge_kinds: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID) or not self.edge_kinds:
            raise ValueError("A projection requires a workspace and owned edge kinds")
        if not isinstance(self.edge_kinds, frozenset) or any(
            not isinstance(kind, str) or not kind for kind in self.edge_kinds
        ):
            raise ValueError("Owned edge kinds must be a nonempty frozenset of names")


@dataclass(frozen=True)
class EdgeProjectionDelta:
    desired_count: int
    inserted: int
    updated: int
    deleted: int
    affected_source_ids: frozenset[UUID]

    @property
    def changed(self) -> bool:
        return bool(self.inserted or self.updated or self.deleted)


def build_edge_row(
    *,
    workspace_id: UUID,
    source_kind: EntityKind,
    source_id: UUID,
    target_kind: EntityKind,
    target_id: UUID,
    edge_kind: str,
    provenance: str,
    source_table: str,
    weight: float = 1.0,
    occurred_at: datetime | None = None,
    support_session_count: int | None = None,
    first_observed_at: datetime | None = None,
) -> EdgeProjectionRow:
    return {
        "workspace_id": workspace_id,
        "source_kind": source_kind.value,
        "source_id": source_id,
        "target_kind": target_kind.value,
        "target_id": target_id,
        "edge_kind": edge_kind,
        "provenance": provenance,
        "weight": weight,
        "occurred_at": occurred_at,
        "support_session_count": support_session_count,
        "first_observed_at": first_observed_at,
        "source_ref": {"table": source_table},
    }


def edge_row_identity(row: Mapping[str, object]) -> EdgeIdentity:
    values = tuple(
        row[key]
        for key in ("source_kind", "source_id", "target_kind", "target_id", "edge_kind")
    )
    if not all(isinstance(values[index], str) for index in (0, 2, 4)) or not all(
        isinstance(values[index], UUID) for index in (1, 3)
    ):
        raise ValueError("Edge projection identity has invalid types")
    return values


def stored_edge_identity(edge: EdgeProjection) -> EdgeIdentity:
    return edge_row_identity(
        {
            key: getattr(edge, key)
            for key in (
                "source_kind",
                "source_id",
                "target_kind",
                "target_id",
                "edge_kind",
            )
        }
    )


_VALUE_FIELDS = (
    "provenance",
    "occurred_at",
    "source_ref",
    "support_session_count",
    "first_observed_at",
)


def _validate_values(values) -> None:
    if not isinstance(values["provenance"], str) or not isinstance(
        values["source_ref"], dict
    ):
        raise ValueError("Edge projection provenance and source metadata are required")
    for key in ("occurred_at", "first_observed_at"):
        if values[key] is not None and not isinstance(values[key], datetime):
            raise ValueError("Edge projection times must be datetimes or None")
    count = values["support_session_count"]
    if count is not None and type(count) is not int:
        raise ValueError("Edge projection support must be an integer or None")
    if not isfinite(float(values["weight"])):
        raise ValueError("Edge projection weight must be finite")


def _apply_edge_values(edge: EdgeProjection, desired: Mapping[str, object]) -> bool:
    changed = False
    for attribute in _VALUE_FIELDS:
        value = desired[attribute]
        if getattr(edge, attribute) != value:
            setattr(edge, attribute, value)
            changed = True
    weight = float(desired["weight"])
    if float(edge.weight) != weight:
        edge.weight = weight
        changed = True
    return changed


def reconcile_edge_projection_rows(
    session: Session,
    *,
    scope: EdgeProjectionScope,
    existing_rows: Collection[EdgeProjection],
    desired_rows: Collection[Mapping[str, object]],
) -> EdgeProjectionDelta:
    """Validate every row before mutation; preserve identities and no-op state."""
    with session.no_autoflush:
        existing, desired = {}, {}
        for row in desired_rows:
            if set(row) != set(EdgeProjectionRow.__annotations__):
                raise ValueError(
                    "Desired edge must contain only projection value fields"
                )
            identity = edge_row_identity(row)
            if (
                row["workspace_id"] != scope.workspace_id
                or identity[4] not in scope.edge_kinds
            ):
                raise ValueError(
                    "Desired edge is outside the producer's projection scope"
                )
            _validate_values(row)
            if identity in desired:
                raise ValueError("Edge extractor produced duplicate identities")
            desired[identity] = dict(row)
        for edge in existing_rows:
            identity = stored_edge_identity(edge)
            if (
                edge.workspace_id != scope.workspace_id
                or identity[4] not in scope.edge_kinds
            ):
                raise ValueError(
                    "Stored edge is outside the producer's projection scope"
                )
            _validate_values(
                {key: getattr(edge, key) for key in (*_VALUE_FIELDS, "weight")}
            )
            if identity in existing:
                raise ValueError("Edge projection contains duplicate identities")
            existing[identity] = edge

    inserted = updated = deleted = 0
    affected: set[UUID] = set()
    for identity, edge in existing.items():
        row = desired.get(identity)
        if row is None:
            session.delete(edge)
            deleted += 1
            affected.add(edge.source_id)
        elif _apply_edge_values(edge, row):
            updated += 1
            affected.add(edge.source_id)
    session.flush()
    now = datetime.now(UTC)
    for identity in sorted(
        desired.keys() - existing.keys(), key=lambda key: tuple(map(str, key))
    ):
        session.add(EdgeProjection(**desired[identity], last_seen_at=now))
        inserted += 1
        affected.add(identity[1])
    session.flush()
    return EdgeProjectionDelta(
        len(desired), inserted, updated, deleted, frozenset(affected)
    )
