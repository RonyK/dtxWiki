from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage
from backend.models.context import ContextPage
from backend.models.coverage import KnowledgeCoverageGap
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.coverage import CoverageGapListData, CoverageGapRead
from backend.services.actor_visible_read import (
    actor_can_read_agent_page,
    context_page_visible,
    library_item_visible,
)
from backend.services.delegated_tasks import produce_coverage_gap_task
from backend.services.entity_ref import normalize_entity_name
from backend.services.library_canonical import (
    canonical_library_artifact_belongs_to_item,
    get_canonical_library_version,
)

_SURFACES = {"ask", "search_rest", "search_mcp"}


def coverage_candidate_visible(
    session: Session,
    candidate: object,
    actor: ActorContext | None,
    workspace_id: UUID | None = None,
) -> bool:
    """Whether a stored coverage-gap ``nearest_candidate`` may be shown to
    ``actor``. A candidate was filtered for the actor who GENERATED the gap, so
    a later reader/leaser must be re-checked. Fails CLOSED on anything it cannot
    verify (non-dict, malformed id, unknown kind, deleted/archived/hidden or
    cross-workspace target), since the stored candidate still carries the
    target's snapshot title/path.
    """
    if not isinstance(candidate, dict):
        return False
    kind = candidate.get("kind")
    try:
        candidate_id = UUID(str(candidate.get("id")))
    except (TypeError, ValueError):
        return False
    if workspace_id is None:
        return False
    if kind in ("library_item", "library_artifact", "library_item_version"):
        item, version = _resolve_candidate_library_item(
            session,
            workspace_id,
            kind,
            candidate_id,
        )
        if item is None:
            return False
        if version is not None:
            canonical_version = get_canonical_library_version(session, item)
            if canonical_version is None or version.id != canonical_version.id:
                return False
        if kind == "library_artifact" and not (
            canonical_library_artifact_belongs_to_item(
                session,
                item=item,
                artifact_id=candidate_id,
            )
        ):
            return False
        if actor is None:
            return True
        return library_item_visible(
            item, actor, workspace_id=workspace_id, version=version
        )
    if kind == "agent_page":
        page = get_in_workspace(
            session,
            AgentPage,
            workspace_id=workspace_id,
            resource_id=candidate_id,
        )
        if page is None:
            return False
        if page.lifecycle_status != "active":
            return False
        if actor is None:
            return True
        return actor_can_read_agent_page(page, actor)
    if kind == "context_page":
        page = get_in_workspace(
            session,
            ContextPage,
            workspace_id=workspace_id,
            resource_id=candidate_id,
        )
        if actor is None:
            return page is not None and page.lifecycle_status != "archived"
        return context_page_visible(page, actor, workspace_id=workspace_id)
    return False


def _resolve_candidate_library_item(
    session: Session,
    workspace_id: UUID,
    kind: str,
    candidate_id: UUID,
) -> tuple[LibraryItem | None, LibraryItemVersion | None]:
    """Resolve a candidate to its ``(item, version)``. ``version`` is None for a
    whole-item candidate; for a version/artifact candidate it is the specific
    version so the caller can enforce current-version equality."""
    if kind == "library_item":
        return (
            get_in_workspace(
                session,
                LibraryItem,
                workspace_id=workspace_id,
                resource_id=candidate_id,
            ),
            None,
        )
    if kind == "library_item_version":
        version = get_in_workspace(
            session,
            LibraryItemVersion,
            workspace_id=workspace_id,
            resource_id=candidate_id,
        )
    else:  # library_artifact
        artifact = get_in_workspace(
            session,
            LibraryArtifact,
            workspace_id=workspace_id,
            resource_id=candidate_id,
        )
        version = (
            get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=artifact.library_item_version_id,
            )
            if artifact is not None
            else None
        )
    if version is None:
        return None, None
    return (
        get_in_workspace(
            session,
            LibraryItem,
            workspace_id=workspace_id,
            resource_id=version.library_item_id,
        ),
        version,
    )


def list_coverage_gaps(
    session: Session,
    *,
    workspace_id: UUID,
    status: str | None = None,
    limit: int = 100,
    actor: ActorContext | None = None,
) -> CoverageGapListData:
    statement = select(KnowledgeCoverageGap).where(
        KnowledgeCoverageGap.workspace_id == workspace_id
    )
    if status:
        if status == "resolved":
            statement = statement.where(
                KnowledgeCoverageGap.status.in_(("triaged", "dismissed", "resolved"))
            )
        else:
            statement = statement.where(KnowledgeCoverageGap.status == status)
    rows = session.scalars(
        statement.order_by(
            KnowledgeCoverageGap.hit_count.desc(),
            KnowledgeCoverageGap.created_at.desc(),
        ).limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    finding_ids: dict[UUID, UUID] = {}
    if rows:
        tasks = session.scalars(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.workspace_id == workspace_id,
                ClientDelegatedTask.target_kind == "knowledge_coverage_gap",
                ClientDelegatedTask.target_id.in_([row.id for row in rows]),
                ClientDelegatedTask.status == "staged",
                ClientDelegatedTask.result_ref.is_not(None),
            )
        ).all()
        for task in tasks:
            prefix, separator, raw_id = (task.result_ref or "").partition(":")
            if prefix != "lint_finding" or not separator:
                continue
            try:
                finding_ids[task.target_id] = UUID(raw_id)
            except ValueError:
                continue
    items: list[CoverageGapRead] = []
    for row in rows:
        # Drop stored candidates the reader cannot see (the gap was filtered for
        # its generating actor, not this reader), tolerating a corrupted scalar
        # value (fail closed / empty, not 500).
        raw = row.nearest_candidates
        filtered = [
            candidate
            for candidate in (raw if isinstance(raw, list) else [])
            if coverage_candidate_visible(session, candidate, actor, workspace_id)
        ]
        # A non-list scalar would fail model_validate; neutralize it just for
        # validation, then restore so the shared ORM row is not corrupted.
        if not isinstance(raw, list):
            row.nearest_candidates = []
        try:
            model = CoverageGapRead.model_validate(row, from_attributes=True)
        finally:
            row.nearest_candidates = raw
        items.append(
            model.model_copy(
                update={
                    "finding_id": finding_ids.get(row.id),
                    "nearest_candidates": filtered,
                }
            )
        )
    return CoverageGapListData(
        items=items,
        has_more=has_more,
    )


