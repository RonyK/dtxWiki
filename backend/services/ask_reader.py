from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.ask import (
    ASK_TURN_COMPLETED,
    ASK_TURN_FAILED,
    ASK_TURN_PENDING,
    AskSession,
    AskTurn,
    ContextIngressProposal,
)
from backend.models.tree import WorkspaceTreeNode
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.ask import (
    AskSessionDetail,
    AskSessionRef,
    AskTurnDetail,
)
from backend.schemas.read import GraphNeighborEntry
from backend.services.ask_composer import AskComposer
from backend.services.ask_retrieval import AskRetriever
from backend.services.ask_types import (
    ASK_FORWARDABLE_WARNINGS,
    ASK_MODEL_ERROR_CODES,
    ASK_SELF_WARNINGS,
    _workspace_answer_language,
)
from backend.services.ask_visibility import (
    _turn_citations_by_turn,
    ask_turn_visible,
)
from backend.services.context_link_targets import ask_context_link_targets
from backend.services.graph_service import GraphService
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError
from backend.services.tree_read import TreeReadService


class AskTurnReader:
    def __init__(
        self,
        tree_read_service: TreeReadService,
        graph_service: GraphService | None,
        retriever: AskRetriever,
        composer: AskComposer,
    ) -> None:
        self.tree_read_service = tree_read_service
        self.graph_service = graph_service
        self._retriever = retriever
        self._composer = composer

    def get_session(
        self,
        session: Session,
        workspace_id: UUID,
        ask_session_id: UUID,
        actor: ActorContext | None = None,
    ) -> AskSessionDetail:
        return self.session_detail(
            session=session,
            ask_session=self._require_session(session, workspace_id, ask_session_id),
            actor=actor,
        )

    def get_turn(
        self,
        session: Session,
        workspace_id: UUID,
        turn_id: UUID,
        actor: ActorContext | None = None,
    ) -> AskTurnDetail:
        turn = get_in_workspace(
            session, AskTurn, workspace_id=workspace_id, resource_id=turn_id
        )
        if turn is None:
            raise ReadNotFoundError("ask turn not found")
        ask_session = self._require_session(session, workspace_id, turn.ask_session_id)
        return self._turn_detail(session, ask_session, turn, actor=actor)

    def search_failed_turn(
        self,
        session: Session,
        workspace_id: UUID,
        turn_id: UUID,
        actor: ActorContext | None = None,
    ) -> AskTurnDetail:
        """Return a keyword-search preview without changing the failed turn."""
        detail = self.get_turn(session, workspace_id, turn_id, actor)
        if not detail.can_search:
            raise RejectedByPolicyError(
                "This Ask turn has no model failure to search again.",
                error_code="ask_turn_not_completed",
            )
        prepared = self._retriever.prepare_turn(
            session,
            workspace_id,
            turn_id,
            actor,
            keyword_only=True,
        )
        retrieved = prepared.retrieved
        composed = self._composer.compose_evidence(prepared)
        warnings = [
            code
            for code in composed.warnings
            if code in ASK_FORWARDABLE_WARNINGS or code in ASK_SELF_WARNINGS
        ]
        if retrieved.found and not retrieved.pack:
            warnings.append("evidence_not_citable")
        return detail.model_copy(
            update={
                "status": ASK_TURN_COMPLETED,
                "assistant_message": composed.answer,
                "cited_context_page_ids": list(retrieved.context_ids),
                "cited_library_artifact_ids": list(retrieved.artifact_ids),
                "retrieval_trace": list(retrieved.trace),
                "tried_lanes": list(retrieved.tried_lanes),
                "fallback_disclosure": retrieved.fallback,
                "warning_codes": warnings,
                "error_code": None,
                "can_search": False,
            }
        )

    @staticmethod
    def _require_session(
        session: Session,
        workspace_id: UUID,
        ask_session_id: UUID,
    ) -> AskSession:
        ask_session = get_in_workspace(
            session,
            AskSession,
            workspace_id=workspace_id,
            resource_id=ask_session_id,
        )
        if ask_session is None:
            raise ReadNotFoundError("ask session not found")
        return ask_session

    def session_ref(
        self,
        session: Session,
        ask_session: AskSession,
    ) -> AskSessionRef:
        return AskSessionRef(
            id=ask_session.id,
            title=ask_session.title,
            started_at=ask_session.started_at,
            updated_at=ask_session.updated_at,
            actor_kind=ask_session.actor_kind,
            title_user_edited=ask_session.title_user_edited,
            topic_tags=ask_session.default_topic_tags,
            turn_count=turn_count(session, ask_session.id),
        )

    def session_detail(
        self,
        session: Session,
        ask_session: AskSession,
        actor: ActorContext | None = None,
    ) -> AskSessionDetail:
        turns = session.scalars(
            select(AskTurn)
            .where(AskTurn.ask_session_id == ask_session.id)
            .order_by(AskTurn.turn_no)
        ).all()
        node = get_in_workspace(
            session,
            WorkspaceTreeNode,
            workspace_id=ask_session.workspace_id,
            resource_id=ask_session.placement_tree_node_id,
        )
        tree_path = (
            self.tree_read_service.path_for_node(session=session, node=node)
            if node is not None
            else []
        )
        # One citation/proposal query for the whole session (no per-turn N+1).
        citations_by_turn = _turn_citations_by_turn(
            session, [turn.id for turn in turns]
        )
        proposals_by_turn = _turn_proposal_ids_by_turn(
            session, [turn.id for turn in turns]
        )
        graph_context_by_page = self._session_graph_contexts(
            session,
            ask_session.workspace_id,
            citations_by_turn,
            actor,
        )
        # Follow-ups repeat their history's sources; check each once.
        visibility_cache: dict[tuple[str, UUID, UUID | None], bool] = {}
        return AskSessionDetail(
            **self.session_ref(session, ask_session).model_dump(),
            workspace_id=ask_session.workspace_id,
            ended_at=ask_session.ended_at,
            actor_id=ask_session.actor_id,
            default_context_parent_tree_node_id=(
                ask_session.default_context_parent_tree_node_id
            ),
            placement_tree_node_id=ask_session.placement_tree_node_id,
            tree_path=tree_path,
            turns=[
                self._turn_detail(
                    session=session,
                    ask_session=ask_session,
                    turn=turn,
                    citations=citations_by_turn.get(turn.id, ([], [])),
                    proposal_ids=proposals_by_turn.get(turn.id, []),
                    actor=actor,
                    graph_context=(
                        graph_context_by_page.get(
                            citations_by_turn.get(turn.id, ([], []))[0][0], []
                        )
                        if citations_by_turn.get(turn.id, ([], []))[0]
                        else []
                    ),
                    visibility_cache=visibility_cache,
                )
                for turn in turns
            ],
        )

    def _turn_detail(
        self,
        session: Session,
        ask_session: AskSession,
        turn: AskTurn,
        citations: tuple[list[UUID], list[UUID]] | None = None,
        proposal_ids: list[str] | None = None,
        actor: ActorContext | None = None,
        graph_context: list[GraphNeighborEntry] | None = None,
        visibility_cache: dict[tuple[str, UUID, UUID | None], bool] | None = None,
    ) -> AskTurnDetail:
        node = get_in_workspace(
            session,
            WorkspaceTreeNode,
            workspace_id=ask_session.workspace_id,
            resource_id=ask_session.placement_tree_node_id,
        )
        tree_path = (
            self.tree_read_service.path_for_node(session=session, node=node)
            if node is not None
            else []
        )
        if citations is None:
            citations = _turn_citations_by_turn(session, [turn.id]).get(
                turn.id, ([], [])
            )
        if proposal_ids is None:
            proposal_ids = _turn_proposal_ids_by_turn(session, [turn.id]).get(
                turn.id, []
            )
        cited_page_ids, cited_artifact_ids = citations
        # H-01: the answer, citations, and retrieval trace were composed with the
        # AUTHOR's search permissions (which may include restricted Library or
        # draft Context evidence). On a READ, re-validate the grounding evidence
        # against the CURRENT actor; if any cited document is no longer visible to
        # them, the answer is derived from evidence they cannot read, so redact the
        # whole evidence-derived payload rather than replay it.
        evidence_visible = ask_turn_visible(
            session,
            turn,
            citations,
            actor,
            ask_session.workspace_id,
            cache=visibility_cache,
        )
        if evidence_visible:
            assistant_message = turn.assistant_message
            retrieval_trace = turn.retrieval_trace or []
            visible_page_ids = cited_page_ids
            visible_artifact_ids = cited_artifact_ids
            turn_graph_context = (
                graph_context
                if graph_context is not None
                else self._turn_graph_context(
                    session,
                    ask_session.workspace_id,
                    cited_page_ids,
                    actor,
                )
            )
        else:
            assistant_message = _restricted_answer_placeholder(
                _workspace_answer_language(session, ask_session.workspace_id)
            )
            retrieval_trace = []
            visible_page_ids = []
            visible_artifact_ids = []
            turn_graph_context = []
        link_targets = (
            ask_context_link_targets(
                session,
                body=assistant_message or "",
                workspace_id=ask_session.workspace_id,
                actor=actor,
            )
            if evidence_visible and assistant_message
            else []
        )
        return AskTurnDetail(
            id=turn.id,
            ask_session_id=turn.ask_session_id,
            turn_no=turn.turn_no,
            user_message=turn.user_message,
            assistant_message=assistant_message,
            used_agent_input_bundle_id=turn.used_agent_input_bundle_id,
            cited_context_page_ids=visible_page_ids,
            cited_library_artifact_ids=visible_artifact_ids,
            topic_tags=turn.topic_tags,
            promoted_context_page_id=turn.promoted_context_page_id,
            context_ingress_proposal_ids=proposal_ids,
            recommended_placement_tree_node_id=(
                turn.recommended_placement_tree_node_id
            ),
            placement_confidence=turn.placement_confidence,
            promotion_state=turn.promotion_state,
            status=(
                "queued"
                if turn.status == ASK_TURN_PENDING and turn.started_at is None
                else turn.status
            ),
            error_code=turn.error_code,
            selection=turn.selection,
            steering_messages=turn.steering_messages or [],
            can_search=(
                turn.status == ASK_TURN_FAILED
                and turn.error_code in ASK_MODEL_ERROR_CODES
            ),
            created_at=turn.created_at,
            started_at=turn.started_at,
            tree_path=tree_path,
            retrieval_trace=retrieval_trace,
            # The search path is evidence-derived too.
            tried_lanes=(turn.tried_lanes or []) if evidence_visible else [],
            fallback_disclosure=turn.fallback_disclosure and evidence_visible,
            warning_codes=turn.warning_codes or [],
            graph_context=turn_graph_context,
            link_targets=link_targets,
        )

    def _session_graph_contexts(
        self,
        session: Session,
        workspace_id: UUID,
        citations_by_turn: dict[UUID, tuple[list[UUID], list[UUID]]],
        actor: ActorContext | None,
    ) -> dict[UUID, list[GraphNeighborEntry]]:
        if self.graph_service is None:
            return {}
        page_ids = list(
            dict.fromkeys(
                context_ids[0]
                for context_ids, _artifact_ids in citations_by_turn.values()
                if context_ids
            )
        )
        if not page_ids:
            return {}
        return self.graph_service.context_pages_graph_context(
            session,
            workspace_id,
            page_ids=page_ids,
            actor=actor,
            limit=5,
        )

    def _turn_graph_context(
        self,
        session: Session,
        workspace_id: UUID,
        cited_page_ids: list[UUID],
        actor: ActorContext | None,
    ) -> list[GraphNeighborEntry]:
        if self.graph_service is None or not cited_page_ids:
            return []
        try:
            return self.graph_service.context_page_graph_context(
                session,
                workspace_id,
                page_id=cited_page_ids[0],
                actor=actor,
                limit=5,
            )
        except ReadNotFoundError:
            # A citation can outlive the actor-visible page head (archive or
            # policy change). The evidence remains in the turn ledger, but its
            # graph neighborhood must not make the hidden document observable.
            return []


