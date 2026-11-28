from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import exists, func, select, tuple_
from sqlalchemy.orm import Session

from backend.llm.binding import ModelBinding
from backend.models.ask import (
    ASK_TURN_COMPLETED,
    AskTurn,
)
from backend.models.chunk import ContentChunk
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace, select_in_workspace
from backend.schemas.read import RankedResult
from backend.services.ask_composer import _history_within_context
from backend.services.ask_types import (
    ASK_HISTORY_MAX_CHARS,
    ASK_HISTORY_MAX_PAIRS,
    UNSUPPORTED_RETRIEVAL_MODE_FALLBACK_WARNING,
    AskEvidence,
    PreparedTurn,
    RetrievedEvidence,
    _workspace_answer_language,
)
from backend.services.ask_visibility import ask_evidence_visible
from backend.services.library_canonical import (
    canonical_library_artifact_belongs_to_item,
    get_canonical_library_version,
    get_canonical_markdown_artifact,
    get_effective_library_markdown,
)
from backend.services.read_errors import ReadNotFoundError
from backend.services.retrieval.identity import ranked_result_document_id
from backend.services.search_service import SearchService
from backend.utils.uuid import parse_uuid_or_none

# Rows read at a time while looking for the history.
_HISTORY_BATCH = 20


def _stable_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


