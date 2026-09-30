"""client_delegated_task domain service (design §17.3.1-c, M0).

Sprint-3 ships the queue skeleton: creation with reconciliation-keyed dedup,
the status transition guard, lease expiry, and the pending summary that the
M1 completion-shaped fallback will serve. No consumer tool yet.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.delegated_task import ClientDelegatedTask

# Dedup discipline: an open or dismissed row with the same recon key blocks
# re-creation (dismissal suppresses while the condition persists); staged and
# expired rows allow a fresh task.
_BLOCKING_STATUSES = ("pending", "leased", "answered", "dismissed")

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"leased", "dismissed", "expired"},
    "leased": {"answered", "pending", "expired"},
    # -> expired: reconciliation close — the producer retires answered work
    # and dismissed suppressions once the underlying condition resolves
    # (dismissal suppresses only WHILE it holds, not forever).
    "answered": {"staged", "dismissed", "expired"},
    "staged": set(),
    "dismissed": {"expired"},
    "expired": set(),
}


class IllegalTaskTransitionError(ValueError):
    pass


def create_task(
    session: Session,
    *,
    workspace_id: UUID,
    task_type: str,
    target_kind: str,
    target_id: UUID,
    source: str,
    fingerprint: str,
    payload: dict | None = None,
    priority: int = 100,
    block_staged: bool = False,
) -> ClientDelegatedTask | None:
    """Create a task unless the reconciliation key is blocked (dedup)."""
    blocking_statuses = (
        (*_BLOCKING_STATUSES, "staged") if block_staged else _BLOCKING_STATUSES
    )
    existing = session.scalar(
        select(ClientDelegatedTask.id)
        .where(ClientDelegatedTask.workspace_id == workspace_id)
        .where(ClientDelegatedTask.task_type == task_type)
        .where(ClientDelegatedTask.target_kind == target_kind)
        .where(ClientDelegatedTask.target_id == target_id)
        .where(ClientDelegatedTask.fingerprint == fingerprint)
        .where(ClientDelegatedTask.status.in_(blocking_statuses))
        .limit(1)
    )
    if existing is not None:
        return None
    task = ClientDelegatedTask(
        workspace_id=workspace_id,
        task_type=task_type,
        target_kind=target_kind,
        target_id=target_id,
        payload=payload or {},
        priority=priority,
        status="pending",
        source=source,
        fingerprint=fingerprint,
    )
    session.add(task)
    session.flush()
    return task


def transition_task(
    session: Session,
    task: ClientDelegatedTask,
    new_status: str,
    *,
    result_ref: str | None = None,
) -> ClientDelegatedTask:
    allowed = ALLOWED_TRANSITIONS.get(task.status, set())
    if new_status not in allowed:
        raise IllegalTaskTransitionError(
            f"{task.status} -> {new_status} is not a legal task transition"
        )
    task.status = new_status
    if result_ref is not None:
        task.result_ref = result_ref
    session.flush()
    return task


def expire_stale_leases(
    session: Session,
    workspace_id: UUID,
    *,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(UTC)
    stale = session.scalars(
        select(ClientDelegatedTask)
        .where(ClientDelegatedTask.workspace_id == workspace_id)
        .where(ClientDelegatedTask.status == "leased")
        .where(ClientDelegatedTask.lease_expires_at.is_not(None))
        .where(ClientDelegatedTask.lease_expires_at < now)
    ).all()
    for task in stale:
        task.status = "pending"
        task.lease_expires_at = None
    session.flush()
    return len(stale)


def pending_summary(session: Session, workspace_id: UUID) -> dict[str, int]:
    """Per-task-type pending counts — the M1 completion-shaped fallback's
    data source ('N maintenance tasks waiting')."""
    rows = session.execute(
        select(ClientDelegatedTask.task_type, func.count())
        .where(ClientDelegatedTask.workspace_id == workspace_id)
        .where(ClientDelegatedTask.status == "pending")
        .group_by(ClientDelegatedTask.task_type)
    ).all()
    return {task_type: count for task_type, count in rows}


def produce_folder_summary_tasks(session: Session, workspace_id: UUID) -> int:
    """Q-1 producer: visible folders with >=10 visible children and no
    nav_summary. Constant fingerprint — dismissal suppresses while the gap
    persists (lint-reconciliation semantics). Runs after lint and reindex."""
    from backend.models.tree import WorkspaceTreeNode

    child_counts = dict(
        session.execute(
            select(
                WorkspaceTreeNode.parent_id,
                func.count(WorkspaceTreeNode.id),
            )
            .where(WorkspaceTreeNode.workspace_id == workspace_id)
            .where(WorkspaceTreeNode.visibility_status == "visible")
            .where(WorkspaceTreeNode.parent_id.is_not(None))
            .group_by(WorkspaceTreeNode.parent_id)
        ).all()
    )
    folders = session.scalars(
        select(WorkspaceTreeNode)
        .where(WorkspaceTreeNode.workspace_id == workspace_id)
        .where(WorkspaceTreeNode.node_kind == "folder")
        .where(WorkspaceTreeNode.visibility_status == "visible")
        .where(WorkspaceTreeNode.nav_summary.is_(None))
    ).all()
    gap_folder_ids = {
        folder.id for folder in folders if child_counts.get(folder.id, 0) >= 10
    }
    # Reconciliation close: the gap resolved (summary authored, folder
    # shrank/archived) — retire open rows so M1 never drains stale work, and
    # retire dismissed rows so a FUTURE re-gap creates a fresh task instead
    # of being suppressed forever.
    stale_statement = (
        select(ClientDelegatedTask)
        .where(ClientDelegatedTask.workspace_id == workspace_id)
        .where(ClientDelegatedTask.task_type == "folder_nav_summary")
        .where(ClientDelegatedTask.fingerprint == "missing_nav_summary")
        .where(ClientDelegatedTask.status.in_(_BLOCKING_STATUSES))
    )
    if gap_folder_ids:
        stale_statement = stale_statement.where(
            ClientDelegatedTask.target_id.not_in(gap_folder_ids)
        )
    for task in session.scalars(stale_statement).all():
        task.status = "expired"
        task.lease_expires_at = None
    session.flush()
    created = 0
    for folder in folders:
        if folder.id not in gap_folder_ids:
            continue
        task = create_task(
            session,
            workspace_id=workspace_id,
            task_type="folder_nav_summary",
            target_kind="workspace_tree_node",
            target_id=folder.id,
            source="lint",
            fingerprint="missing_nav_summary",
            payload={"title": folder.title, "section": folder.section},
            priority=50,
        )
        if task is not None:
            created += 1
    return created


def produce_page_meta_refresh_task(
    session: Session,
    *,
    workspace_id: UUID,
    page_id: UUID,
    published_version_id: UUID,
    title: str,
    concept_path: str,
    reason: str | None,
) -> ClientDelegatedTask | None:
    """Q-2 producer (publish hook): fingerprint = published version id, so a
    republish creates a fresh task while dismissal suppresses only that
    version's task."""
    return create_task(
        session,
        workspace_id=workspace_id,
        task_type="page_meta_refresh",
        target_kind="context_page",
        target_id=page_id,
        source="publish",
        fingerprint=str(published_version_id),
        payload={
            "published_version_id": str(published_version_id),
            "title": title,
            "concept_path": concept_path,
            "reason": reason,
        },
    )


