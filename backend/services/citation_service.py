import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.context_write import (
    CitationCandidate,
    CiteRequest,
    CiteResult,
)
from backend.services.actor_visible_read import (
    actor_can_read_library_trust_level,
    context_page_visible,
)
from backend.services.context_visibility import (
    visible_context_version_expression,
)
from backend.services.library_canonical import (
    get_canonical_library_version,
    get_canonical_markdown_artifact,
)
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
from backend.services.read_errors import ReadNotFoundError

CITATION_SCAN_LIMIT = 500
CITATION_SCORE_TEXT_MAX_CHARS = 100_000
CJK_SEQUENCE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")


@dataclass(frozen=True)
class ScoredCandidate:
    score: float
    title: str
    candidate: CitationCandidate


class CitationService:
    def cite(
        self,
        session: Session,
        workspace_id: UUID,
        request: CiteRequest,
        *,
        actor: ActorContext | None,
    ) -> CiteResult:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        if request.target_page_id is not None:
            page = session.get(ContextPage, request.target_page_id)
            if not context_page_visible(page, actor, workspace_id=workspace_id):
                raise ReadNotFoundError("target context page not found")

        accepted_types = set(
            workspace.citation_policy.get("accepted_citation_types")
            or {"raw_source", "derived_artifact", "context_page"}
        )
        candidates: list[ScoredCandidate] = []
        if request.desired_citation_type in {
            "raw_source",
            "both",
        } and accepted_types.intersection({"raw_source", "derived_artifact"}):
            candidates.extend(
                self._source_candidates(
                    session=session,
                    workspace_id=workspace_id,
                    request=request,
                    actor=actor,
                )
            )
        if (
            request.desired_citation_type in {"context_page", "both"}
            and "context_page" in accepted_types
        ):
            candidates.extend(
                self._context_page_candidates(
                    session=session,
                    workspace_id=workspace_id,
                    request=request,
                    actor=actor,
                )
            )

        ordered = sorted(
            candidates,
            key=lambda item: (-item.score, item.title, item.candidate.snippet),
        )
        # Codex #15: validate provenance only for the candidates actually returned
        # (bounded to `limit`, NOT the full scan — no N+1 over scanned pages). A
        # context page with no transitive raw-source lineage is a dead-end: flag
        # it and never recommend it as 'use', so ungrounded context-to-context
        # chains are not built. Reuse the write-path lineage check (DRY); lazy
        # import avoids a module-level cycle with context_write_service.
        from backend.services.context_write_service import ContextWriteService

        selected: list[CitationCandidate] = []
        for scored in ordered[: request.limit]:
            cand = scored.candidate
            if (
                cand.citation_type == "context_page"
                and cand.cited_context_page_version_id is not None
                and not ContextWriteService._has_raw_lineage(
                    session, cand.cited_context_page_version_id, set()
                )
            ):
                cand = cand.model_copy(
                    update={
                        "recommendation": "needs_review",
                        "location": {**cand.location, "provenance_dead_end": True},
                    }
                )
            if cand.citation_type not in accepted_types:
                continue
            selected.append(cand)
        return CiteResult(citation_candidates=selected)

    def _source_candidates(
        self,
        session: Session,
        workspace_id: UUID,
        request: CiteRequest,
        actor: ActorContext | None,
    ) -> list[ScoredCandidate]:
        statement = (
            select(LibraryItem)
            .where(
                LibraryItem.workspace_id == workspace_id,
                # M-04: exclude BOTH archived and hidden, matching every other
                # retrieval lane (search/tree/graph/OKF/FTS). Previously only
                # "archived" was excluded, so a hidden item still surfaced as a
                # citation candidate with its title and index_text snippet.
                LibraryItem.lifecycle_status.notin_(
                    EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
                ),
            )
            .order_by(LibraryItem.title, LibraryItem.uri, LibraryItem.id)
            .limit(CITATION_SCAN_LIMIT)
        )
        if not actor_can_read_library_trust_level("restricted", actor):
            statement = statement.where(LibraryItem.trust_level != "restricted")
        if request.source_scope:
            statement = statement.where(LibraryItem.id.in_(request.source_scope))
        sources = session.scalars(statement).all()

        candidates: list[ScoredCandidate] = []
        for source in sources:
            if not actor_can_read_library_trust_level(source.trust_level, actor):
                continue
            version = get_canonical_library_version(session, source)
            if version is None:
                continue
            artifact = get_canonical_markdown_artifact(session, version)
            if artifact is None:
                continue
            text = " ".join(
                part
                for part in [
                    source.title or "",
                    artifact.conversion_detail.index_text or "",
                ]
                if part
            )
            score = score_text(request.claim_text, text)
            if score <= 0:
                continue
            candidates.append(
                ScoredCandidate(
                    score=score,
                    title=source.title or source.uri,
                    candidate=CitationCandidate(
                        citation_type="derived_artifact",
                        library_artifact_id=artifact.id,
                        location=artifact.conversion_detail.location_model,
                        snippet=snippet_for(text, request.claim_text),
                        support_level=support_level(score),
                        freshness=source_freshness(source),
                        recommendation=recommendation(score),
                        write_citation={
                            "citation_type": "derived_artifact",
                            "library_artifact_id": artifact.id,
                            "location": artifact.conversion_detail.location_model,
                            "snippet": snippet_for(text, request.claim_text),
                            "created_via": "cite_tool",
                        },
                    ),
                )
            )
        return candidates

    def _context_page_candidates(
        self,
        session: Session,
        workspace_id: UUID,
        request: CiteRequest,
        actor: ActorContext | None,
    ) -> list[ScoredCandidate]:
        # M-04: a citation must target a STABLE, broadly-readable version. Prefer
        # the published version when one exists, falling back to the actor-visible
        # head only for draft-only pages. The visibility expression (draft-first
        # for editors) is used solely to gate WHICH pages the actor may cite, not
        # to pick the version — otherwise cite pins an editor's citation to an
        # unpublished draft a viewer cannot read (and that dead-ends if discarded).
        visible_head = visible_context_version_expression(actor)
        version_id = func.coalesce(
            ContextPage.current_published_version_id, visible_head
        )
        statement = (
            select(ContextPage, ContextPageVersion)
            # Explicit left side: `version_id` embeds visible_head, an
            # EXISTS-bearing CASE (context_visibility.py) with its own
            # internal aliased ContextPageVersion, which otherwise leaves
            # the ORM unable to infer this join's implicit left.
            .select_from(ContextPage)
            .join(ContextPageVersion, ContextPageVersion.id == version_id)
            .where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.lifecycle_status != "archived",
                # The actor must be able to see SOME version of the page.
                visible_head.is_not(None),
            )
            .order_by(ContextPage.title)
            .limit(CITATION_SCAN_LIMIT)
        )
        if request.target_page_id is not None:
            statement = statement.where(ContextPage.id != request.target_page_id)
        rows = session.execute(statement).tuples().all()

        candidates: list[ScoredCandidate] = []
        for page, version in rows:
            text = " ".join([page.title, version.body_md])
            score = score_text(request.claim_text, text)
            if score <= 0:
                continue
            candidates.append(
                ScoredCandidate(
                    score=score,
                    title=page.title,
                    candidate=CitationCandidate(
                        citation_type="context_page",
                        cited_context_page_version_id=version.id,
                        location={
                            "page_id": str(page.id),
                            "version_no": version.version_no,
                        },
                        snippet=snippet_for(text, request.claim_text),
                        support_level=support_level(score),
                        freshness=context_freshness(page),
                        recommendation=recommendation(score),
                        write_citation={
                            "citation_type": "context_page",
                            "cited_context_page_version_id": version.id,
                            "location": {
                                "page_id": str(page.id),
                                "version_no": version.version_no,
                            },
                            "snippet": snippet_for(text, request.claim_text),
                            "created_via": "cite_tool",
                        },
                    ),
                )
            )
        return candidates


