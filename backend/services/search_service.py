"""Deterministic search planner and ranked retrieval-lane composition."""

import base64
import json
import logging
from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from backend.llm.base import RerankProvider, RerankRequest
from backend.models.context import ContextPage, ContextPageVersion
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import (
    actor_can_read_drafts,
    actor_can_read_restricted_library,
)
from backend.repositories.search_projection import (
    PageNameTrigramHit,
    WorkspaceFts5ProjectionRepository,
)
from backend.schemas.read import (
    RankedResult,
    SearchFilters,
    SearchPayload,
)
from backend.services.context_visibility import visible_context_version_id
from backend.services.entity_ref import (
    EntityKind,
    context_page_entity_ref,
    normalize_entity_name,
    ranked_result_entity_ref,
)
from backend.services.graph_ppr import context_page_trust_priors
from backend.services.okf import context_concept_path
from backend.services.page_name_lookup import (
    NAME_KIND_PRIORITY,
    VisiblePageNameMatch,
    best_match_per_page,
    visible_page_name_matches,
)
from backend.services.read_errors import UnsupportedRetrievalModeError
from backend.services.retrieval.keyword import (
    EXCLUDED_LIBRARY_LIFECYCLE_STATUSES,
    RESTRICTED_LIBRARY_TRUST_LEVELS,
    SEARCH_SCOPES,
    KeywordRetriever,
    sqlite_symbolic_lanes_available,
)
from backend.services.retrieval.protocol import Retriever
from backend.services.search_query import QueryPlan
from backend.services.search_result_metadata import enrich_search_result_metadata

# Re-exported for backward-compatible imports (api/read.py imports these names
# from this module). The canonical definitions live in retrieval.keyword.
__all__ = [
    "SearchService",
    "ranked_result_types",
    "can_read_restricted_library_items",
    "restricted_library_permission_note",
    "SEARCH_SCOPES",
    "RESTRICTED_LIBRARY_TRUST_LEVELS",
    "EXCLUDED_LIBRARY_LIFECYCLE_STATUSES",
    "APPLIED_SEARCH_FILTERS",
    "ADVISORY_SEARCH_FILTERS",
]

logger = logging.getLogger("dtxwiki.search")

_CURSOR_VERSION = 1
APPLIED_SEARCH_FILTERS = frozenset(
    {
        "include_agents",
        "context_type",
        "status",
        "connector_type",
        "format_hint",
        "trust_level",
    }
)
ADVISORY_SEARCH_FILTERS = frozenset({"tag", "date_from", "date_to"})
_TRIGRAM_CANDIDATE_LIMIT = 200
_TRIGRAM_MIN_SIMILARITY = 0.50
_TRIGRAM_MAX_QUERY_CHARS = 160
KEYWORD_LANE_UNAVAILABLE_WARNING = "keyword_lane_unavailable"


