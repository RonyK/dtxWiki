from __future__ import annotations

import json
import math
import struct
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.chunk import ChunkEmbedding, ContentChunk
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.lint import LintFinding, LintRun
from backend.models.tree import WorkspaceTreeNode
from backend.services.delegated_tasks import create_task
from backend.services.entity_ref import EntityKind

NEAR_DUPLICATE_THRESHOLD = 0.92
MISPLACED_THRESHOLD = 0.55
LIBRARIAN_MAX_PAGES = 200


def run_librarian_tick(
    session: Session,
    workspace_id: UUID,
    *,
    embedding_provider: str,
    embedding_model: str | None = None,
    now: datetime | None = None,
) -> dict[str, int | str]:
    """Analyze existing embeddings without invoking an embedding provider."""

    provider = embedding_provider.strip().lower()
    if provider == "none":
        return {"skipped": "embedding_provider=none"}
    active_model = embedding_model or session.scalar(
        select(ChunkEmbedding.model)
        .join(ContentChunk, ChunkEmbedding.chunk_id == ContentChunk.id)
        .where(
            ContentChunk.workspace_id == workspace_id,
            ChunkEmbedding.provider == provider,
        )
        .order_by(ChunkEmbedding.created_at.desc(), ChunkEmbedding.id.desc())
        .limit(1)
    )
    if active_model is None:
        return {"skipped": "no_embeddings"}
    eligible_ids = list(
        session.scalars(
            select(ContextPage.id)
            .join(
                ContextPageVersion,
                ContextPage.current_published_version_id == ContextPageVersion.id,
            )
            .where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.lifecycle_status != "archived",
            )
            .order_by(ContextPageVersion.created_at.desc(), ContextPage.id.asc())
            .limit(LIBRARIAN_MAX_PAGES + 1)
        ).all()
    )
    truncated = len(eligible_ids) > LIBRARIAN_MAX_PAGES
    eligible_ids = eligible_ids[:LIBRARIAN_MAX_PAGES]
    vectors = _page_vectors(
        session,
        workspace_id,
        provider=provider,
        model=active_model,
        page_ids=eligible_ids,
    )
    if not vectors:
        return {"skipped": "no_embeddings"}

    now = now or datetime.now(UTC)
    run = LintRun(
        workspace_id=workspace_id,
        lint_scope={"kind": "maintenance"},
        trigger_type="librarian_tick",
        status="completed",
        finding_count_by_severity={},
        started_at=now,
        completed_at=now,
    )
    session.add(run)
    session.flush()

    pages = {
        page.id: page
        for page in session.scalars(
            select(ContextPage).where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.id.in_(tuple(vectors)),
                ContextPage.lifecycle_status != "archived",
                ContextPage.current_published_version_id.is_not(None),
            )
        ).all()
    }
    active_fingerprints = set(
        session.scalars(
            select(LintFinding.fingerprint).where(
                LintFinding.workspace_id == workspace_id,
                LintFinding.rule_id.in_(
                    ("kb.semantic_near_duplicate", "kb.misplaced_page")
                ),
                LintFinding.status.in_(("open", "acknowledged", "dismissed")),
            )
        ).all()
    )
    duplicate_findings = _stage_near_duplicates(
        session,
        run,
        workspace_id,
        pages,
        vectors,
        active_fingerprints,
    )
    misplaced_findings = _stage_misplaced_pages(
        session,
        run,
        workspace_id,
        pages,
        vectors,
        active_fingerprints,
    )
    total = duplicate_findings + misplaced_findings
    if total:
        run.finding_count_by_severity = {"info": total}
    else:
        session.delete(run)
    session.flush()
    return {
        "analyzed_pages": len(pages),
        "truncated": int(truncated),
        "near_duplicate_findings": duplicate_findings,
        "misplaced_findings": misplaced_findings,
    }