def produce_coverage_gap_task(
    session: Session,
    *,
    workspace_id: UUID,
    gap_id: UUID,
    query_norm: str,
    surface: str,
    nearest_candidates: list[dict],
) -> ClientDelegatedTask | None:
    """Q-4 producer: one review task per unresolved coverage-gap row."""

    return create_task(
        session,
        workspace_id=workspace_id,
        task_type="coverage_gap_review",
        target_kind="knowledge_coverage_gap",
        target_id=gap_id,
        source="coverage_gap",
        fingerprint="unresolved",
        payload={
            "query_norm": query_norm,
            "surface": surface,
            "nearest_candidates": nearest_candidates[:3],
        },
        priority=60,
    )


def produce_import_batch_classify_task(
    session: Session,
    *,
    workspace_id: UUID,
    library_item_id: UUID,
    version_id: UUID,
    title: str,
    uri: str,
    format_hint: str,
) -> ClientDelegatedTask | None:
    return create_task(
        session,
        workspace_id=workspace_id,
        task_type="import_batch_classify",
        target_kind="library_item",
        target_id=library_item_id,
        source="import",
        fingerprint=str(version_id),
        payload={
            "library_item_version_id": str(version_id),
            "title": title,
            "uri": uri,
            "format_hint": format_hint,
        },
        priority=85,
        block_staged=True,
    )