def _encode_search_cursor(offset: int) -> str:
    raw = json.dumps({"v": _CURSOR_VERSION, "offset": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_search_cursor(cursor: str | None) -> int:
    # Tolerant: a missing, garbled, or wrong-version cursor restarts at the first
    # page (offset 0), matching the keyset list decoders (list_pagination.py).
    if not cursor:
        return 0
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            return 0
        offset = int(payload["offset"])
    except (ValueError, KeyError, TypeError):
        return 0
    return max(offset, 0)


class SearchService:
    def __init__(
        self,
        retrievers: Sequence[Retriever] | None = None,
        reranker: RerankProvider | None = None,
        rerank_candidate_k: int = 50,
        rerank_top_n: int = 8,
        associative_lane: bool = False,
        graph_service=None,
    ) -> None:
        # Default db-native backend: keyword only. Phase 6 registers the
        # semantic + hybrid retrievers here; the public API is unchanged.
        self._retrievers: list[Retriever] = (
            list(retrievers) if retrievers else [KeywordRetriever()]
        )
        # Optional post-retrieval cross-encoder rerank (Sprint-2 Phase C). None
        # by default, so ranking is unchanged unless a rerank endpoint is set.
        self._reranker = reranker
        self._rerank_candidate_k = max(1, rerank_candidate_k)
        self._rerank_top_n = max(1, rerank_top_n)
        self._associative_lane = associative_lane
        self._graph_service = graph_service

    def _supported_modes(self) -> set[str]:
        modes: set[str] = set()
        for retriever in self._retrievers:
            modes |= retriever.supported_modes()
        return modes

    def supported_modes(self) -> list[str]:
        """Public view of the retrieval modes this service can serve."""
        return sorted(self._supported_modes())

    def runtime_warnings(self, session: Session | None) -> list[str]:
        """Advertise data-plane degradation without removing request modes."""
        if session is None:
            return []
        # The dialect warning belongs to the core SQLite FTS implementation,
        # not to keyword mode as a concept. An injected search-plane retriever
        # may serve keyword retrieval perfectly well on PostgreSQL or another
        # data plane.
        if not isinstance(self._retriever_for("keyword"), KeywordRetriever):
            return []
        if sqlite_symbolic_lanes_available(session):
            return []
        return [KEYWORD_LANE_UNAVAILABLE_WARNING]

    def _retriever_for(self, mode: str) -> Retriever | None:
        for retriever in self._retrievers:
            if mode in retriever.supported_modes():
                return retriever
        return None

    def search(
        self,
        session: Session,
        workspace_id: UUID,
        query: str,
        scope: str = "all",
        filters: SearchFilters | dict[str, object] | None = None,
        retrieval_mode: str = "keyword",
        limit: int = 20,
        cursor: str | None = None,
        actor: ActorContext | None = None,
    ) -> SearchPayload:
        retriever = self._retriever_for(retrieval_mode)
        if retriever is None:
            raise UnsupportedRetrievalModeError(
                f"unsupported retrieval mode: {retrieval_mode}"
            )
        if scope not in SEARCH_SCOPES:
            scope = "all"
        plan = QueryPlan.build(query)
        active_filters = (
            SearchFilters.model_validate(filters)
            if isinstance(filters, dict)
            else filters or SearchFilters()
        )
        warnings = [
            *_unsupported_filter_warnings(active_filters),
            *self.runtime_warnings(session),
        ]
        tried_lanes: list[str] = []
        symbolic_lane_eligible = (
            session is not None
            and bool(plan.query_norm)
            and scope in {EntityKind.CONTEXT_PAGE, "all"}
        )
        direct_results = (
            self._direct_lane(
                session=session,
                workspace_id=workspace_id,
                plan=plan,
                scope=scope,
                filters=active_filters,
                actor=actor,
            )
            if symbolic_lane_eligible
            else []
        )
        explicit_direct = (
            plan.direct_page_id is not None or plan.explicit_context_slug is not None
        )
        if direct_results:
            tried_lanes.append("direct")
            ranked = sorted(
                direct_results,
                key=lambda result: (-result.score, str(result.id)),
            )
        else:
            if explicit_direct:
                tried_lanes.append("direct")
            entity_results = (
                self._entity_lane(
                    session=session,
                    workspace_id=workspace_id,
                    normalized_query=plan.query_norm,
                    scope=scope,
                    filters=active_filters,
                    actor=actor,
                )
                if symbolic_lane_eligible
                else []
            )
            if symbolic_lane_eligible:
                tried_lanes.append("entity")
            # Exact-name hits retain resolver priority without fallback reranking.
            if entity_results:
                ranked = sorted(
                    entity_results,
                    key=lambda result: (-result.score, str(result.id)),
                )
            else:
                trigram_eligible = (
                    symbolic_lane_eligible
                    and 3 <= len(plan.query_norm) <= _TRIGRAM_MAX_QUERY_CHARS
                    and plan.intent
                    in {"direct_path", "entity_lookup", "concept_lookup"}
                )
                trigram_results = (
                    self._trigram_lane(
                        session=session,
                        workspace_id=workspace_id,
                        normalized_query=plan.query_norm,
                        filters=active_filters,
                        actor=actor,
                        limit=max(limit * 4, 20),
                    )
                    if trigram_eligible
                    else []
                )
                if trigram_eligible:
                    tried_lanes.append("trigram")
                # Trigram is a fuzzy resolver candidate, not proof that other
                # source kinds are irrelevant. Compose it with the requested
                # fallback lane; only exact/direct hits short-circuit.
                fetch_limit = (
                    max(limit, self._rerank_candidate_k)
                    if self._reranker is not None
                    else limit
                )
                results = retriever.retrieve(
                    session=session,
                    workspace_id=workspace_id,
                    query=(
                        plan.keyword_query
                        if retrieval_mode == "keyword"
                        else plan.lane_query
                    ),
                    scope=scope,
                    filters=active_filters,
                    limit=fetch_limit,
                    actor=actor,
                )
                tried_lanes.append(retrieval_mode)
                if (
                    symbolic_lane_eligible
                    and results
                    and retrieval_mode in {"semantic", "hybrid"}
                ):
                    warnings = [*warnings, "semantic_fallback_used"]
                results = [
                    result
                    if result.match_reason is not None
                    else result.model_copy(update={"match_reason": retrieval_mode})
                    for result in results
                ]
                # B-10: the cross-encoder sees fallback candidates only.
                trust_priors = context_page_trust_priors(
                    session,
                    workspace_id,
                    {result.id for result in results if result.type == "context_page"},
                )
                fallback_ranked = self._maybe_rerank(
                    query=plan.raw_query,
                    ranked=sorted(
                        results,
                        key=lambda result: (
                            -result.score,
                            -trust_priors.get(result.id, 0.0),
                            str(result.id),
                        ),
                    ),
                )
                ranked = _merge_ranked_results(
                    primary=trigram_results,
                    fallback=fallback_ranked,
                )
        if self._associative_lane and ranked:
            tried_lanes.append("associative")
            associative = self._associative_results(
                session=session,
                workspace_id=workspace_id,
                ranked=ranked,
                actor=actor,
                limit=max(limit, 8),
            )
            if associative:
                fallback_reasons = {"keyword", "semantic", "hybrid"}
                insertion = next(
                    (
                        index
                        for index, result in enumerate(ranked)
                        if result.match_reason in fallback_reasons
                    ),
                    len(ranked),
                )
                ranked = _merge_ranked_results(
                    primary=[*ranked[:insertion], *associative],
                    fallback=ranked[insertion:],
                )
        if not active_filters.include_agents:
            # Apply the request policy to custom retrievers too, before counts,
            # pagination, and metadata enrichment use the ranked candidate set.
            ranked = [
                result for result in ranked if result.type != EntityKind.AGENT_PAGE
            ]
        nearest_candidates: list[dict[str, str]] = []
        if (
            not ranked
            and symbolic_lane_eligible
            and 3 <= len(plan.query_norm) <= _TRIGRAM_MAX_QUERY_CHARS
        ):
            nearest_candidates = [
                _coverage_candidate(result)
                for result in self._trigram_lane(
                    session=session,
                    workspace_id=workspace_id,
                    normalized_query=plan.query_norm,
                    filters=active_filters,
                    actor=actor,
                    limit=3,
                    min_similarity=0.15,
                )
            ]
        # Ranking is deterministic within the bounded planner candidate set, so
        # paging is a stable offset slice. total_count below is the size of this
        # set (especially for semantic mode), never a full-corpus COUNT.
        offset = _decode_search_cursor(cursor)
        window = ranked[offset : offset + limit]
        if session is not None:
            enrich_search_result_metadata(session, workspace_id, window, actor)
        next_offset = offset + limit
        has_more = len(ranked) > next_offset
        permission_note = restricted_library_permission_note(actor)
        if permission_note and "permission_limited" not in warnings:
            warnings = [*warnings, "permission_limited"]
        return SearchPayload(
            ranked_results=window,
            supported_retrieval_modes=sorted(self._supported_modes()),
            total_count=len(ranked),
            has_more=has_more,
            next_cursor=_encode_search_cursor(next_offset) if has_more else None,
            permission_note=permission_note,
            warnings=warnings,
            tried_lanes=tried_lanes,
            raw_query=plan.raw_query,
            normalized_query=plan.query_norm,
            nearest_candidates=nearest_candidates,
        )

    def _associative_results(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        ranked: list[RankedResult],
        actor: ActorContext | None,
        limit: int,
    ) -> list[RankedResult]:
        if self._graph_service is None:
            from backend.services.graph_service import GraphService

            graph_service = GraphService()
        else:
            graph_service = self._graph_service
        seeds: list[tuple[str, UUID, float]] = []
        seed_ids: set[tuple[str, UUID]] = set()
        for result in ranked[:8]:
            try:
                ref = ranked_result_entity_ref(result)
            except ValueError:
                continue
            if ref.id is None:
                continue
            seed = (ref.kind.value, ref.id)
            if seed in seed_ids:
                continue
            seed_ids.add(seed)
            seeds.append((seed[0], seed[1], max(result.score, 0.001)))
        if not seeds:
            return []
        edges = graph_service.ppr(
            session,
            workspace_id,
            seeds=seeds,
            actor=actor,
            top_k=max(limit * 2, 20),
        )
        results: list[RankedResult] = []
        for edge in edges:
            if (edge.kind, edge.id) in seed_ids or edge.kind != "context_page":
                continue
            page = session.get(ContextPage, edge.id)
            if page is None or page.workspace_id != workspace_id:
                continue
            version_id, _ = visible_context_version_id(page, actor)
            version = (
                session.get(ContextPageVersion, version_id) if version_id else None
            )
            if version is None:
                continue
            results.append(
                _ranked_context_page(
                    page=page,
                    version=version,
                    match_reason="associative",
                    matched_name=None,
                    score=edge.weight,
                )
            )
            if len(results) >= limit:
                break
        return results

    def _entity_lane(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        normalized_query: str,
        scope: str,
        filters: SearchFilters,
        actor: ActorContext | None,
    ) -> list[RankedResult]:
        if not normalized_query or scope not in {EntityKind.CONTEXT_PAGE, "all"}:
            return []
        matches = visible_page_name_matches(
            session,
            workspace_id,
            name_norm=normalized_query,
            actor=actor,
            context_types=filters.context_type,
            statuses=filters.status,
        )
        results = [
            _ranked_context_match(
                match,
                match_reason=_match_reason(match.index.name_kind),
                matched_name=match.index.name,
                score=_entity_score(match.index.name_kind),
            )
            for match in best_match_per_page(matches)
        ]
        return results

    def _direct_lane(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        plan: QueryPlan,
        scope: str,
        filters: SearchFilters,
        actor: ActorContext | None,
    ) -> list[RankedResult]:
        if scope not in {EntityKind.CONTEXT_PAGE, "all"}:
            return []
        page_id = plan.direct_page_id
        if page_id is not None:
            page = session.get(ContextPage, page_id)
            if (
                page is None
                or page.workspace_id != workspace_id
                or page.lifecycle_status == "archived"
                or not _page_matches_context_filters(page, filters)
            ):
                return []
            version_id, _is_draft = visible_context_version_id(page, actor)
            version = (
                session.get(ContextPageVersion, version_id)
                if version_id is not None
                else None
            )
            if version is None or version.page_id != page.id:
                return []
            return [
                _ranked_context_page(
                    page=page,
                    version=version,
                    match_reason="exact_id",
                    matched_name=str(page.id),
                    score=10020.0,
                )
            ]

        slug = plan.context_slug
        if slug is None:
            return []
        matches = visible_page_name_matches(
            session,
            workspace_id,
            name_norm=normalize_entity_name(slug),
            actor=actor,
            name_kinds={"slug"},
            context_types=filters.context_type,
            statuses=filters.status,
        )
        if plan.explicit_context_slug is None:
            same_name_pages = best_match_per_page(
                visible_page_name_matches(
                    session,
                    workspace_id,
                    name_norm=plan.query_norm,
                    actor=actor,
                    context_types=filters.context_type,
                    statuses=filters.status,
                )
            )
            if len(same_name_pages) != 1:
                return []
        results = [
            _ranked_context_match(
                match,
                match_reason="exact_slug",
                matched_name=match.index.name,
                score=10010.0,
            )
            for match in best_match_per_page(matches)
        ]
        return results

    def _trigram_lane(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        normalized_query: str,
        filters: SearchFilters,
        actor: ActorContext | None,
        limit: int,
        min_similarity: float = _TRIGRAM_MIN_SIMILARITY,
    ) -> list[RankedResult]:
        if not sqlite_symbolic_lanes_available(session):
            return []
        match_query = _trigram_match_query(normalized_query)
        if not match_query:
            return []
        visibility_scopes = ["published"]
        if actor_can_read_drafts(actor):
            visibility_scopes.append("draft")
        hits: list[PageNameTrigramHit] = WorkspaceFts5ProjectionRepository(
            session, workspace_id
        ).search_page_names(
            match_query=match_query,
            visibility_scopes=visibility_scopes,
            take=min(_TRIGRAM_CANDIDATE_LIMIT, max(limit * 4, 20)),
        )
        if not hits:
            return []
        hits_by_key = {
            (
                hit.page_id,
                hit.source_version_id,
                hit.name_kind,
                hit.name_norm,
            ): hit
            for hit in hits
        }
        matches = visible_page_name_matches(
            session,
            workspace_id,
            actor=actor,
            page_ids={hit.page_id for hit in hits},
            context_types=filters.context_type,
            statuses=filters.status,
        )
        best_by_page: dict[UUID, tuple[float, VisiblePageNameMatch]] = {}
        for match in matches:
            hit = hits_by_key.get(
                (
                    match.page.id,
                    match.version.id,
                    match.index.name_kind,
                    match.index.name_norm,
                )
            )
            if hit is None:
                continue
            similarity = _trigram_similarity(
                normalized_query,
                match.index.name_norm,
            )
            if similarity < min_similarity:
                continue
            current = best_by_page.get(match.page.id)
            candidate_order = (
                -similarity,
                NAME_KIND_PRIORITY.get(match.index.name_kind, 9),
                hit.rank,
                hit.rowid,
            )
            if current is None:
                best_by_page[match.page.id] = (similarity, match)
                continue
            current_similarity, current_match = current
            current_hit = hits_by_key[
                (
                    current_match.page.id,
                    current_match.version.id,
                    current_match.index.name_kind,
                    current_match.index.name_norm,
                )
            ]
            current_order = (
                -current_similarity,
                NAME_KIND_PRIORITY.get(current_match.index.name_kind, 9),
                current_hit.rank,
                current_hit.rowid,
            )
            if candidate_order < current_order:
                best_by_page[match.page.id] = (similarity, match)
        results = [
            _ranked_context_match(
                match,
                match_reason="trigram",
                matched_name=match.index.name,
                score=9000.0 + similarity * 100.0,
            )
            for similarity, match in best_by_page.values()
        ]
        return sorted(
            results,
            key=lambda result: (-result.score, str(result.id)),
        )[:limit]

    def _maybe_rerank(
        self, *, query: str, ranked: list[RankedResult]
    ) -> list[RankedResult]:
        if self._reranker is None or len(ranked) <= 1:
            return ranked
        candidates = ranked[: self._rerank_candidate_k]
        tail = ranked[self._rerank_candidate_k :]
        documents = [
            f"{result.title}\n{result.snippet}".strip() for result in candidates
        ]
        try:
            response = self._reranker.rerank(
                RerankRequest(
                    query=query,
                    documents=documents,
                    top_n=self._rerank_top_n,
                )
            )
        except Exception:
            # Reranking is a quality enhancement, not a correctness requirement:
            # an unreachable/failing endpoint falls back to the fusion order
            # rather than failing the whole search. Log it though, or a bad
            # reranker URL/auth/model silently disables reranking with no
            # operator-visible signal (Codex F5 / merged I8).
            logger.warning(
                "reranker failed; falling back to fusion order (reranker=%s)",
                type(self._reranker).__name__,
                exc_info=True,
            )
            return ranked
        reordered: list[RankedResult] = []
        seen: set[int] = set()
        for item in response.results:
            if item.index in seen or not 0 <= item.index < len(candidates):
                continue
            seen.add(item.index)
            reordered.append(
                candidates[item.index].model_copy(update={"score": item.score})
            )
        # The reranker may return only its top_n; keep every retrieved result by
        # appending the candidates it dropped (in fusion order) then the
        # un-reranked tail, so pagination past top_n still returns them all.
        leftover = [
            candidate for index, candidate in enumerate(candidates) if index not in seen
        ]
        return reordered + leftover + tail


def ranked_result_types(results: Iterable[RankedResult]) -> set[str]:
    return {result.type for result in results}


def can_read_restricted_library_items(actor: ActorContext | None) -> bool:
    # Delegates to the single policy predicate so REST/MCP/retrieval agree.
    return actor_can_read_restricted_library(actor)


def restricted_library_permission_note(actor: ActorContext | None) -> str | None:
    if can_read_restricted_library_items(actor):
        return None
    return "Restricted Library items and artifacts were excluded."


def build_search_service(settings, *, graph_service=None) -> SearchService:
    """Compose the SearchService for the configured backend. Keyword-only when
    no embedding provider is set; otherwise keyword + semantic + hybrid so
    supported_retrieval_modes advertises {keyword, semantic, hybrid}."""
    from backend.llm.registry import resolve_embedding_provider, resolve_reranker
    from backend.services.retrieval.hybrid import HybridRetriever
    from backend.services.retrieval.semantic import SemanticRetriever
    from backend.services.vector_store import build_vector_store

    keyword = KeywordRetriever()
    # A misconfigured embedding/rerank provider costs only the lanes it powers;
    # keyword search must keep answering instead of turning every search and Ask
    # request into a 500 at dependency-resolution time.
    reranker, rerank_reason = resolve_reranker(settings)
    embedder, embedding_reason = resolve_embedding_provider(settings)
    degraded_reasons = [
        reason for reason in (embedding_reason, rerank_reason) if reason
    ]
    if degraded_reasons:
        logger.warning(
            "search provider configuration degraded: %s",
            "; ".join(degraded_reasons),
        )
    if embedder is None:
        return SearchService(
            retrievers=[keyword],
            reranker=reranker,
            rerank_candidate_k=settings.rerank_candidate_k,
            rerank_top_n=settings.rerank_top_n,
            associative_lane=settings.search_associative_lane,
            graph_service=graph_service,
        )
    # A model/dim swap without reindex is detected at query time by
    # SemanticRetriever._warn_if_stale_index (it reads the dim/provider/model the
    # index was actually built with). A build-time assert here would be a
    # tautology: the provider's width is derived from settings.embedding_dim
    # (registry.build_embedding_provider), so it can never disagree with it.
    vector_store = build_vector_store()
    semantic = SemanticRetriever(embedder=embedder, vector_store=vector_store)
    hybrid = HybridRetriever(keyword=keyword, semantic=semantic)
    return SearchService(
        retrievers=[keyword, semantic, hybrid],
        reranker=reranker,
        rerank_candidate_k=settings.rerank_candidate_k,
        rerank_top_n=settings.rerank_top_n,
        associative_lane=settings.search_associative_lane,
        graph_service=graph_service,
    )


def _ranked_context_match(
    match: VisiblePageNameMatch,
    *,
    match_reason: str,
    matched_name: str | None,
    score: float,
) -> RankedResult:
    return _ranked_context_page(
        page=match.page,
        version=match.version,
        match_reason=match_reason,
        matched_name=matched_name,
        score=score,
    )


def _ranked_context_page(
    *,
    page: ContextPage,
    version: ContextPageVersion,
    match_reason: str,
    matched_name: str | None,
    score: float,
) -> RankedResult:
    entity_ref = context_page_entity_ref(
        workspace_id=page.workspace_id,
        page_id=page.id,
        slug=page.slug,
        version_id=version.id,
    )
    result = RankedResult(
        id=page.id,
        type=EntityKind.CONTEXT_PAGE.value,
        title=page.title,
        snippet="",
        matched_name=matched_name,
        score=score,
        citation_available=True,
        archived=False,
        concept_path=context_concept_path(page.slug),
        concept_type=page.page_type,
        version_id=version.id,
        source_ref={"kind": EntityKind.CONTEXT_PAGE.value, "id": str(page.id)},
        match_reason=match_reason,
        entity_ref=entity_ref.source_ref(),
    )
    result._summary_markdown = version.body_md
    return result


def _page_matches_context_filters(
    page: ContextPage,
    filters: SearchFilters,
) -> bool:
    if filters.context_type and page.page_type not in filters.context_type:
        return False
    return not (filters.status and page.lifecycle_status not in filters.status)


def _merge_ranked_results(
    *,
    primary: list[RankedResult],
    fallback: list[RankedResult],
) -> list[RankedResult]:
    merged: list[RankedResult] = []
    seen: set[tuple[str, UUID]] = set()
    for result in [*primary, *fallback]:
        key = (result.type, result.id)
        if result.source_ref and result.source_ref.get("kind") == "content_chunk":
            try:
                ref = ranked_result_entity_ref(result)
            except ValueError:
                pass
            else:
                key = (ref.kind.value, ref.id or result.id)
        if key in seen:
            continue
        seen.add(key)
        merged.append(result)
    return merged


def _coverage_candidate(result: RankedResult) -> dict[str, str]:
    ref = ranked_result_entity_ref(result)
    return {
        "id": str(ref.id or result.id),
        "kind": ref.kind.value,
        "title": result.title,
        "concept_path": result.concept_path or "",
        "match_reason": result.match_reason or "trigram",
    }


def _match_reason(name_kind: str) -> str:
    if name_kind == "title":
        return "exact_title"
    if name_kind == "alias":
        return "exact_alias"
    return "exact_slug"


def _entity_score(name_kind: str) -> float:
    if name_kind == "slug":
        return 10000.0
    if name_kind == "title":
        return 9990.0
    return 9980.0


def _trigram_match_query(query_norm: str) -> str:
    terms = _trigrams(query_norm)
    return " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"' for term in sorted(terms)
    )


def _trigram_similarity(query_norm: str, candidate_norm: str) -> float:
    query_terms = _trigrams(query_norm)
    candidate_terms = _trigrams(candidate_norm)
    if not query_terms or not candidate_terms:
        return 0.0
    overlap = len(query_terms & candidate_terms)
    coverage = overlap / len(query_terms)
    dice = (2.0 * overlap) / (len(query_terms) + len(candidate_terms))
    return 0.7 * coverage + 0.3 * dice


def _trigrams(value: str) -> set[str]:
    if len(value) < 3:
        return set()
    return {
        value[index : index + 3]
        for index in range(len(value) - 2)
        if value[index : index + 3].strip()
    }


def _unsupported_filter_warnings(filters: SearchFilters) -> list[str]:
    warnings: list[str] = []
    if filters.tag:
        warnings.append("unsupported_filter_tag")
    if filters.date_from is not None or filters.date_to is not None:
        warnings.append("unsupported_filter_date")
    return warnings
