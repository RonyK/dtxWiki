"""SemanticRetriever — Epic-1 Sprint-1 Phase 6.

Embeds the query, searches the VectorStore for the active (provider, model),
and maps the chunk hits back to RankedResults carrying the OKF concept pointer
(path/type/version/heading_path). Per-actor authorization is applied here, after
the vector search (the index physically holds restricted/draft chunks, §7.3-7.4):
draft chunks only for draft-capable actors, restricted Library only for allowed
actors. Over-fetches before filtering so the top-N survives the filter.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.llm.base import EmbeddingProvider, EmbeddingRequest, EmbeddingResponse
from backend.models.chunk import ChunkEmbedding, ContentChunk
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryItem
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import (
    actor_can_read_drafts,
    actor_can_read_restricted_library,
)
from backend.schemas.read import RankedResult, SearchFilters
from backend.services.context_visibility import visible_search_version_ids
from backend.services.entity_ref import (
    EntityKind,
    context_page_entity_ref,
    library_item_entity_ref,
)
from backend.services.library_canonical import (
    get_canonical_library_version,
    get_canonical_markdown_artifact,
    get_effective_library_markdown,
)
from backend.services.okf import context_concept_path, library_concept_path
from backend.services.retrieval.keyword import (
    EXCLUDED_LIBRARY_LIFECYCLE_STATUSES,
    RESTRICTED_LIBRARY_TRUST_LEVELS,
)
from backend.services.retrieval.outline_location import (
    heading_location_for_offset,
    public_heading_path,
)
from backend.services.vector_store import VectorStore

logger = logging.getLogger("dtxwiki.retrieval.semantic")

_OVERFETCH = 4
_SNIPPET_CHARS = 240


def _document_kinds_for_scope(scope: str) -> set[str]:
    if scope in {EntityKind.CONTEXT_PAGE, "context_claim"}:
        return {EntityKind.CONTEXT_PAGE.value}
    if scope in {EntityKind.LIBRARY_ITEM, EntityKind.LIBRARY_ARTIFACT}:
        return {EntityKind.LIBRARY_ITEM.value}
    return {EntityKind.CONTEXT_PAGE.value, EntityKind.LIBRARY_ITEM.value}


class SemanticRetriever:
    def __init__(
        self, *, embedder: EmbeddingProvider, vector_store: VectorStore
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store

    def supported_modes(self) -> set[str]:
        return {"semantic"}

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
        if not query.strip():
            return []
        response = self.embedder.embed(EmbeddingRequest(texts=[query]))
        query_vector = response.embeddings[0]

        scopes = {"published"}
        if actor_can_read_drafts(actor):
            scopes.add("draft")
        kinds = _document_kinds_for_scope(scope)
        allow_restricted = actor_can_read_restricted_library(actor)

        candidates = self.vector_store.search_similar(
            session=session,
            workspace_id=workspace_id,
            query_vector=query_vector,
            provider=response.provider,
            model=response.model,
            limit=limit * _OVERFETCH,
            content_scopes=scopes,
        )
        if not candidates:
            self._warn_if_stale_index(session, workspace_id, response)
            return []

        results: list[RankedResult] = []
        for chunk_id, similarity in candidates:
            chunk = session.get(ContentChunk, chunk_id)
            if chunk is None or chunk.document_kind not in kinds:
                continue
            mapped = self._to_result(
                session,
                chunk,
                similarity,
                actor=actor,
                allow_restricted=allow_restricted,
                filters=filters,
            )
            if mapped is not None:
                results.append(mapped)
        # Return the full filtered top-K (bounded by the over-fetch window) and
        # let the dispatcher own offset/limit pagination; capping at ``limit``
        # here would make total_count <= limit and break page 2 (§ dispatcher
        # ranks + paginates, protocol.py).
        return results

    def _warn_if_stale_index(
        self, session: Session, workspace_id: UUID, response: EmbeddingResponse
    ) -> None:
        """Distinguish "nothing indexed" from "model swapped without reindex".

        ``search_similar`` filters by the active (provider, model); if the index
        only holds embeddings under a *different* model, it silently returns
        nothing. Surface that so the operator knows to reindex rather than
        assuming the corpus is empty.
        """
        stale = session.scalar(
            select(ChunkEmbedding.id)
            .join(ContentChunk, ChunkEmbedding.chunk_id == ContentChunk.id)
            .where(ContentChunk.workspace_id == workspace_id)
            .where(
                (ChunkEmbedding.provider != response.provider)
                | (ChunkEmbedding.model != response.model)
            )
            .limit(1)
        )
        if stale is not None:
            logger.warning(
                "semantic search found no candidates for the active embedding "
                "model %s/%s, but workspace %s holds embeddings under a different "
                "model; run `dtxwiki reindex` after changing the embedding model.",
                response.provider,
                response.model,
                workspace_id,
            )

    def _to_result(
        self,
        session: Session,
        chunk: ContentChunk,
        similarity: float,
        *,
        actor: ActorContext | None,
        allow_restricted: bool,
        filters: SearchFilters,
    ) -> RankedResult | None:
        if chunk.document_kind == "library_item":
            item = session.get(LibraryItem, chunk.document_id)
            if item is None:
                return None
            # Lifecycle parity with the keyword retriever (keyword.py): archived
            # and hidden Library items are excluded from all retrieval modes —
            # the index physically retains their chunks (archiving enqueues no
            # remove), so the filter must live here too.
            if item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
                return None
            # Chunks are a rebuildable projection and an interrupted/late
            # embed job may temporarily leave an older version behind.  Never
            # expose that stale generation: only the item's canonical ready
            # version is eligible for semantic retrieval.  The canonical
            # helper retains the pre-0005 completed-version fallback.
            canonical_version = get_canonical_library_version(session, item)
            if (
                canonical_version is None
                or chunk.source_version_id != canonical_version.id
            ):
                return None
            if (
                item.trust_level in RESTRICTED_LIBRARY_TRUST_LEVELS
                and not allow_restricted
            ):
                return None
            # User SearchFilters parity with keyword.py (_search_library_items):
            # semantic candidates come from the vector index, which has no
            # knowledge of these filters, so they must be applied post-hoc or the
            # mode leaks out-of-filter documents into search/hybrid/Ask.
            if (
                filters.connector_type
                and item.connector_type not in filters.connector_type
            ):
                return None
            if filters.format_hint and item.format_hint not in filters.format_hint:
                return None
            if filters.trust_level and item.trust_level not in filters.trust_level:
                return None
            if filters.status and item.lifecycle_status not in filters.status:
                return None
            title = item.title or item.uri
            concept_type = "library_item"
            concept_path = library_concept_path(item.id)
            result_id = item.id
            result_kind = EntityKind.LIBRARY_ITEM
            entity_ref = library_item_entity_ref(
                workspace_id=item.workspace_id,
                item_id=item.id,
                version_id=chunk.source_version_id,
            )
            effective = get_effective_library_markdown(session, canonical_version)
            artifact = get_canonical_markdown_artifact(session, canonical_version)
            body_md = (
                effective.body_markdown
                if effective is not None and effective.overlay is not None
                else (
                    artifact.conversion_detail.index_text
                    if artifact is not None
                    else None
                )
            )
        else:
            page = session.get(ContextPage, chunk.document_id)
            if page is None:
                return None
            # Vector rows are rebuildable projections and may outlive an
            # archive or an old version. Match the same lifecycle/head set as
            # keyword search before exposing the chunk (F-1/R-14).
            if page.lifecycle_status == "archived":
                return None
            if chunk.source_version_id not in visible_search_version_ids(page, actor):
                return None
            # User SearchFilters parity with keyword.py (_search_context_pages).
            if filters.context_type and page.page_type not in filters.context_type:
                return None
            if filters.status and page.lifecycle_status not in filters.status:
                return None
            title = page.title
            concept_type = page.page_type
            concept_path = context_concept_path(page.slug)
            result_id = page.id
            result_kind = EntityKind.CONTEXT_PAGE
            entity_ref = context_page_entity_ref(
                workspace_id=page.workspace_id,
                page_id=page.id,
                slug=page.slug,
                version_id=chunk.source_version_id,
            )
            version = session.get(ContextPageVersion, chunk.source_version_id)
            if version is None or version.page_id != page.id:
                return None
            body_md = version.body_md
        location = (
            heading_location_for_offset(body_md, chunk.char_start)
            if body_md is not None
            else None
        )
        result = RankedResult(
            # Public search identity is the document. The chunk remains in
            # source_ref/heading_path as provenance, so multiple matching
            # chunks collapse without losing the evidence location (F-2/R-15).
            id=result_id,
            type=result_kind.value,
            title=title,
            snippet=" ".join(chunk.chunk_text.split())[:_SNIPPET_CHARS],
            score=similarity,
            citation_available=True,
            concept_path=concept_path,
            concept_type=concept_type,
            version_id=chunk.source_version_id,
            heading_path=public_heading_path(location),
            section_id=location.section_id if location is not None else None,
            source_ref={
                "kind": "content_chunk",
                "id": str(chunk.id),
                "document_kind": chunk.document_kind,
                "document_id": str(chunk.document_id),
                "content_scope": chunk.content_scope,
            },
            entity_ref=entity_ref.source_ref(),
        )
        result._summary_markdown = body_md
        return result