class AskRetriever:
    def __init__(self, search_service: SearchService) -> None:
        self.search_service = search_service

    def prepare_turn(
        self,
        session: Session,
        workspace_id: UUID,
        turn_id: UUID,
        actor: ActorContext | None = None,
        *,
        binding: ModelBinding | None = None,
        keyword_only: bool = False,
    ) -> PreparedTurn:
        """Find this question's evidence and, for ``binding``, the earlier
        turns to show it."""

        turn = get_in_workspace(
            session, AskTurn, workspace_id=workspace_id, resource_id=turn_id
        )
        if turn is None:
            raise ReadNotFoundError("ask turn not found")
        question = turn.user_message or ""
        retrieved = self._retrieve(
            session, workspace_id, question, actor, keyword_only=keyword_only
        )
        # Earlier turns only matter when a model is asked.
        history = (
            self._history(session, workspace_id, turn, actor, target=binding.target)
            if binding is not None and retrieved.pack
            else ()
        )
        if binding is not None and binding.context_window is not None:
            history = _history_within_context(history, question, retrieved, binding)
        evidence = retrieved.evidence
        for _question, _answer, earlier in history:
            evidence = evidence.union(earlier)
        return PreparedTurn(
            turn_id=turn.id,
            ask_session_id=turn.ask_session_id,
            question=question,
            retrieved=retrieved,
            history=tuple((asked, answered) for asked, answered, _ in history),
            evidence=evidence,
        )

    def _history(
        self,
        session: Session,
        workspace_id: UUID,
        turn: AskTurn,
        actor: ActorContext | None,
        *,
        target: dict[str, str],
    ) -> tuple[tuple[str, str, AskEvidence], ...]:
        """Earlier answered turns to show the model, oldest first.

        Only turns answered for the same model are used, so changing the model
        starts a new history; a turn stored before evidence was recorded ends
        it as well. Turns the asker can no longer see are left out.
        """

        picked: list[tuple[str, str, AskEvidence]] = []
        size = 0
        # A replacement can run before previously reserved questions even
        # though it was submitted later. History follows execution order.
        executed_at = func.coalesce(AskTurn.started_at, AskTurn.created_at)
        before = (turn.started_at or turn.created_at, turn.turn_no)
        visibility_cache: dict[tuple[str, UUID, UUID | None], bool] = {}
        while True:
            rows = session.scalars(
                select_in_workspace(
                    AskTurn,
                    workspace_id=workspace_id,
                    criteria=(
                        AskTurn.ask_session_id == turn.ask_session_id,
                        tuple_(executed_at, AskTurn.turn_no) < before,
                        AskTurn.status == ASK_TURN_COMPLETED,
                    ),
                )
                .order_by(executed_at.desc(), AskTurn.turn_no.desc())
                .limit(_HISTORY_BATCH)
            ).all()
            for row in rows:
                if row.input_evidence is None:
                    return tuple(reversed(picked))
                if not isinstance(row.generation, Mapping):
                    return tuple(reversed(picked))
                generation = row.generation
                # Only model answers continue a conversation: results without
                # a model are skipped, and a different model's answer ends it.
                if generation.get("outcome") != "completed":
                    continue
                if generation.get("target") != target:
                    return tuple(reversed(picked))
                evidence = AskEvidence.from_record(row.input_evidence)
                if evidence is None or not ask_evidence_visible(
                    session,
                    context_refs=evidence.context,
                    artifact_ids=evidence.library,
                    actor=actor,
                    workspace_id=workspace_id,
                    cache=visibility_cache,
                ):
                    continue
                asked = "\n\n".join(
                    [
                        row.user_message or "",
                        *(
                            message["user_message"]
                            for message in (row.steering_messages or [])
                            if message["status"] == "applied"
                        ),
                    ]
                )
                answered = row.assistant_message or ""
                size += len(asked) + len(answered)
                if size > ASK_HISTORY_MAX_CHARS:
                    return tuple(reversed(picked))
                picked.append((asked, answered, evidence))
                if len(picked) == ASK_HISTORY_MAX_PAIRS:
                    return tuple(reversed(picked))
            if len(rows) < _HISTORY_BATCH:
                return tuple(reversed(picked))
            last = rows[-1]
            before = (last.started_at or last.created_at, last.turn_no)

    def _workspace_retrieval_mode(
        self, session: Session, workspace_id: UUID
    ) -> str | None:
        return session.scalar(
            select(Workspace.ask_retrieval_mode).where(Workspace.id == workspace_id)
        )

    def _best_retrieval_mode(self, preferred: str | None = None) -> tuple[str, bool]:
        """The mode to search with, and whether the workspace's choice was kept.

        `preferred` is the workspace policy. An unsupported one must not raise:
        `SearchService` rejects an unknown mode outright, which is right for a
        caller who asked for it explicitly and wrong for a stored setting that
        an operator changed months ago -- Ask would 500 for everyone until
        someone found the row. Fall back and say so instead.
        """

        supported = getattr(self.search_service, "supported_modes", None)
        modes = supported() if callable(supported) else {"keyword"}
        if preferred and preferred != "auto":
            if preferred in modes:
                return preferred, True
            return self._ladder_mode(modes), False
        return self._ladder_mode(modes), True

    @staticmethod
    def _ladder_mode(modes) -> str:
        # hybrid -> semantic -> keyword so Ask rides the vector tier when an
        # embedding provider is configured, falling back to keyword otherwise.
        for mode in ("hybrid", "semantic", "keyword"):
            if mode in modes:
                return mode
        return "keyword"

    def _retrieve(
        self,
        session: Session,
        workspace_id: UUID,
        question: str,
        actor: ActorContext | None = None,
        *,
        keyword_only: bool = False,
    ) -> RetrievedEvidence:
        """Search for the question's evidence.

        Only hits whose source is proven enter the pack, the trace and the
        citations: a hit that cannot be tied to the document it came from
        could not be hidden later from readers who lose access to it.
        """

        mode, honored = (
            ("keyword", True)
            if keyword_only
            else self._best_retrieval_mode(
                self._workspace_retrieval_mode(session, workspace_id)
            )
        )
        search_options = {"allow_rerank": False} if keyword_only else {}
        context_payload = self.search_service.search(
            session=session,
            workspace_id=workspace_id,
            query=question,
            scope="context_page",
            retrieval_mode=mode,
            limit=3,
            actor=actor,
            citable_only=True,
            **search_options,
        )
        context = context_payload.ranked_results[:3]
        tried_lanes = list(context_payload.tried_lanes)
        search_warnings = list(context_payload.warnings)
        nearest_candidates = list(context_payload.nearest_candidates)
        if not honored:
            search_warnings = [
                *search_warnings,
                UNSUPPORTED_RETRIEVAL_MODE_FALLBACK_WARNING,
            ]
        fallback = False
        results: list[RankedResult] = context
        if not results:
            fallback = True
            library_payload = self.search_service.search(
                session=session,
                workspace_id=workspace_id,
                query=question,
                scope="library_artifact",
                retrieval_mode=mode,
                limit=3,
                actor=actor,
                **search_options,
            )
            results = library_payload.ranked_results
            nearest_candidates = [
                *nearest_candidates,
                *library_payload.nearest_candidates,
            ][:3]
            tried_lanes = _stable_unique(
                [*tried_lanes, "library_fallback", *library_payload.tried_lanes]
            )
            search_warnings = _stable_unique(
                [*search_warnings, *library_payload.warnings]
            )
        pack: list[str] = []
        context_refs: dict[UUID, UUID | None] = {}
        artifact_ids: list[UUID] = []
        trace: list[dict[str, str]] = []
        for result in results:
            evidence_id = self._result_document_id(result) or result.id
            if result.type == "context_page":
                # The answer is checked later against the version it read.
                if result.version_id is None or evidence_id in context_refs:
                    continue
                context_refs[evidence_id] = result.version_id
            elif result.type in ("library_artifact", "library_item"):
                artifact_id = self._library_artifact_id(
                    session,
                    workspace_id=workspace_id,
                    result=result,
                )
                if artifact_id is None or artifact_id in artifact_ids:
                    continue
                artifact_ids.append(artifact_id)
                evidence_id = artifact_id
            else:
                continue
            pack.append(f"{len(pack) + 1}. {result.title}: {result.snippet}")
            evidence_trace = {
                "id": str(evidence_id),
                "type": result.type,
                "title": result.title,
                "score": f"{result.score:.2f}",
                "match_reason": result.match_reason or "",
                "stable_url": result.stable_url or "",
            }
            if result.type == "context_page" and result.version_id is not None:
                evidence_trace["version_id"] = str(result.version_id)
                evidence_trace["version_status"] = result.version_status or ""
                evidence_trace["citation_available"] = str(
                    result.citation_available
                ).lower()
            trace.append(evidence_trace)
        return RetrievedEvidence(
            language=_workspace_answer_language(session, workspace_id),
            pack=tuple(pack),
            context_ids=tuple(context_refs),
            artifact_ids=tuple(artifact_ids),
            evidence=AskEvidence(
                context=frozenset(context_refs.items()),
                library=frozenset(artifact_ids),
            ),
            trace=tuple(trace),
            tried_lanes=tuple(tried_lanes),
            fallback=fallback,
            nearest_candidates=tuple(nearest_candidates),
            warnings=tuple(search_warnings),
            found=bool(results),
        )

    @staticmethod
    def _result_document_id(result: RankedResult) -> UUID | None:
        return ranked_result_document_id(result)

    def _library_artifact_id(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        result: RankedResult,
    ) -> UUID | None:
        """Resolve a library result to the extracted-text artifact id Ask cites.

        Keyword ``library_artifact`` hits already identify an artifact, but the
        id is still checked against the item's canonical selected-attempt set.
        Semantic/hybrid ``library_item`` hits map through the same canonical
        ready version and Markdown artifact that the indexer consumed.
        """
        if result.type == "library_artifact":
            artifact = get_in_workspace(
                session,
                LibraryArtifact,
                workspace_id=workspace_id,
                resource_id=result.id,
            )
            if artifact is None:
                return None
            version = get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=artifact.library_item_version_id,
            )
            if version is None:
                return None
            item = get_in_workspace(
                session,
                LibraryItem,
                workspace_id=workspace_id,
                resource_id=version.library_item_id,
            )
            if item is None:
                return None
            return (
                artifact.id
                if canonical_library_artifact_belongs_to_item(
                    session,
                    item=item,
                    artifact_id=artifact.id,
                )
                else None
            )
        item_id = self._result_document_id(result)
        if item_id is None:
            return None
        # Read the canonical pointers fresh: the search may have loaded these
        # rows before a newer conversion was promoted.
        item = session.scalar(
            select(LibraryItem)
            .where(LibraryItem.id == item_id, LibraryItem.workspace_id == workspace_id)
            .execution_options(populate_existing=True)
        )
        if item is None:
            return None
        version = get_canonical_library_version(session, item)
        if version is None or version.id != result.version_id:
            return None
        version = session.scalar(
            select(LibraryItemVersion)
            .where(
                LibraryItemVersion.id == version.id,
                LibraryItemVersion.workspace_id == workspace_id,
                LibraryItemVersion.library_item_id == item.id,
            )
            .execution_options(populate_existing=True)
        )
        if version is None:
            return None
        artifact = get_canonical_markdown_artifact(session, version)
        if artifact is None or not _library_chunk_is_current(
            session,
            workspace_id=workspace_id,
            item=item,
            version=version,
            result=result,
        ):
            return None
        return artifact.id


