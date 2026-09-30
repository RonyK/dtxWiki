"""Concept resolution over the Markdown-native page-name projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion
from backend.policy.auth import ActorContext
from backend.services.entity_ref import normalize_entity_name
from backend.services.okf import context_concept_path
from backend.services.page_name_lookup import (
    VisiblePageNameMatch,
    best_match_per_page,
    visible_page_name_matches,
)


@dataclass(frozen=True)
class ConceptCandidate:
    page_id: UUID
    version_id: UUID
    title: str
    path: str
    match_kind: str


@dataclass(frozen=True)
class ConceptReadResult:
    status: str
    page: ContextPage | None = None
    version: ContextPageVersion | None = None
    canonical_path: str | None = None
    redirect_target_path: str | None = None
    candidates: list[ConceptCandidate] = field(default_factory=list)

    @property
    def page_id(self) -> UUID | None:
        return self.page.id if self.page is not None else None

    @property
    def version_id(self) -> UUID | None:
        return self.version.id if self.version is not None else None


def resolve_context_concept(
    session: Session,
    workspace_id: UUID,
    *,
    identifier: str,
    actor: ActorContext | None,
) -> ConceptReadResult:
    norm = normalize_entity_name(identifier.removesuffix(".md").strip("/"))
    if not norm:
        return ConceptReadResult(status="not_found")
    # page_name_index is write-synced by every mutation seam and backfilled by
    # 0002_epic1 (and by reindex), so an index miss IS not_found — no O(N)
    # page scan.
    matches = visible_page_name_matches(
        session,
        workspace_id,
        name_norm=norm,
        actor=actor,
    )
    if not matches:
        return ConceptReadResult(status="not_found")
    pages = {match.page.id for match in matches}
    if len(pages) > 1:
        # One candidate per page, keeping the shared slug > title > alias
        # priority. Search and read_concept therefore cannot drift.
        return ConceptReadResult(
            status="ambiguous",
            candidates=[
                ConceptCandidate(
                    page_id=match.page.id,
                    version_id=match.version.id,
                    title=match.page.title,
                    path=context_concept_path(match.page.slug),
                    match_kind=match.index.name_kind,
                )
                for match in best_match_per_page(matches)
            ],
        )
    match = _best_match(matches)
    index, page, version = match.index, match.page, match.version
    canonical_path = context_concept_path(page.slug)
    if index.name_kind == "slug" and normalize_entity_name(page.slug) == norm:
        return ConceptReadResult(
            status="found",
            page=page,
            version=version,
            canonical_path=canonical_path,
        )
    return ConceptReadResult(
        status="redirected",
        page=page,
        version=version,
        canonical_path=canonical_path,
        redirect_target_path=canonical_path,
    )
def _best_match(
    matches: list[VisiblePageNameMatch],
) -> VisiblePageNameMatch:
    return best_match_per_page(matches)[0]