def record_coverage_gap(
    session: Session,
    *,
    workspace_id: UUID,
    query: str,
    surface: str,
    tried_lanes: list[str],
    nearest_candidates: list[dict[str, Any]],
) -> KnowledgeCoverageGap | None:
    if surface not in _SURFACES:
        raise ValueError(f"unsupported coverage-gap surface: {surface}")
    query_norm = normalize_entity_name(query)
    if len(query_norm) < 3:
        return None

    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(KnowledgeCoverageGap)
    elif dialect == "sqlite":
        statement = sqlite_insert(KnowledgeCoverageGap)
    else:  # The product supports SQLite and PostgreSQL only.
        raise RuntimeError(f"unsupported coverage-gap dialect: {dialect}")

    row = session.execute(
        statement.values(
            workspace_id=workspace_id,
            query_norm=query_norm,
            surface=surface,
            tried_lanes=_stable_unique(tried_lanes),
            nearest_candidates=nearest_candidates[:3],
            status="open",
            hit_count=1,
        )
        .on_conflict_do_update(
            index_elements=["workspace_id", "query_norm"],
            index_where=text("status = 'open'"),
            set_={"hit_count": KnowledgeCoverageGap.hit_count + 1},
        )
        .returning(KnowledgeCoverageGap.id, KnowledgeCoverageGap.hit_count)
    ).one()
    gap = get_in_workspace(
        session,
        KnowledgeCoverageGap,
        workspace_id=workspace_id,
        resource_id=row.id,
    )
    if gap is None:
        raise RuntimeError("coverage-gap upsert returned a missing row")
    # The upsert's RETURNING value is authoritative even when the scoped query
    # resolves to an existing identity-map object whose attributes were loaded
    # before ON CONFLICT incremented the database row.
    gap.hit_count = row.hit_count
    gap.tried_lanes = _stable_unique([*(gap.tried_lanes or []), *tried_lanes])
    if nearest_candidates:
        gap.nearest_candidates = nearest_candidates[:3]
    session.flush()
    if row.hit_count == 1:
        produce_coverage_gap_task(
            session,
            workspace_id=workspace_id,
            gap_id=gap.id,
            query_norm=query_norm,
            surface=surface,
            nearest_candidates=nearest_candidates,
        )
    return gap


def resolve_coverage_gap(
    session: Session,
    *,
    workspace_id: UUID,
    query: str,
) -> int:
    """Close an obsolete miss and retire its unconsumed Q-4 work.

    A normalized query can become answerable after publish/reindex/alias
    changes. Leaving the old open row would ask borrowed intelligence to
    compile knowledge that now exists (F-5/R-16).
    """

    query_norm = normalize_entity_name(query)
    if len(query_norm) < 3:
        return 0
    gaps = session.scalars(
        select(KnowledgeCoverageGap).where(
            KnowledgeCoverageGap.workspace_id == workspace_id,
            KnowledgeCoverageGap.query_norm == query_norm,
            KnowledgeCoverageGap.status == "open",
        )
    ).all()
    if not gaps:
        return 0
    now = datetime.now(UTC)
    gap_ids = [gap.id for gap in gaps]
    for gap in gaps:
        gap.status = "resolved"
        gap.resolved_at = now
    tasks = session.scalars(
        select(ClientDelegatedTask).where(
            ClientDelegatedTask.workspace_id == workspace_id,
            ClientDelegatedTask.task_type == "coverage_gap_review",
            ClientDelegatedTask.target_kind == "knowledge_coverage_gap",
            ClientDelegatedTask.target_id.in_(gap_ids),
            ClientDelegatedTask.status.in_(
                ("pending", "leased", "answered", "dismissed")
            ),
        )
    ).all()
    for task in tasks:
        task.status = "expired"
        task.lease_expires_at = None
    session.flush()
    return len(gaps)


def record_coverage_rewrite_attempt(
    session: Session,
    *,
    workspace_id: UUID,
    query: str,
    rewritten_query: str,
    result_count: int,
    tried_lanes: list[str],
) -> None:
    """Attach a bounded SY-4 attempt to the original coverage-gap row."""
    query_norm = normalize_entity_name(query)
    if len(query_norm) < 3:
        return
    gap = session.scalar(
        select(KnowledgeCoverageGap).where(
            KnowledgeCoverageGap.workspace_id == workspace_id,
            KnowledgeCoverageGap.query_norm == query_norm,
            KnowledgeCoverageGap.status == "open",
        )
    )
    if gap is None:
        return
    attempt = {
        "query": rewritten_query,
        "result_count": max(0, result_count),
        "tried_lanes": _stable_unique(tried_lanes),
    }
    prior = list(gap.rewrite_attempts or [])
    gap.rewrite_attempts = [*prior[-7:], attempt]
    session.flush()


def _stable_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
