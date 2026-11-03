"""Document identities shared by reference writers, publishers and readers."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

DocumentKind = Literal["context_page", "library_item", "agent_page"]


@dataclass(frozen=True)
class DocumentRef:
    target_kind: DocumentKind
    target_id: UUID

    @property
    def key(self) -> tuple[str, UUID]:
        return self.target_kind, self.target_id


@dataclass(frozen=True)
class CitationOccurrence:
    href: str
    start: int
    end: int


@dataclass(frozen=True)
class ResolvedDocumentTarget:
    reference: DocumentRef
    title: str | None
    href: str | None
    state: Literal["available", "unavailable", "unpublished", "deleted"]
    location: str | None = None
    graph_eligible: bool = False