def _library_chunk_is_current(
    session: Session,
    *,
    workspace_id: UUID,
    item: LibraryItem,
    version: LibraryItemVersion,
    result: RankedResult,
) -> bool:
    """Whether a semantic hit's chunk was cut from ``version``'s current text.

    Chunks do not name the conversion attempt they came from. Promotion swaps
    the canonical pointer and deletes the old attempt's chunks in one
    transaction, and the indexer hashes the same text this selects, so a chunk
    that still exists with this hash after the pointer read belongs to it. A
    fresh query, not the identity map, so a chunk loaded earlier cannot answer.
    """

    source = result.source_ref or {}
    chunk_id = (
        parse_uuid_or_none(source.get("id"))
        if source.get("kind") == "content_chunk"
        else None
    )
    if chunk_id is None:
        return False
    effective = get_effective_library_markdown(session, version)
    content_hash = (
        effective.content_sha256
        if effective is not None and effective.overlay is not None
        else version.content_hash
    )
    return bool(
        session.scalar(
            select(
                exists().where(
                    ContentChunk.id == chunk_id,
                    ContentChunk.workspace_id == workspace_id,
                    ContentChunk.document_kind == "library_item",
                    ContentChunk.document_id == item.id,
                    ContentChunk.source_version_id == version.id,
                    ContentChunk.source_content_hash == content_hash,
                )
            )
        )
    )
