"""What a review decision changed, recorded so the decision can be undone.

A decision moves review and change-set statuses, page pointers and metadata,
and version statuses through several writers (rejection, publication,
supersession). Rather than mirror each writer, a session listener records the
value each tracked column held before the decision first changed it and the
value the decision left. The record rides in the decision's audit payload.
Undo proves nothing moved since and writes the recorded values back.

Guarded columns are the decision's result: if any moved since, undo would
discard later work and is refused. Advisory columns are maintenance state the
publication cleared on the decided page (its own stale warning, findings and
tasks); undo restores each one that nothing touched since, and never refuses
because a maintenance worker moved one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session, attributes

from backend.models.change import ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.lint import LintFinding
from backend.models.review import ReviewItem
from backend.repositories.workspace_scoped import (
    get_in_workspace,
    select_in_workspace,
)

EFFECT_FORMAT = 1

# model -> (table, guarded columns, advisory columns)
_TRACKED: dict[type, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    ContextPage: (
        "context_page",
        (
            "current_version_id",
            "current_published_version_id",
            "latest_draft_version_id",
            "lifecycle_status",
            "title",
            "page_type",
            "tags",
            "aliases",
            "placement_parent_node_id",
            "is_public",
            "public_exposure_scope",
            "public_exposed_version_id",
            "content_changed_at",
        ),
        ("stale_candidate",),
    ),
    ContextPageVersion: (
        "context_page_version",
        ("version_status", "graph_status"),
        (),
    ),
    ReviewItem: ("review_item", ("status",), ()),
    ChangeSet: ("change_set", ("status",), ()),
    LintFinding: ("lint_finding", (), ("status", "resolved_at")),
    ClientDelegatedTask: ("client_delegated_task", (), ("status", "lease_expires_at")),
}
_MODEL_BY_TABLE = {table: model for model, (table, *_columns) in _TRACKED.items()}
_ADVISORY = {table: set(advisory) for table, _guarded, advisory in _TRACKED.values()}
# Rows that belong to the decided page, not to the pages publication touches
# on its behalf (dependents flagged stale keep their own maintenance state).
_PAGE_SCOPED = {
    ContextPage: "id",
    LintFinding: "target_id",
    ClientDelegatedTask: "target_id",
}
_UUID_COLUMNS = {
    "current_version_id",
    "current_published_version_id",
    "latest_draft_version_id",
    "placement_parent_node_id",
    "public_exposed_version_id",
}
_DATETIME_COLUMNS = {"content_changed_at", "resolved_at", "lease_expires_at"}


def _encode(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()
    if isinstance(value, list | tuple):
        return [_encode(item) for item in value]
    return value


def _decode(column: str, value: object) -> object:
    if value is None:
        return None
    if column in _UUID_COLUMNS:
        return UUID(str(value))
    if column in _DATETIME_COLUMNS:
        return datetime.fromisoformat(str(value))
    if isinstance(value, list):
        return list(value)
    return value


class DecisionEffectRecorder:
    """Collect the tracked columns one decision changes on ``session``."""

    def __init__(self) -> None:
        self._before: dict[tuple[type, UUID], dict[str, object]] = {}
        self._objects: dict[tuple[type, UUID], object] = {}
        self._created: dict[str, set[UUID]] = {}
        # The decided Context page; set once the decision has locked it.
        self.page_id: UUID | None = None
        # A column changed while its prior value was not loaded has no
        # recorded "before"; such a decision is recorded as not undoable.
        self.complete = True

    def _in_scope(self, obj: object) -> bool:
        model = type(obj)
        if model not in _TRACKED:
            return False
        attribute = _PAGE_SCOPED.get(model)
        return attribute is None or getattr(obj, attribute) == self.page_id

    def _after_flush(self, session: Session, _flush_context: object) -> None:
        for obj in session.new:
            if self._in_scope(obj):
                table = _TRACKED[type(obj)][0]
                self._created.setdefault(table, set()).add(obj.id)
        for obj in session.dirty:
            model = type(obj)
            if not self._in_scope(obj) or obj.id in self._created.get(
                _TRACKED[model][0], ()
            ):
                continue
            key = (model, obj.id)
            _table, guarded, advisory = _TRACKED[model]
            for column in (*guarded, *advisory):
                history = attributes.get_history(obj, column)
                if not history.added:
                    continue
                before = self._before.setdefault(key, {})
                self._objects[key] = obj
                if column in before:
                    continue
                if history.deleted:
                    before[column] = history.deleted[0]
                else:
                    self.complete = False

    def effect(self, **extra: object) -> dict | None:
        """The changed columns as ``{table: {id: {column: [before, after]}}}``."""

        if not self.complete or self._created.get("context_page_version"):
            return None
        rows: dict[str, dict[str, dict[str, list[object]]]] = {}
        for (model, row_id), before in self._before.items():
            obj = self._objects[(model, row_id)]
            changed = {
                column: [_encode(value), _encode(getattr(obj, column))]
                for column, value in before.items()
                if _encode(value) != _encode(getattr(obj, column))
            }
            if changed:
                rows.setdefault(_TRACKED[model][0], {})[str(row_id)] = changed
        page_row = rows.get("context_page", {}).get(str(self.page_id), {})
        if page_row.get("lifecycle_status", [None])[0] == "archived":
            # Publishing revived an archived page and its tree node; undo does
            # not re-run the archive cascade, so such an approval stays.
            return None
        return {
            "format": EFFECT_FORMAT,
            "page_id": str(self.page_id) if self.page_id is not None else None,
            "rows": rows,
            "created": {
                table: sorted(str(row_id) for row_id in ids)
                for table, ids in self._created.items()
                if table in {"review_item", "lint_finding", "client_delegated_task"}
            },
            **{key: _encode(value) for key, value in extra.items()},
        }


@contextmanager
def record_decision_effect(session: Session) -> Iterator[DecisionEffectRecorder]:
    # Earlier pending writes in this transaction belong to their own callers
    # (a bulk run decides several reviews in one transaction).
    session.flush()
    recorder = DecisionEffectRecorder()
    event.listen(session, "after_flush", recorder._after_flush)
    try:
        yield recorder
        session.flush()
    finally:
        event.remove(session, "after_flush", recorder._after_flush)


def _load_rows(
    session: Session, workspace_id: UUID, effect: dict
) -> dict[tuple[str, str], object] | None:
    loaded: dict[tuple[str, str], object] = {}
    for table, rows in effect.get("rows", {}).items():
        model = _MODEL_BY_TABLE.get(table)
        if model is None:
            return None
        for row_id in rows:
            obj = get_in_workspace(
                session, model, workspace_id=workspace_id, resource_id=UUID(row_id)
            )
            if obj is None:
                if all(column in _ADVISORY[table] for column in rows[row_id]):
                    continue
                return None
            loaded[(table, row_id)] = obj
    return loaded


def effect_still_current(
    session: Session, workspace_id: UUID, effect: dict, *, decided_at: datetime
) -> bool:
    """Whether every column the decision changed still holds what it left.

    A newer version of the page means someone wrote on top of the result,
    even where no tracked column moved (a fresh draft, a private edit).
    """

    loaded = _load_rows(session, workspace_id, effect)
    if loaded is None:
        return False
    for (table, row_id), obj in loaded.items():
        for column, (_before, after) in effect["rows"][table][row_id].items():
            if column in _ADVISORY[table]:
                continue
            if _encode(getattr(obj, column)) != after:
                return False
    for review_id in effect["created"].get("review_item", []):
        review = get_in_workspace(
            session, ReviewItem, workspace_id=workspace_id, resource_id=UUID(review_id)
        )
        if review is None or review.status != "pending":
            return False
    page_id = effect.get("page_id")
    if page_id is not None:
        newer = session.scalar(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                criteria=[
                    ContextPageVersion.page_id == UUID(page_id),
                    ContextPageVersion.created_at > decided_at,
                ],
            )
            .with_only_columns(ContextPageVersion.id)
            .limit(1)
        )
        if newer is not None:
            return False
    return True


def restore_effect(
    session: Session, workspace_id: UUID, effect: dict
) -> dict[str, set[str]]:
    """Write every recorded "before" value back; return the columns per table.

    Undo runs only after ``effect_still_current`` proved the rows unchanged.
    """

    loaded = _load_rows(session, workspace_id, effect)
    if loaded is None:
        raise ValueError("decision effect names a row that no longer exists")
    restored: dict[str, set[str]] = {}
    versions = {
        row_id: obj
        for (table, row_id), obj in loaded.items()
        if table == "context_page_version"
    }
    # Like a publication, status repair must not look like an author save.
    preserved_last_saved_at = {
        row_id: version.last_saved_at for row_id, version in versions.items()
    }
    # The partial unique index allows one published_snapshot per page: write
    # the demotion first, then the rest.
    demoted = {
        ("context_page_version", row_id)
        for row_id, obj in versions.items()
        if obj.version_status == "published_snapshot"
        and "version_status" in effect["rows"]["context_page_version"][row_id]
    }
    for batch in (demoted, loaded.keys() - demoted):
        for table, row_id in batch:
            obj = loaded[(table, row_id)]
            for column, (before, after) in effect["rows"][table][row_id].items():
                moved = _encode(getattr(obj, column)) != after
                if column in _ADVISORY[table] and moved:
                    continue
                setattr(obj, column, _decode(column, before))
                restored.setdefault(table, set()).add(column)
        session.flush()
    # Maintenance the publication queued for the decided page answers a
    # publication that no longer stands.
    now = datetime.now(UTC)
    for table, ids in effect["created"].items():
        for row_id in ids:
            obj = get_in_workspace(
                session,
                _MODEL_BY_TABLE[table],
                workspace_id=workspace_id,
                resource_id=UUID(row_id),
            )
            if isinstance(obj, ClientDelegatedTask) and obj.status == "pending":
                obj.status = "expired"
            elif isinstance(obj, LintFinding) and obj.status == "open":
                obj.status = "fixed"
                obj.resolved_at = now
    for row_id, version in versions.items():
        version.last_saved_at = preserved_last_saved_at[row_id]
    session.flush()
    return restored


def review_status_before(effect: dict, review_id: UUID) -> str | None:
    row = effect.get("rows", {}).get("review_item", {}).get(str(review_id), {})
    status = row.get("status")
    return status[0] if status else None