def score_text(claim_text: str, candidate_text: str) -> float:
    claim_terms = set(tokenize(claim_text[:CITATION_SCORE_TEXT_MAX_CHARS]))
    if not claim_terms:
        return 0.0
    candidate_terms = set(tokenize(candidate_text[:CITATION_SCORE_TEXT_MAX_CHARS]))
    if not candidate_terms:
        return 0.0
    return len(claim_terms & candidate_terms) / len(claim_terms)


def tokenize(value: str) -> list[str]:
    tokens = [
        token.strip("_")
        for token in re.split(r"[^\w]+", value.lower())
        if token.strip("_")
    ]
    for match in CJK_SEQUENCE.finditer(value.lower()):
        segment = match.group(0)
        for size in (2, 3):
            if len(segment) < size:
                continue
            tokens.extend(
                segment[index : index + size]
                for index in range(0, len(segment) - size + 1)
            )
    return tokens


def snippet_for(text: str, claim_text: str, length: int = 240) -> str:
    tokens = tokenize(claim_text)
    lower_text = text.lower()
    first_index = min(
        (lower_text.find(token) for token in tokens if lower_text.find(token) >= 0),
        default=0,
    )
    start = max(first_index - 40, 0)
    return text[start : start + length].strip()


def support_level(score: float) -> str:
    if score >= 0.75:
        return "strong"
    if score >= 0.35:
        return "partial"
    return "weak"


def recommendation(score: float) -> str:
    if score >= 0.35:
        return "use"
    return "needs_review"


def source_freshness(source: LibraryItem) -> str:
    if source.lifecycle_status == "archived":
        return "archived"
    return "current"


def context_freshness(page: ContextPage) -> str:
    if page.lifecycle_status == "archived":
        return "archived"
    return "current"
