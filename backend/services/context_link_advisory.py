from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from backend.repositories import context_page_repo

CONTEXT_PAGE_HAS_NO_GRAPH_VISIBLE_LINKS = (
    "context_page_has_no_graph_visible_links"
)


@dataclass(frozen=True, slots=True)
class ContextLinkAdvisoryState:
    source_is_advisory_eligible: bool
    has_other_graph_visible_context: bool
    has_graph_visible_outbound_link: bool

    @property
    def should_warn(self) -> bool:
        return (
            self.source_is_advisory_eligible
            and self.has_other_graph_visible_context
            and not self.has_graph_visible_outbound_link
        )


def context_link_advisory_state(
    session: Session,
    *,
    workspace_id: UUID,
    page_id: UUID,
    version_id: UUID,
) -> ContextLinkAdvisoryState:
    """Describe whether one submitted version needs an authored-link advisory.

    The target predicates deliberately mirror the Context document set used by
    WorkspaceGraphService: a non-archived page whose current published pointer
    resolves to a published-head status. The submitted version may still be a
    draft, but its source page must remain non-archived and the version must
    remain one of that page's live PageLink projections. A late idempotent
    replay therefore cannot warn for a source absent from the graph or mistake
    a pruned historical projection for a link-free submission.
    """
    row = context_page_repo.context_link_advisory_row(
        session,
        workspace_id=workspace_id,
        page_id=page_id,
        version_id=version_id,
    )
    return ContextLinkAdvisoryState(
        source_is_advisory_eligible=row.source_is_advisory_eligible,
        has_other_graph_visible_context=row.has_other_graph_visible_context,
        has_graph_visible_outbound_link=row.has_graph_visible_outbound_link,
    )