def _restricted_answer_placeholder(language: str) -> str:
    return (
        "이 답변은 현재 접근 권한이 없는 근거를 인용하고 있어 표시할 수 없습니다."
        if language == "ko"
        else "This answer cites evidence you do not have access to and is hidden."
    )


def turn_count(session: Session, ask_session_id: UUID) -> int:
    return int(
        session.scalar(
            select(func.count(AskTurn.id)).where(
                AskTurn.ask_session_id == ask_session_id
            )
        )
        or 0
    )


def _turn_proposal_ids_by_turn(
    session: Session,
    turn_ids: list[UUID],
) -> dict[UUID, list[str]]:
    """Proposal ids per source turn (wire shape of the dropped
    AskTurn.context_ingress_proposal_ids column), oldest first."""

    if not turn_ids:
        return {}
    rows = session.execute(
        select(
            ContextIngressProposal.source_ask_turn_id,
            ContextIngressProposal.id,
        )
        .where(ContextIngressProposal.source_ask_turn_id.in_(turn_ids))
        .order_by(
            ContextIngressProposal.created_at,
            ContextIngressProposal.id,
        )
    ).all()
    proposals: dict[UUID, list[str]] = {}
    for turn_id, proposal_id in rows:
        proposals.setdefault(turn_id, []).append(str(proposal_id))
    return proposals