def _page_vectors(
    session: Session,
    workspace_id: UUID,
    *,
    provider: str,
    model: str,
    page_ids: list[UUID],
) -> dict[UUID, list[float]]:
    if not page_ids:
        return {}
    rows = session.execute(
        select(
            ContentChunk.document_id,
            ChunkEmbedding.dim,
            ChunkEmbedding.embedding,
        )
        .join(ChunkEmbedding, ChunkEmbedding.chunk_id == ContentChunk.id)
        .where(
            ContentChunk.workspace_id == workspace_id,
            ContentChunk.document_id.in_(page_ids),
            ContentChunk.document_kind == EntityKind.CONTEXT_PAGE.value,
            ContentChunk.content_scope == "published",
            ChunkEmbedding.provider == provider,
            ChunkEmbedding.model == model,
        )
    ).all()
    grouped: dict[UUID, list[list[float]]] = defaultdict(list)
    for page_id, dim, packed in rows:
        if len(packed) != dim * 4:
            continue
        grouped[page_id].append(list(struct.unpack(f"<{dim}f", packed)))
    result: dict[UUID, list[float]] = {}
    for page_id, items in grouped.items():
        dim = len(items[0])
        if any(len(item) != dim for item in items):
            continue
        result[page_id] = [
            sum(item[i] for item in items) / len(items) for i in range(dim)
        ]
    return result


def _stage_near_duplicates(
    session: Session,
    run: LintRun,
    workspace_id: UUID,
    pages: dict[UUID, ContextPage],
    vectors: dict[UUID, list[float]],
    active_fingerprints: set[str],
) -> int:
    created = 0
    ids = sorted(pages, key=str)
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            similarity = _cosine(vectors[left], vectors[right])
            if similarity < NEAR_DUPLICATE_THRESHOLD:
                continue
            fingerprint = f"near-duplicate:{left}:{right}"
            if fingerprint in active_fingerprints:
                continue
            action = {
                "candidate_page_id": str(right),
                "similarity": round(similarity, 6),
                "proposal": "review_merge_or_distinguish",
            }
            finding = _add_finding(
                session,
                run,
                workspace_id,
                target_id=left,
                rule_id="kb.semantic_near_duplicate",
                fingerprint=fingerprint,
                action=action,
            )
            create_task(
                session,
                workspace_id=workspace_id,
                task_type="near_duplicate_verdict",
                target_kind=EntityKind.CONTEXT_PAGE.value,
                target_id=left,
                source="lint",
                fingerprint=fingerprint,
                payload={**action, "finding_id": str(finding.id)},
                priority=70,
            )
            active_fingerprints.add(fingerprint)
            created += 1
    return created


def _stage_misplaced_pages(
    session: Session,
    run: LintRun,
    workspace_id: UUID,
    pages: dict[UUID, ContextPage],
    vectors: dict[UUID, list[float]],
    active_fingerprints: set[str],
) -> int:
    placements = session.execute(
        select(WorkspaceTreeNode.parent_id, WorkspaceTreeNode.target_id).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.visibility_status == "visible",
            WorkspaceTreeNode.primary_placement.is_(True),
            WorkspaceTreeNode.target_kind == EntityKind.CONTEXT_PAGE.value,
            WorkspaceTreeNode.target_id.in_(tuple(pages)),
            WorkspaceTreeNode.parent_id.is_not(None),
        )
    ).all()
    siblings: dict[UUID, list[UUID]] = defaultdict(list)
    for parent_id, page_id in placements:
        siblings[parent_id].append(page_id)
    created = 0
    for parent_id, page_ids in siblings.items():
        if len(page_ids) < 3:
            continue
        for page_id in page_ids:
            peers = [vectors[other] for other in page_ids if other != page_id]
            centroid = [
                sum(item[i] for item in peers) / len(peers)
                for i in range(len(peers[0]))
            ]
            similarity = _cosine(vectors[page_id], centroid)
            if similarity > MISPLACED_THRESHOLD:
                continue
            fingerprint = f"misplaced:{page_id}:{parent_id}"
            if fingerprint in active_fingerprints:
                continue
            _add_finding(
                session,
                run,
                workspace_id,
                target_id=page_id,
                rule_id="kb.misplaced_page",
                fingerprint=fingerprint,
                action={
                    "folder_id": str(parent_id),
                    "similarity": round(similarity, 6),
                    "proposal": "review_placement",
                },
            )
            active_fingerprints.add(fingerprint)
            created += 1
    return created


def _add_finding(
    session: Session,
    run: LintRun,
    workspace_id: UUID,
    *,
    target_id: UUID,
    rule_id: str,
    fingerprint: str,
    action: dict[str, object],
) -> LintFinding:
    finding = LintFinding(
        lint_run_id=run.id,
        workspace_id=workspace_id,
        target_type=EntityKind.CONTEXT_PAGE.value,
        target_id=target_id,
        rule_id=rule_id,
        severity="info",
        status="open",
        suggested_action=json.dumps(action, sort_keys=True),
        fingerprint=fingerprint,
    )
    session.add(finding)
    session.flush()
    return finding
def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return numerator / (left_norm * right_norm)
