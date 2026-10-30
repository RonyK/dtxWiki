"""H2 regression: Ask must cite the right ids regardless of retrieval mode.

Semantic/hybrid hits carry the *chunk* id in ``RankedResult.id`` and the stable
document id in ``source_ref`` (kind="content_chunk"), and use type
"library_item" (not "library_artifact"). Before the fix Ask stored chunk ids as
context-page ids and never matched the library type, so citations were wrong or
empty under the semantic/hybrid tier that Ask now prefers.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from backend.models.ask import AskTurn, AskTurnCitation
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.coverage import KnowledgeCoverageGap
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryAttempt,
    LibraryAttemptConversionDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
    LibraryRunConversionDetail,
)
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.ask import AskSessionCreate, AskTurnCreate
from backend.schemas.read import RankedResult, SearchPayload
from backend.services.ask_service import AskService
from backend.services.graph_service import GraphService

pytestmark = pytest.mark.integration

ACTOR_ID = "00000000-0000-0000-0000-000000000010"


def _actor(role: str) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=ACTOR_ID,
        token_kind="session",
        workspace_role=role,
        role=role,
    )


class FakeSemanticSearch:
    """Returns semantic-shaped results: chunk id in ``id``, document id in
    ``source_ref``. ``context`` results answer the context_page scope; ``library``
    results answer the library_artifact fallback scope."""

    def __init__(self, context=None, library=None, warnings=None):
        self._context = context or []
        self._library = library or []
        self._warnings = warnings or []
        self.actors = []

    def supported_modes(self):
        return {"keyword", "semantic", "hybrid"}

    def search(self, **kwargs):
        self.actors.append(kwargs.get("actor"))
        scope = kwargs["scope"]
        results = (
            self._context
            if scope == "context_page"
            else (self._library if scope == "library_artifact" else [])
        )
        return SearchPayload(
            ranked_results=results,
            supported_retrieval_modes=["keyword", "semantic", "hybrid"],
            permission_note=None,
            next_cursor=None,
            warnings=self._warnings,
        )


def _ws(session) -> Workspace:
    return session.scalar(select(Workspace).where(Workspace.slug == "default"))


def _seed_page(session, workspace_id) -> ContextPage:
    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug="dep-plan",
        title="Deployment Plan",
        aliases=[],
        tags=[],
    )
    session.add(page)
    session.flush()
    return page


def _seed_library(session, workspace_id):
    item = LibraryItem(
        workspace_id=workspace_id,
        connector_type="upload",
        uri="upload://doc.md",
        title="Doc",
    )
    session.add(item)
    session.flush()
    version = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash="h1",
        raw_storage_ref="inline://raw",
        processing_status="completed",
    )
    session.add(version)
    session.flush()
    item.current_version_id = version.id
    item.current_ready_version_id = version.id
    # session_factory (backend/tests/conftest.py) uses a bare create_engine()
    # with no PRAGMA foreign_keys=ON, so run_id/attempt_id/blob_id need not
    # reference other real rows -- only the NOT NULL/CHECK-satisfying columns
    # and the selected_conversion_attempt_id link (required by
    # canonical_library_artifact_belongs_to_item now that A-1 retired the
    # nullable-attempt "belongs by default" shape) matter for this test.
    run = LibraryRun(
        workspace_id=workspace_id,
        library_item_version_id=version.id,
        stage="conversion",
        run_seq=1,
        reason="initial",
        status="completed",
        provider_chain_json=[],
        policy_revision="test",
        output_schema_version="1",
        idempotency_key=f"test:{version.id}",
    )
    run.conversion_detail = LibraryRunConversionDetail(
        error_fallback_matrix_json={},
        per_provider_budget_json={},
        total_budget_json={},
    )
    session.add(run)
    session.flush()
    attempt = LibraryAttempt(
        workspace_id=workspace_id,
        run_id=run.id,
        library_item_version_id=version.id,
        stage="conversion",
        provider_id="test.provider",
        provider_version="1",
        provider_spi_version="1",
        config_hash="test",
        input_sha256="0" * 64,
        output_schema_version="1",
        status="completed",
        chain_index=0,
    )
    attempt.conversion_detail = LibraryAttemptConversionDetail(
        manual_retry_disposition=None,
        disposition_reason=None,
    )
    session.add(attempt)
    session.flush()
    artifact = LibraryArtifact(
        workspace_id=workspace_id,
        library_item_version_id=version.id,
        attempt_id=attempt.id,
        stage="conversion",
        blob_workspace_id=workspace_id,
        blob_id=uuid4(),
        blob_epoch=1,
        role="markdown",
        kind="text",
        media_type="text/plain",
        content_sha256="a" * 64,
        byte_size=len("Body"),
    )
    artifact.conversion_detail = LibraryArtifactConversionDetail(
        is_complete=True,
        index_text="Body",
    )
    session.add(artifact)
    session.flush()
    version.selected_conversion_attempt_id = attempt.id
    session.add(version)
    session.flush()
    return item, artifact


def _semantic_context_result(page_id: UUID) -> RankedResult:
    return RankedResult(
        id=uuid4(),  # chunk id — deliberately NOT the page id
        type="context_page",
        title="Deployment Plan",
        snippet="Rollout guidance",
        score=1.0,
        citation_available=True,
        source_ref={"kind": "content_chunk", "document_id": str(page_id)},
    )


def _semantic_library_result(item_id: UUID) -> RankedResult:
    return RankedResult(
        id=uuid4(),  # chunk id
        type="library_item",  # semantic emits library_item, not library_artifact
        title="Doc",
        snippet="Body",
        score=1.0,
        citation_available=True,
        source_ref={"kind": "content_chunk", "document_id": str(item_id)},
    )


def _create_turn(
    service,
    session,
    workspace_id,
    message,
    *,
    expected_warnings=None,
    actor=None,
):
    ask_session = service.create_session(
        session=session,
        workspace_id=workspace_id,
        request=AskSessionCreate(),
        actor_kind="human",
        actor_id=ACTOR_ID,
    )
    detail, warnings = service.create_turn(
        session=session,
        workspace_id=workspace_id,
        ask_session_id=ask_session.id,
        request=AskTurnCreate(user_message=message),
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor=actor,
    )
    assert warnings == (expected_warnings or [])
    turn = session.scalar(
        select(AskTurn).where(AskTurn.ask_session_id == ask_session.id)
    )
    return turn, detail


def test_proposal_evidence_visible_rejects_cross_workspace_turn(session_factory):
    # Codex final: a proposal and its source turn have independent workspace
    # ancestry. A malformed workspace-A proposal referencing a workspace-B turn
    # (whose citations may be empty/visible) must fail closed, not launder B's
    # cloned answer to an A-only actor.
    from uuid import uuid4

    from backend.services.context_ingress_service import _proposal_evidence_visible

    with session_factory.begin() as session:
        workspace = _ws(session)
        service = AskService(search_service=FakeSemanticSearch())
        turn, _ = _create_turn(
            service,
            session,
            workspace.id,
            "cross-workspace check",
            expected_warnings=["coverage_gap_recorded"],
        )
        owner = _actor("owner")

        # Correct proposal workspace: the (uncited) turn is visible.
        assert _proposal_evidence_visible(session, turn.id, owner, workspace.id) is True
        # A foreign proposal workspace must fail closed on the mismatch.
        assert _proposal_evidence_visible(session, turn.id, owner, uuid4()) is False


def test_ask_passes_actor_to_every_search_lane(session_factory):
    search = FakeSemanticSearch()
    actor = _actor("viewer")
    with session_factory.begin() as session:
        workspace = _ws(session)
        service = AskService(search_service=search)
        _create_turn(
            service,
            session,
            workspace.id,
            "actor visibility check",
            expected_warnings=["coverage_gap_recorded"],
            actor=actor,
        )

    assert search.actors
    assert all(observed is actor for observed in search.actors)


def test_ask_cites_page_id_for_semantic_chunk_result(session_factory):
    with session_factory.begin() as session:
        workspace = _ws(session)
        page = _seed_page(session, workspace.id)
        service = AskService(
            search_service=FakeSemanticSearch(
                context=[_semantic_context_result(page.id)]
            )
        )
        turn, detail = _create_turn(
            service, session, workspace.id, "how do we roll out?"
        )
        cited = session.scalars(
            select(AskTurnCitation.target_id)
            .where(AskTurnCitation.ask_turn_id == turn.id)
            .where(AskTurnCitation.target_kind == "context_page")
            .order_by(AskTurnCitation.rank)
        ).all()
    assert cited == [page.id]
    assert detail.retrieval_trace[0]["id"] == str(page.id)


def test_ask_session_read_redacts_evidence_for_actor_without_visibility(
    session_factory,
):
    # H-01: an answer grounded in a draft Context page (editor-visible only) must
    # be redacted when a viewer reads the session back, so the stored answer /
    # citations / trace cannot replay evidence the current actor cannot read.
    with session_factory.begin() as session:
        workspace = _ws(session)
        draft = ContextPage(
            workspace_id=workspace.id,
            slug="ask-h01-draft",
            title="H01 Draft Source",
            page_type="concept",
            lifecycle_status="draft",
        )
        session.add(draft)
        session.flush()
        version = ContextPageVersion(
            workspace_id=draft.workspace_id,
            page_id=draft.id,
            version_no=1,
            body_md="# H01 Draft Source\n\nDraft evidence body.",
            content_hash="ask-h01-draft-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
            version_status="draft",
        )
        session.add(version)
        session.flush()
        draft.current_version_id = version.id
        draft.latest_draft_version_id = version.id
        draft_id = draft.id

        service = AskService(
            search_service=FakeSemanticSearch(
                context=[_semantic_context_result(draft_id)]
            )
        )
        ask_session = service.create_session(
            session=session,
            workspace_id=workspace.id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_actor("editor"),
        )
        service.create_turn(
            session=session,
            workspace_id=workspace.id,
            ask_session_id=ask_session.id,
            request=AskTurnCreate(user_message="what is the plan?"),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_actor("editor"),
        )
        editor_detail = service.get_session(
            session=session,
            workspace_id=workspace.id,
            ask_session_id=ask_session.id,
            actor=_actor("editor"),
        )
        viewer_detail = service.get_session(
            session=session,
            workspace_id=workspace.id,
            ask_session_id=ask_session.id,
            actor=_actor("viewer"),
        )

    editor_turn = editor_detail.turns[0]
    viewer_turn = viewer_detail.turns[0]
    # The editor who can read the draft evidence sees the full answer.
    assert editor_turn.cited_context_page_ids == [draft_id]
    assert editor_turn.retrieval_trace
    # The viewer cannot read the draft, so the whole evidence-derived payload is
    # redacted.
    assert viewer_turn.cited_context_page_ids == []
    assert viewer_turn.cited_library_artifact_ids == []
    assert viewer_turn.retrieval_trace == []
    assert viewer_turn.assistant_message != editor_turn.assistant_message
    assert "access" in viewer_turn.assistant_message.lower()


def test_ask_turn_evidence_visible_fails_closed_on_dangling(session_factory):
    # H-01 (Codex follow-up): a dangling citation (cited page/artifact gone)
    # must fail CLOSED — the stored answer was built from it and we cannot
    # confirm the reader was ever allowed to see it.
    from backend.services.ask_service import ask_turn_evidence_visible

    with session_factory.begin() as session:
        workspace = _ws(session)
        owner = _actor("owner")
        wsid = workspace.id
        # A citation to a non-existent page id.
        assert ask_turn_evidence_visible(session, [uuid4()], [], owner, wsid) is False
        # A citation to a non-existent artifact id.
        assert ask_turn_evidence_visible(session, [], [uuid4()], owner, wsid) is False
        # No citations at all: nothing to leak.
        assert ask_turn_evidence_visible(session, [], [], owner, wsid) is True
        # Belt-and-suspenders: workspace exists so the fixture is valid.
        assert workspace is not None


def test_ask_turn_evidence_keeps_ready_a_until_ready_b_supersedes_it(
    session_factory,
):
    # Codex round-11: the stored answer paraphrases the cited library evidence,
    # so ask_turn_evidence_visible must fail closed when the source is later
    # archived/hidden or superseded by a newer version — trust alone under-gated
    # lifecycle and currency.
    from backend.services.ask_service import ask_turn_evidence_visible

    with session_factory.begin() as session:
        workspace = _ws(session)
        owner = _actor("owner")
        wsid = workspace.id
        item, artifact = _seed_library(session, wsid)

        # Live source, cited version current: visible.
        assert (
            ask_turn_evidence_visible(session, [], [artifact.id], owner, wsid) is True
        )

        # Archived source: fail closed on lifecycle.
        item.lifecycle_status = "archived"
        session.flush()
        assert (
            ask_turn_evidence_visible(session, [], [artifact.id], owner, wsid) is False
        )

        # Un-archived with pending latest B: ready A remains canonical.
        item.lifecycle_status = "registered"
        v2 = LibraryItemVersion(
            workspace_id=item.workspace_id,
            library_item_id=item.id,
            version_no=2,
            content_hash="h2",
            raw_storage_ref="inline://raw2",
        )
        session.add(v2)
        session.flush()
        item.current_version_id = v2.id
        session.flush()
        assert (
            ask_turn_evidence_visible(session, [], [artifact.id], owner, wsid) is True
        )

        # Once B becomes the ready projection, evidence from A is superseded.
        v2.processing_status = "completed"
        item.current_ready_version_id = v2.id
        session.flush()
        assert (
            ask_turn_evidence_visible(session, [], [artifact.id], owner, wsid) is False
        )


def test_ask_forwards_permission_limited_but_not_search_only_warnings(session_factory):
    # permission_limited is a trust disclosure about the evidence the answer was
    # built on, so it forwards; unsupported search filters do not apply to an
    # Ask turn and remain on the search response only.
    with session_factory.begin() as session:
        workspace = _ws(session)
        page = _seed_page(session, workspace.id)
        service = AskService(
            search_service=FakeSemanticSearch(
                context=[_semantic_context_result(page.id)],
                warnings=["permission_limited", "unsupported_filter_tag"],
            )
        )
        turn, detail = _create_turn(
            service,
            session,
            workspace.id,
            "which deployment plan?",
            expected_warnings=["permission_limited"],
        )

    assert turn.warning_codes == ["permission_limited"]
    assert detail.warning_codes == ["permission_limited"]


def test_ask_resolves_library_item_result_to_artifact_id(session_factory):
    with session_factory.begin() as session:
        workspace = _ws(session)
        item, artifact = _seed_library(session, workspace.id)
        service = AskService(
            search_service=FakeSemanticSearch(
                library=[_semantic_library_result(item.id)]
            )
        )
        turn, detail = _create_turn(
            service, session, workspace.id, "what does the doc say?"
        )
        cited_artifacts = session.scalars(
            select(AskTurnCitation.target_id)
            .where(AskTurnCitation.ask_turn_id == turn.id)
            .where(AskTurnCitation.target_kind == "library_artifact")
            .order_by(AskTurnCitation.rank)
        ).all()
    assert cited_artifacts == [artifact.id]
    assert detail.retrieval_trace[0]["id"] == str(artifact.id)
    assert detail.fallback_disclosure is True


def test_library_hit_without_artifact_is_evidence_not_coverage_gap(session_factory):
    with session_factory.begin() as session:
        workspace = _ws(session)
        item = LibraryItem(
            workspace_id=workspace.id,
            connector_type="upload",
            uri="upload://pending.md",
            title="Pending extraction",
        )
        session.add(item)
        session.flush()
        service = AskService(
            search_service=FakeSemanticSearch(
                library=[_semantic_library_result(item.id)]
            )
        )
        turn, detail = _create_turn(
            service,
            session,
            workspace.id,
            "what is pending extraction?",
            expected_warnings=["evidence_not_citable"],
        )
        citations = session.scalars(
            select(AskTurnCitation).where(AskTurnCitation.ask_turn_id == turn.id)
        ).all()

        assert citations == []
        assert detail.retrieval_trace[0]["id"] == str(item.id)
        assert detail.placement_confidence == "medium"
        assert session.scalar(select(func.count(KnowledgeCoverageGap.id))) == 0
        assert session.scalar(select(func.count(ClientDelegatedTask.id))) == 0


def test_ask_graph_context_is_bounded_and_actor_visible(
    session_factory,
    page_service,
):
    linked = page_service.write_page(
        workspace_slug="default",
        slug="ask-graph-linked",
        title="Ask Graph Linked",
        body_md="# Ask Graph Linked\n\nPublished.",
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace = _ws(session)
        hidden = ContextPage(
            workspace_id=workspace.id,
            slug="ask-graph-hidden",
            title="Ask Graph Hidden",
            page_type="concept",
            lifecycle_status="draft",
        )
        session.add(hidden)
        session.flush()
        hidden_version = ContextPageVersion(
            workspace_id=hidden.workspace_id,
            page_id=hidden.id,
            version_no=1,
            body_md="# Ask Graph Hidden\n\nDraft.",
            content_hash="ask-graph-hidden-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
            version_status="draft",
        )
        session.add(hidden_version)
        session.flush()
        hidden.current_version_id = hidden_version.id
        hidden.latest_draft_version_id = hidden_version.id
        hidden_id = hidden.id

    seed = page_service.write_page(
        workspace_slug="default",
        slug="ask-graph-seed",
        title="Ask Graph Seed",
        body_md=(
            "# Ask Graph Seed\n\nSee [[ask-graph-linked]] and [[ask-graph-hidden]]."
        ),
        actor_kind="human",
        actor_id=ACTOR_ID,
        actor_provenance="browser_session",
    )

    with session_factory.begin() as session:
        workspace = _ws(session)
        service = AskService(
            search_service=FakeSemanticSearch(
                context=[_semantic_context_result(seed.id)]
            ),
            graph_service=GraphService(),
        )
        ask_session = service.create_session(
            session=session,
            workspace_id=workspace.id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_actor("editor"),
        )
        editor_turn, warnings = service.create_turn(
            session=session,
            workspace_id=workspace.id,
            ask_session_id=ask_session.id,
            request=AskTurnCreate(user_message="show graph evidence"),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor=_actor("editor"),
        )
        viewer_session = service.get_session(
            session=session,
            workspace_id=workspace.id,
            ask_session_id=ask_session.id,
            actor=_actor("viewer"),
        )

    assert warnings == []
    assert len(editor_turn.graph_context) <= 5
    assert {item.id for item in editor_turn.graph_context} == {
        linked.id,
        hidden_id,
    }
    assert {item.id for item in viewer_session.turns[0].graph_context} == {linked.id}


def test_ask_graph_context_is_empty_without_context_citation(session_factory):
    with session_factory.begin() as session:
        workspace = _ws(session)
        service = AskService(
            search_service=FakeSemanticSearch(),
            graph_service=GraphService(),
        )
        ask_session = service.create_session(
            session=session,
            workspace_id=workspace.id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        detail, warnings = service.create_turn(
            session=session,
            workspace_id=workspace.id,
            ask_session_id=ask_session.id,
            request=AskTurnCreate(user_message="no matching evidence"),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
    assert warnings == ["coverage_gap_recorded"]
    assert detail.graph_context == []


def test_deterministic_no_evidence_message_uses_workspace_language(session_factory):
    with session_factory.begin() as session:
        workspace = _ws(session)
        workspace.default_language = "ko"
        service = AskService(search_service=FakeSemanticSearch())
        ask_session = service.create_session(
            session=session,
            workspace_id=workspace.id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        detail, warnings = service.create_turn(
            session=session,
            workspace_id=workspace.id,
            ask_session_id=ask_session.id,
            request=AskTurnCreate(user_message="근거 없는 질문"),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )

    assert warnings == ["coverage_gap_recorded"]
    assert detail.assistant_message == (
        "일치하는 Context 또는 Library 근거를 찾지 못했습니다."
    )


def test_session_detail_batches_graph_context_for_all_turns(session_factory):
    class BatchGraphSpy:
        def __init__(self):
            self.calls = 0
            self.page_ids = []

        def context_pages_graph_context(self, _session, _workspace_id, **kwargs):
            self.calls += 1
            self.page_ids = kwargs["page_ids"]
            return {page_id: [] for page_id in self.page_ids}

        def context_page_graph_context(self, *_args, **_kwargs):
            raise AssertionError("session GET must use the batch graph API")

    with session_factory.begin() as session:
        workspace = _ws(session)
        seed = _seed_page(session, workspace.id)
        version = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=seed.id,
            version_no=1,
            body_md="# Deployment Plan\n\nVisible source.",
            content_hash="ask-link-target-v1",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        seed.current_version_id = version.id
        seed.current_published_version_id = version.id
        # Budget exception: this existing DB fixture needs real published-page
        # provenance to prove Ask exposes only a confirmed same-workspace link
        # and omits an external target. Pure projection tests cannot verify the
        # service's publication and source-page checks; both links share this
        # one existing fixture and add no separate DB scenario.
        graph = BatchGraphSpy()
        service = AskService(
            search_service=FakeSemanticSearch(),
            graph_service=graph,
        )
        ask_session = service.create_session(
            session=session,
            workspace_id=workspace.id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=ACTOR_ID,
        )
        for turn_no in range(1, 51):
            turn = AskTurn(
                workspace_id=workspace.id,
                ask_session_id=ask_session.id,
                turn_no=turn_no,
                user_message=f"Question {turn_no}",
                assistant_message=(
                    f"Answer {turn_no}"
                    if turn_no != 1
                    else (
                        f"[Grounding](/context?target={seed.id}) "
                        "[outside](https://outside.example/secret)"
                    )
                ),
                placement_confidence="medium",
            )
            session.add(turn)
            session.flush()
            session.add(
                AskTurnCitation(
                    workspace_id=turn.workspace_id,
                    ask_turn_id=turn.id,
                    target_kind="context_page",
                    target_id=seed.id,
                    rank=0,
                )
            )
        detail = service.get_session(
            session,
            workspace.id,
            ask_session.id,
            actor=_actor("viewer"),
        )

    assert len(detail.turns) == 50
    assert graph.calls == 1
    assert graph.page_ids == [seed.id]
    assert len(detail.turns[0].link_targets) == 1
    link_target = detail.turns[0].link_targets[0]
    assert link_target.locator == f"/context?target={seed.id}"
    assert link_target.href == f"/p/{workspace.id}/{seed.id}"
    assert link_target.target_page_id == seed.id
    assert link_target.state == "ok"
