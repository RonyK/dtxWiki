"""Codex #15: a context-page citation to an ungrounded page warns (dead-end)."""

from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.context_write import CitationInput, CiteRequest
from backend.services.citation_service import CitationService
from backend.services.context_write_service import ContextWriteService

CITATION_ACTOR = ActorContext(
    actor_kind="system",
    actor_id="citation-dead-end-test",
    scopes=("*",),
    workspace_role="owner",
    role="owner",
    token_kind="system",
)


def _ungrounded_version(session, workspace_id) -> ContextPageVersion:
    page = ContextPage(
        workspace_id=workspace_id,
        slug="ungrounded",
        title="Ungrounded",
        page_type="overview",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md="x",
        content_hash="h-ungrounded",
        actor_type="user",
        created_by_actor_kind="user",
        created_by_actor_id="tester",
    )
    session.add(version)
    session.flush()
    return version


def test_ungrounded_context_page_has_no_raw_lineage(page_service):
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        version = _ungrounded_version(session, workspace.id)
        assert ContextWriteService._has_raw_lineage(session, version.id, set()) is False


def test_dead_end_warning_for_ungrounded_citation(page_service):
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        version = _ungrounded_version(session, workspace.id)
        citation = CitationInput(
            citation_type="context_page",
            cited_context_page_version_id=version.id,
        )
        assert ContextWriteService._dead_end_warnings(session, [citation]) == [
            "provenance_dead_end"
        ]
        # No context_page citations -> no dead-end warning.
        assert ContextWriteService._dead_end_warnings(session, []) == []


def test_recommender_never_recommends_ungrounded_context_page(page_service):
    # Codex #15: the recommender must not surface a provenance dead-end as 'use',
    # even on a perfect text match — otherwise ungrounded context-to-context
    # chains get built.
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        version = _ungrounded_version(session, workspace.id)
        # Make the page resolvable by the candidate scan (coalesce of the version
        # pointers) so it actually appears as a recommender candidate.
        page = session.get(ContextPage, version.page_id)
        page.latest_draft_version_id = version.id
        session.flush()
        result = CitationService().cite(
            session,
            workspace.id,
            CiteRequest(
                claim_text="Ungrounded",
                desired_citation_type="context_page",
                limit=10,
            ),
            actor=CITATION_ACTOR,
        )
    matches = [
        candidate
        for candidate in result.citation_candidates
        if candidate.citation_type == "context_page"
        and candidate.cited_context_page_version_id == version.id
    ]
    assert matches, "the ungrounded page should still be returned, just flagged"
    candidate = matches[0]
    assert candidate.recommendation == "needs_review"
    assert candidate.location.get("provenance_dead_end") is True
