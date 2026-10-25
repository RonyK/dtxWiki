"""FTS5 BM25 keyword retrieval with actor-visible display hydration."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from functools import cache
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.models.feedback import FeedbackItem
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.review import ReviewItem
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import (
    actor_can_read_drafts,
    actor_can_read_restricted_library,
    actor_can_read_sensitive_agent_pages,
)
from backend.repositories.search_projection import (
    FtsDocumentHit,
    WorkspaceFts5ProjectionRepository,
)
from backend.schemas.agent import SENSITIVE_AGENT_PAGE_CATEGORIES
from backend.schemas.read import RankedResult, SearchFilters
from backend.services.actor_visible_read import (
    VISIBLE_AGENT_SENSITIVITIES,
    actor_can_read_audit_records,
    actor_can_read_feedback,
    change_set_visible,
)
from backend.services.context_visibility import visible_search_version_ids
from backend.services.library_canonical import (
    get_canonical_library_version,
    get_canonical_markdown_artifact,
    get_effective_library_markdown,
    list_canonical_library_artifacts,
)
from backend.services.okf import context_concept_path, library_concept_path
from backend.services.retrieval.outline_location import (
    heading_location_for_query,
    public_heading_path,
)

logger = logging.getLogger("dtxwiki.search")

_SYMBOLIC_LANE_UNAVAILABLE_MESSAGE = (
    "Tier-B FTS5 symbolic lanes are disabled for {dialect}; exact/direct lanes "
    "remain active — configure a search-plane Retriever for this database."
)

SEARCH_SCOPES = {
    "context_page",
    "context_claim",
    "library_item",
    "library_artifact",
    "feedback",
    "change",
    "agent_page",
    "all",
}
RESTRICTED_LIBRARY_TRUST_LEVELS = {"restricted"}
EXCLUDED_LIBRARY_LIFECYCLE_STATUSES = {"archived", "hidden"}

_KNOWLEDGE_SOURCE_KINDS = {
    "context_page",
    "context_claim",
    "library_item",
    "library_artifact",
    "agent_page",
}
_OVERFETCH = 4
_MAX_FTS_SCAN = 2000
_MAX_CHANGE_SCAN = 2000


class KeywordRetriever:
    """Top-K BM25 retrieval over the five knowledge source kinds.

    ``limit`` is the caller's candidate budget. The FTS lane over-fetches a
    bounded window before actor/filter hydration so hidden rows cannot starve
    the visible top-K; it never loads all source-of-truth rows into Python.
    """

    def supported_modes(self) -> set[str]:
        return {"keyword"}

    def retrieve(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        query: str,
        scope: str,
        filters: SearchFilters,
        limit: int,
        actor: ActorContext | None,
    ) -> list[RankedResult]:
        query = query.strip()
        if not query:
            return []
        candidate_limit = max(1, limit * _OVERFETCH)
        results: list[RankedResult] = []
        source_kinds = _source_kinds_for_scope(scope)
        if not filters.include_agents:
            source_kinds.discard("agent_page")
        if source_kinds and query.strip():
            results.extend(
                self._search_fts(
                    session=session,
                    workspace_id=workspace_id,
                    query=query,
                    source_kinds=source_kinds,
                    filters=filters,
                    actor=actor,
                    limit=candidate_limit,
                )
            )
        if scope == "feedback" and actor_can_read_feedback(actor):
            results.extend(
                self._search_feedback(
                    session=session,
                    workspace_id=workspace_id,
                    query=query,
                    limit=candidate_limit,
                )
            )
        if scope == "change" and actor_can_read_audit_records(actor):
            results.extend(
                self._search_changes(
                    session=session,
                    workspace_id=workspace_id,
                    query=query,
                    actor=actor,
                    limit=candidate_limit,
                )
            )
        return results

    def _search_fts(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        query: str,
        source_kinds: set[str],
        filters: SearchFilters,
        actor: ActorContext | None,
        limit: int,
    ) -> list[RankedResult]:
        if not sqlite_symbolic_lanes_available(session):
            return []
        match_query = _fts_match_query(query)
        if not match_query:
            return []
        visibility_scopes = ["published"]
        if actor_can_read_drafts(actor):
            visibility_scopes.append("draft")
        batch_size = min(256, max(32, limit * 2))
        scan_budget = min(_MAX_FTS_SCAN, max(128, limit * 8))
        hits_by_key: dict[tuple[str, UUID], list[FtsDocumentHit]] = {}
        hydrated_by_key: dict[
            tuple[str, UUID], tuple[FtsDocumentHit, RankedResult]
        ] = {}
        repository = WorkspaceFts5ProjectionRepository(session, workspace_id)
        offset = 0
        while offset < scan_budget and len(hydrated_by_key) < limit:
            take = min(batch_size, scan_budget - offset)
            batch_hits, scanned_count = repository.search_documents(
                match_query=match_query,
                source_kinds=sorted(source_kinds),
                visibility_scopes=visibility_scopes,
                allow_sensitive=actor_can_read_sensitive_agent_pages(actor),
                sensitive_categories=sorted(SENSITIVE_AGENT_PAGE_CATEGORIES),
                take=take,
                offset=offset,
            )
            if scanned_count == 0:
                break
            offset += scanned_count
            impacted_keys = {(hit.source_kind, hit.entity_id) for hit in batch_hits}
            for hit in batch_hits:
                hits_by_key.setdefault((hit.source_kind, hit.entity_id), []).append(hit)
            impacted_hits = [hit for key in impacted_keys for hit in hits_by_key[key]]
            for key in impacted_keys:
                hydrated_by_key.pop(key, None)
            for hit, result in self._hydrate_hit_pairs(
                session=session,
                workspace_id=workspace_id,
                query=query,
                hits=impacted_hits,
                filters=filters,
                actor=actor,
            ):
                hydrated_by_key[(hit.source_kind, hit.entity_id)] = (hit, result)
            if scanned_count < take:
                break
        hydrated = sorted(
            hydrated_by_key.values(),
            key=lambda pair: (pair[0].rank, pair[0].rowid),
        )
        return [result for _hit, result in hydrated[:limit]]

    def _hydrate_hit_pairs(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        query: str,
        hits: list[FtsDocumentHit],
        filters: SearchFilters,
        actor: ActorContext | None,
    ) -> list[tuple[FtsDocumentHit, RankedResult]]:
        mapped: list[tuple[FtsDocumentHit, RankedResult]] = []
        mapped.extend(
            self._hydrate_context_pages(
                session, workspace_id, query, hits, filters, actor
            )
        )
        mapped.extend(
            self._hydrate_claims(session, workspace_id, query, hits, filters, actor)
        )
        mapped.extend(
            self._hydrate_library_items(
                session, workspace_id, query, hits, filters, actor
            )
        )
        mapped.extend(
            self._hydrate_artifacts(session, workspace_id, query, hits, filters, actor)
        )
        mapped.extend(self._hydrate_agents(session, workspace_id, query, hits, actor))
        mapped.sort(key=lambda pair: (pair[0].rank, pair[0].rowid))
        return mapped

    @staticmethod
    def _hydrate_context_pages(
        session: Session,
        workspace_id: UUID,
        query: str,
        hits: list[FtsDocumentHit],
        filters: SearchFilters,
        actor: ActorContext | None,
    ) -> list[tuple[FtsDocumentHit, RankedResult]]:
        grouped = _hits_by_entity(hits, "context_page")
        if not grouped:
            return []
        statement = select(ContextPage).where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.id.in_(grouped),
            ContextPage.lifecycle_status != "archived",
        )
        if filters.context_type:
            statement = statement.where(ContextPage.page_type.in_(filters.context_type))
        if filters.status:
            statement = statement.where(
                ContextPage.lifecycle_status.in_(filters.status)
            )
        results: list[tuple[FtsDocumentHit, RankedResult]] = []
        for page in session.scalars(statement).all():
            chosen = _choose_visible_hit(
                grouped[page.id], visible_search_version_ids(page, actor)
            )
            if chosen is None:
                continue
            version = session.get(ContextPageVersion, chosen.version_id)
            if version is None or version.page_id != page.id:
                continue
            location = heading_location_for_query(version.body_md, query)
            results.append(
                (
                    chosen,
                    _ranked(
                        chosen,
                        result_id=page.id,
                        result_type="context_page",
                        title=page.title,
                        citation_available=True,
                        summary_markdown=version.body_md,
                        archived=page.lifecycle_status == "archived",
                        concept_path=context_concept_path(page.slug),
                        concept_type=page.page_type,
                        heading_path=public_heading_path(location),
                        section_id=(
                            location.section_id if location is not None else None
                        ),
                        source_ref={
                            "kind": "context_page_version",
                            "id": str(chosen.version_id),
                        },
                    ),
                )
            )
        return results

    @staticmethod
    def _hydrate_claims(
        session: Session,
        workspace_id: UUID,
        query: str,
        hits: list[FtsDocumentHit],
        filters: SearchFilters,
        actor: ActorContext | None,
    ) -> list[tuple[FtsDocumentHit, RankedResult]]:
        grouped = _hits_by_entity(hits, "context_claim")
        if not grouped:
            return []
        statement = (
            select(ContextClaim, ContextPageVersion, ContextPage)
            .join(
                ContextPageVersion,
                ContextClaim.page_version_id == ContextPageVersion.id,
            )
            .join(ContextPage, ContextPageVersion.page_id == ContextPage.id)
            .where(
                ContextPage.workspace_id == workspace_id,
                ContextClaim.id.in_(grouped),
                ContextPage.lifecycle_status != "archived",
            )
        )
        if filters.context_type:
            statement = statement.where(ContextPage.page_type.in_(filters.context_type))
        if filters.status:
            statement = statement.where(
                ContextPage.lifecycle_status.in_(filters.status)
            )
        results: list[tuple[FtsDocumentHit, RankedResult]] = []
        for claim, version, page in session.execute(statement).all():
            visible_ids = visible_search_version_ids(page, actor)
            if version.id not in visible_ids:
                continue
            chosen = min(grouped[claim.id], key=lambda hit: (hit.rank, hit.rowid))
            if chosen.version_id != version.id:
                continue
            location = heading_location_for_query(version.body_md, query)
            results.append(
                (
                    chosen,
                    _ranked(
                        chosen,
                        result_id=claim.id,
                        result_type="context_claim",
                        title="Context claim",
                        summary_markdown=version.body_md,
                        citation_available=True,
                        archived=claim.status != "active",
                        concept_path=context_concept_path(page.slug),
                        concept_type=page.page_type,
                        heading_path=public_heading_path(location),
                        section_id=(
                            location.section_id if location is not None else None
                        ),
                        source_ref={"kind": "context_page", "id": str(page.id)},
                    ),
                )
            )
        return results

    @staticmethod
    def _hydrate_library_items(
        session: Session,
        workspace_id: UUID,
        query: str,
        hits: list[FtsDocumentHit],
        filters: SearchFilters,
        actor: ActorContext | None,
    ) -> list[tuple[FtsDocumentHit, RankedResult]]:
        grouped = _hits_by_entity(hits, "library_item")
        if not grouped:
            return []
        statement = _visible_library_items_statement(workspace_id, actor).where(
            LibraryItem.id.in_(grouped)
        )
        statement = _apply_library_filters(statement, filters)
        results: list[tuple[FtsDocumentHit, RankedResult]] = []
        for item in session.scalars(statement).all():
            chosen = min(grouped[item.id], key=lambda hit: (hit.rank, hit.rowid))
            canonical_version = get_canonical_library_version(session, item)
            canonical_version_id = (
                canonical_version.id if canonical_version is not None else None
            )
            if chosen.version_id not in {None, canonical_version_id}:
                continue
            canonical_artifact = (
                get_canonical_markdown_artifact(session, canonical_version)
                if canonical_version is not None
                else None
            )
            effective = (
                get_effective_library_markdown(session, canonical_version)
                if canonical_version is not None
                else None
            )
            body_md = (
                effective.body_markdown
                if effective is not None and effective.overlay is not None
                else (
                    canonical_artifact.conversion_detail.index_text
                    if canonical_artifact is not None
                    else None
                )
            )
            location = (
                heading_location_for_query(body_md, query)
                if body_md is not None
                else None
            )
            results.append(
                (
                    chosen,
                    _ranked(
                        chosen,
                        result_id=item.id,
                        result_type="library_item",
                        title=item.title or item.uri,
                        summary_markdown=body_md,
                        citation_available=canonical_artifact is not None,
                        concept_path=library_concept_path(item.id),
                        concept_type="library_item",
                        heading_path=public_heading_path(location),
                        section_id=(
                            location.section_id if location is not None else None
                        ),
                        version_id=canonical_version_id,
                        source_ref={"kind": "library_item", "id": str(item.id)},
                    ),
                )
            )
        return results

    @staticmethod
    def _hydrate_artifacts(
        session: Session,
        workspace_id: UUID,
        query: str,
        hits: list[FtsDocumentHit],
        filters: SearchFilters,
        actor: ActorContext | None,
    ) -> list[tuple[FtsDocumentHit, RankedResult]]:
        grouped = _hits_by_entity(hits, "library_artifact")
        if not grouped:
            return []
        statement = (
            select(LibraryArtifact, LibraryItemVersion, LibraryItem)
            .join(
                LibraryItemVersion,
                LibraryArtifact.library_item_version_id == LibraryItemVersion.id,
            )
            .join(LibraryItem, LibraryItemVersion.library_item_id == LibraryItem.id)
            .where(
                LibraryItem.workspace_id == workspace_id,
                LibraryArtifact.id.in_(grouped),
                LibraryItem.lifecycle_status.notin_(
                    EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
                ),
            )
        )
        if not actor_can_read_restricted_library(actor):
            statement = statement.where(
                LibraryItem.trust_level.notin_(RESTRICTED_LIBRARY_TRUST_LEVELS)
            )
        statement = _apply_library_filters(statement, filters)
        results: list[tuple[FtsDocumentHit, RankedResult]] = []
        for artifact, version, item in session.execute(statement).all():
            chosen = min(grouped[artifact.id], key=lambda hit: (hit.rank, hit.rowid))
            canonical_version = get_canonical_library_version(session, item)
            if canonical_version is None or canonical_version.id != version.id:
                continue
            if chosen.version_id != canonical_version.id:
                continue
            canonical_artifact_ids = {
                row.id
                for row in list_canonical_library_artifacts(session, canonical_version)
            }
            if artifact.id not in canonical_artifact_ids:
                continue
            location = (
                heading_location_for_query(artifact.conversion_detail.index_text, query)
                if artifact.conversion_detail.index_text is not None
                else None
            )
            results.append(
                (
                    chosen,
                    _ranked(
                        chosen,
                        result_id=artifact.id,
                        result_type="library_artifact",
                        title=(
                            item.title
                            or artifact.kind.replace("_", " ").title()
                        ),
                        citation_available=True,
                        summary_markdown=artifact.conversion_detail.index_text,
                        location_fallback=("library_item", item.id),
                        concept_path=library_concept_path(item.id),
                        concept_type="library_artifact",
                        heading_path=public_heading_path(location),
                        section_id=(
                            location.section_id if location is not None else None
                        ),
                        source_ref={
                            "kind": "library_artifact",
                            "id": str(artifact.id),
                        },
                    ),
                )
            )
        return results

    @staticmethod
    def _hydrate_agents(
        session: Session,
        workspace_id: UUID,
        query: str,
        hits: list[FtsDocumentHit],
        actor: ActorContext | None,
    ) -> list[tuple[FtsDocumentHit, RankedResult]]:
        grouped = _hits_by_entity(hits, "agent_page")
        if not grouped:
            return []
        statement = select(AgentPage).where(
            AgentPage.workspace_id == workspace_id,
            AgentPage.id.in_(grouped),
            AgentPage.lifecycle_status != "archived",
        )
        if not actor_can_read_sensitive_agent_pages(actor):
            statement = statement.where(
                AgentPage.category.notin_(SENSITIVE_AGENT_PAGE_CATEGORIES),
                AgentPage.sensitivity.in_(VISIBLE_AGENT_SENSITIVITIES),
            )
        results: list[tuple[FtsDocumentHit, RankedResult]] = []
        can_draft = actor_can_read_drafts(actor)
        for page in session.scalars(statement).all():
            visible_ids: list[UUID] = []
            if can_draft and page.latest_draft_version_id is not None:
                visible_ids.append(page.latest_draft_version_id)
            if (
                page.lifecycle_status == "active"
                and page.current_active_version_id is not None
                and page.current_active_version_id not in visible_ids
            ):
                visible_ids.append(page.current_active_version_id)
            chosen = _choose_visible_hit(grouped[page.id], tuple(visible_ids))
            if chosen is None:
                continue
            version = session.get(AgentPageVersion, chosen.version_id)
            if version is None or version.agent_page_id != page.id:
                continue
            location = heading_location_for_query(version.body_markdown, query)
            results.append(
                (
                    chosen,
                    _ranked(
                        chosen,
                        result_id=page.id,
                        result_type="agent_page",
                        title=page.title,
                        summary_markdown=version.body_markdown,
                        citation_available=False,
                        heading_path=public_heading_path(location),
                        section_id=(
                            location.section_id if location is not None else None
                        ),
                        source_ref={"kind": "agent_page", "id": str(page.id)},
                    ),
                )
            )
        return results

    def _search_feedback(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        query: str,
        limit: int,
    ) -> list[RankedResult]:
        statement = select(FeedbackItem).where(
            FeedbackItem.workspace_id == workspace_id
        )
        for term in query.casefold().split():
            statement = statement.where(
                or_(
                    func.lower(FeedbackItem.body).contains(term),
                    func.lower(FeedbackItem.status).contains(term),
                )
            )
        feedback_items = session.scalars(
            statement.order_by(FeedbackItem.created_at.desc(), FeedbackItem.id).limit(
                limit
            )
        ).all()
        source_refs = self._feedback_source_refs(session, feedback_items)
        return [
            RankedResult(
                id=feedback.id,
                type="feedback",
                title=f"Feedback: {feedback.severity}",
                snippet=_snippet(query, feedback.body),
                score=_workflow_score(query, feedback.body, feedback.status),
                citation_available=False,
                archived=feedback.status == "closed",
                source_ref=source_refs.get(feedback.id),
                match_reason="keyword",
            )
            for feedback in feedback_items
            if _matches(query, f"{feedback.body} {feedback.status}")
        ]

    def _search_changes(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        query: str,
        actor: ActorContext | None,
        limit: int,
    ) -> list[RankedResult]:
        statement = select(ChangeSet).where(ChangeSet.workspace_id == workspace_id)
        for term in query.casefold().split():
            operation_match = (
                select(ChangeOperation.id)
                .where(
                    ChangeOperation.change_set_id == ChangeSet.id,
                    or_(
                        func.lower(ChangeOperation.operation).contains(term),
                        func.lower(
                            func.coalesce(ChangeOperation.diff_summary, "")
                        ).contains(term),
                    ),
                )
                .exists()
            )
            statement = statement.where(
                or_(
                    func.lower(func.coalesce(ChangeSet.reason, "")).contains(term),
                    func.lower(ChangeSet.status).contains(term),
                    operation_match,
                )
            )
        statement = statement.order_by(ChangeSet.created_at.desc(), ChangeSet.id)
        results: list[RankedResult] = []
        # LEAK-02: change_set_visible must run per-row (an operation's target
        # visibility is not expressible as a WHERE clause here), so a single
        # LIMIT-then-filter would let hidden rows starve the visible top-K —
        # same failure mode _search_fts guards against. Scan in bounded
        # batches instead, filtering inside each window.
        scan_budget = min(_MAX_CHANGE_SCAN, max(128, limit * 8))
        batch_size = min(256, max(32, limit * 2))
        offset = 0
        while offset < scan_budget and len(results) < limit:
            take = min(batch_size, scan_budget - offset)
            changesets = session.scalars(statement.offset(offset).limit(take)).all()
            if not changesets:
                break
            offset += len(changesets)
            visible = [
                change_set
                for change_set in changesets
                if change_set_visible(session, change_set, actor)
            ]
            if visible:
                source_refs = self._change_set_source_refs(session, visible)
                operations_by_change_set = self._change_operations_by_change_set(
                    session, visible
                )
                for change_set in visible:
                    operations = operations_by_change_set.get(change_set.id, [])
                    haystack = " ".join(
                        [
                            change_set.reason or "",
                            change_set.status,
                            *[
                                f"{operation.operation} {operation.diff_summary or ''}"
                                for operation in operations
                            ],
                        ]
                    )
                    if not _matches(query, haystack):
                        continue
                    results.append(
                        RankedResult(
                            id=change_set.id,
                            type="change_set",
                            title=change_set.reason or "Change set",
                            snippet=_snippet(query, haystack),
                            score=_workflow_score(
                                query, change_set.reason or "", haystack
                            ),
                            citation_available=False,
                            archived=change_set.status in {"superseded", "discarded"},
                            source_ref=source_refs.get(change_set.id),
                            match_reason="keyword",
                        )
                    )
            if len(changesets) < take:
                break
        return results[:limit]

    @staticmethod
    def _feedback_source_refs(
        session: Session,
        feedback_items: Sequence[FeedbackItem],
    ) -> dict[UUID, dict[str, str]]:
        refs: dict[UUID, dict[str, str]] = {}
        feedback_by_target: dict[tuple[str, UUID], list[UUID]] = {}
        claim_ids: list[UUID] = []
        citation_ids: list[UUID] = []
        artifact_ids: list[UUID] = []
        for feedback in feedback_items:
            target_type = feedback.target_type
            if target_type in {"context_page", "section"}:
                refs[feedback.id] = {
                    "kind": "context_page",
                    "id": str(feedback.target_id),
                }
            elif target_type == "context_claim":
                claim_ids.append(feedback.target_id)
                feedback_by_target.setdefault(
                    (target_type, feedback.target_id), []
                ).append(feedback.id)
            elif target_type == "context_citation":
                citation_ids.append(feedback.target_id)
                feedback_by_target.setdefault(
                    (target_type, feedback.target_id), []
                ).append(feedback.id)
            elif target_type in {"library_item", "source"}:
                refs[feedback.id] = {
                    "kind": "library_item",
                    "id": str(feedback.target_id),
                }
            elif target_type == "library_artifact":
                artifact_ids.append(feedback.target_id)
                feedback_by_target.setdefault(
                    (target_type, feedback.target_id), []
                ).append(feedback.id)
            elif target_type == "agent_page":
                refs[feedback.id] = {
                    "kind": "agent_page",
                    "id": str(feedback.target_id),
                }
        if claim_ids:
            for claim_id, page_id in session.execute(
                select(ContextClaim.id, ContextPageVersion.page_id)
                .join(
                    ContextPageVersion,
                    ContextClaim.page_version_id == ContextPageVersion.id,
                )
                .where(ContextClaim.id.in_(claim_ids))
            ):
                for feedback_id in feedback_by_target.get(
                    ("context_claim", claim_id), []
                ):
                    refs[feedback_id] = {
                        "kind": "context_page",
                        "id": str(page_id),
                    }
        if citation_ids:
            for citation_id, page_id in session.execute(
                select(ContextCitation.id, ContextPageVersion.page_id)
                .join(
                    ContextPageVersion,
                    ContextCitation.target_id == ContextPageVersion.id,
                )
                .where(ContextCitation.id.in_(citation_ids))
                .where(
                    ContextCitation.target_kind.in_(
                        (
                            "context_page_version",
                            "context_page_section",
                            "context_page_whole",
                        )
                    )
                )
            ):
                for feedback_id in feedback_by_target.get(
                    ("context_citation", citation_id), []
                ):
                    refs[feedback_id] = {
                        "kind": "context_page",
                        "id": str(page_id),
                    }
            for citation_id, page_id in session.execute(
                select(ContextCitation.id, ContextPageVersion.page_id)
                .join(ContextClaim, ContextCitation.target_id == ContextClaim.id)
                .join(
                    ContextPageVersion,
                    ContextClaim.page_version_id == ContextPageVersion.id,
                )
                .where(ContextCitation.id.in_(citation_ids))
                .where(ContextCitation.target_kind == "context_claim")
            ):
                for feedback_id in feedback_by_target.get(
                    ("context_citation", citation_id), []
                ):
                    refs[feedback_id] = {
                        "kind": "context_page",
                        "id": str(page_id),
                    }
        if artifact_ids:
            for artifact_id, library_item_id in session.execute(
                select(LibraryArtifact.id, LibraryItemVersion.library_item_id)
                .join(
                    LibraryItemVersion,
                    LibraryArtifact.library_item_version_id == LibraryItemVersion.id,
                )
                .where(LibraryArtifact.id.in_(artifact_ids))
            ):
                for feedback_id in feedback_by_target.get(
                    ("library_artifact", artifact_id), []
                ):
                    refs[feedback_id] = {
                        "kind": "library_item",
                        "id": str(library_item_id),
                    }
        return refs

    @staticmethod
    def _change_set_source_refs(
        session: Session,
        change_sets: Sequence[ChangeSet],
    ) -> dict[UUID, dict[str, str]]:
        change_set_ids = [change_set.id for change_set in change_sets]
        if not change_set_ids:
            return {}
        refs: dict[UUID, dict[str, str]] = {}
        rows = session.execute(
            select(ReviewItem.change_set_id, ReviewItem.id)
            .where(ReviewItem.change_set_id.in_(change_set_ids))
            .order_by(ReviewItem.created_at.desc())
        )
        for change_set_id, review_id in rows:
            refs.setdefault(
                change_set_id,
                {"kind": "review_item", "id": str(review_id)},
            )
        return refs

    @staticmethod
    def _change_operations_by_change_set(
        session: Session,
        change_sets: Sequence[ChangeSet],
    ) -> dict[UUID, list[ChangeOperation]]:
        change_set_ids = [change_set.id for change_set in change_sets]
        if not change_set_ids:
            return {}
        operations_by_change_set: dict[UUID, list[ChangeOperation]] = {}
        operations = session.scalars(
            select(ChangeOperation).where(
                ChangeOperation.change_set_id.in_(change_set_ids)
            )
        )
        for operation in operations:
            operations_by_change_set.setdefault(operation.change_set_id, []).append(
                operation
            )
        return operations_by_change_set


def sqlite_symbolic_lanes_available(session: Session) -> bool:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return True
    _warn_symbolic_lane_unavailable(dialect)
    return False


@cache
def _warn_symbolic_lane_unavailable(dialect: str) -> None:
    logger.warning(_SYMBOLIC_LANE_UNAVAILABLE_MESSAGE.format(dialect=dialect))


def _source_kinds_for_scope(scope: str) -> set[str]:
    if scope == "all":
        return set(_KNOWLEDGE_SOURCE_KINDS)
    return {scope} if scope in _KNOWLEDGE_SOURCE_KINDS else set()


def _fts_match_query(query: str) -> str:
    terms = _lexical_query_terms(query)
    # BM25 is the lexical fallback for a natural-language question. Requiring
    # every surface token made interrogatives and Korean particles turn a real
    # hit into a false coverage gap. Meaningful terms use OR; BM25 naturally
    # rewards documents matching several of them.
    escaped = [f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms]
    if len(escaped) <= 2:
        return " OR ".join(escaped)

    # One incidental surface match is too weak once a query has several
    # meaningful terms (for example a hidden draft title sharing one word with
    # the published body). FTS5 has no minimum-should-match operator, so encode
    # a bounded two-of-N query. This remains less strict than the old all-token
    # AND while preventing a single common term from manufacturing a hit.
    escaped = escaped[:16]
    return " OR ".join(
        f"({left} AND {right})"
        for index, left in enumerate(escaped)
        for right in escaped[index + 1 :]
    )


_LEXICAL_TOKEN_RE = re.compile(r"[^\W_]+(?:[-_.][^\W_]+)*", re.UNICODE)
_ENGLISH_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "of",
        "on",
        "please",
        "tell",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "would",
    }
)
_KOREAN_QUERY_STOPWORDS = frozenset(
    {
        "뭐",
        "무엇",
        "어디",
        "어떤",
        "언제",
        "얼마",
        "왜",
        "어떻게",
        "알려줘",
        "알려주세요",
        "설명",
        "설명해줘",
        "설명해주세요",
    }
)
_KOREAN_QUERY_SUFFIXES = tuple(
    sorted(
        {
            "인가요",
            "입니까",
            "인가",
            "일까요",
            "에서는",
            "으로는",
            "에게서",
            "부터",
            "까지",
            "에서",
            "으로",
            "에게",
            "처럼",
            "보다",
            "하고",
            "이며",
            "이고",
            "은",
            "는",
            "이",
            "가",
            "을",
            "를",
            "의",
            "에",
            "로",
            "와",
            "과",
            "도",
            "만",
        },
        key=len,
        reverse=True,
    )
)


def _lexical_query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for raw in _LEXICAL_TOKEN_RE.findall(query.casefold()):
        term = _strip_korean_query_suffix(raw)
        if (
            not term
            or term in _ENGLISH_QUERY_STOPWORDS
            or term in _KOREAN_QUERY_STOPWORDS
            or term in terms
        ):
            continue
        terms.append(term)
    return terms


def _strip_korean_query_suffix(term: str) -> str:
    for suffix in _KOREAN_QUERY_SUFFIXES:
        if term.endswith(suffix) and len(term) - len(suffix) >= 2:
            return term[: -len(suffix)]
    return term


def _hits_by_entity(
    hits: list[FtsDocumentHit], source_kind: str
) -> dict[UUID, list[FtsDocumentHit]]:
    grouped: dict[UUID, list[FtsDocumentHit]] = {}
    for hit in hits:
        if hit.source_kind == source_kind:
            grouped.setdefault(hit.entity_id, []).append(hit)
    return grouped


def _choose_visible_hit(
    hits: list[FtsDocumentHit], visible_version_ids: tuple[UUID, ...]
) -> FtsDocumentHit | None:
    priority = {
        version_id: index for index, version_id in enumerate(visible_version_ids)
    }
    visible = [hit for hit in hits if hit.version_id in priority]
    if not visible:
        return None
    return min(
        visible,
        key=lambda hit: (priority[hit.version_id], hit.rank, hit.rowid),
    )


def _ranked(
    hit: FtsDocumentHit,
    *,
    result_id: UUID,
    result_type: str,
    title: str,
    citation_available: bool,
    summary_markdown: str | None = None,
    location_fallback: tuple[str, UUID] | None = None,
    archived: bool = False,
    concept_path: str | None = None,
    concept_type: str | None = None,
    heading_path: list[str] | None = None,
    section_id: str | None = None,
    version_id: UUID | None = None,
    source_ref: dict[str, str] | None = None,
) -> RankedResult:
    result = RankedResult(
        id=result_id,
        type=result_type,
        title=title,
        snippet=hit.snippet,
        score=1000.0 - hit.rank,
        citation_available=citation_available,
        archived=archived,
        concept_path=concept_path,
        concept_type=concept_type,
        version_id=version_id or hit.version_id,
        heading_path=heading_path,
        section_id=section_id,
        source_ref=source_ref,
        match_reason="keyword",
    )
    result._summary_markdown = summary_markdown
    result._location_fallback = location_fallback
    return result


def _visible_library_items_statement(
    workspace_id: UUID,
    actor: ActorContext | None,
):
    statement = select(LibraryItem).where(
        LibraryItem.workspace_id == workspace_id,
        LibraryItem.lifecycle_status.notin_(EXCLUDED_LIBRARY_LIFECYCLE_STATUSES),
    )
    if not actor_can_read_restricted_library(actor):
        statement = statement.where(
            LibraryItem.trust_level.notin_(RESTRICTED_LIBRARY_TRUST_LEVELS)
        )
    return statement


def _apply_library_filters(statement, filters: SearchFilters):
    if filters.connector_type:
        statement = statement.where(
            LibraryItem.connector_type.in_(filters.connector_type)
        )
    if filters.format_hint:
        statement = statement.where(LibraryItem.format_hint.in_(filters.format_hint))
    if filters.trust_level:
        statement = statement.where(LibraryItem.trust_level.in_(filters.trust_level))
    if filters.status:
        statement = statement.where(LibraryItem.lifecycle_status.in_(filters.status))
    return statement


def _matches(query: str, haystack: str) -> bool:
    if not query:
        return True
    normalized = haystack.lower()
    return all(term in normalized for term in query.lower().split())


def _workflow_score(query: str, title: str, haystack: str) -> float:
    if not query:
        return 0.0
    title_lower = title.lower()
    haystack_lower = haystack.lower()
    score = 0.0
    for term in query.lower().split():
        if term in title_lower:
            score += 5.0
        score += haystack_lower.count(term)
    # Knowledge FTS rows are resolver-priority 1000; workflow rows remain
    # supporting results in an all-scope query while retaining local ordering.
    return score / 1000.0


def _snippet(query: str, haystack: str) -> str:
    collapsed = " ".join(haystack.split())
    if not collapsed:
        return ""
    positions = [
        collapsed.lower().find(term)
        for term in query.lower().split()
        if collapsed.lower().find(term) >= 0
    ]
    start = max(min(positions) - 60, 0) if positions else 0
    return collapsed[start : start + 240]
