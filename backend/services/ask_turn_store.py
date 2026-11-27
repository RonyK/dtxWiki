from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from backend.models.ask import (
    ASK_TURN_COMPLETED,
    ASK_TURN_FAILED,
    ASK_TURN_PENDING,
    AskSession,
    AskTurn,
    AskTurnCitation,
)
from backend.repositories.workspace_scoped import select_in_workspace
from backend.schemas.ask import (
    AskQuestion,
    AskTurnCreate,
)
from backend.schemas.input_policy import validate_request_inputs
from backend.services.ask_session_service import AskSessionService
from backend.services.ask_types import (
    ASK_FORWARDABLE_WARNINGS,
    ASK_SELF_WARNINGS,
    ASK_TURN_SERVER_RESTARTED,
    ComposedTurn,
    PreparedTurn,
    _validate_user_message,
)
from backend.services.audit_service import AuditService
from backend.services.coverage_service import record_coverage_gap, resolve_coverage_gap
from backend.services.db_write_intent import acquire_workspace_write_intent
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError
from backend.services.search_service import KEYWORD_LANE_UNAVAILABLE_WARNING

logger = logging.getLogger(__name__)


def fail_interrupted_ask_turns(session_factory: sessionmaker[Session]) -> int:
    """End every pending turn as failed; for server startup only.

    The serving process answers turns in its own requests, so at startup no
    pending turn has anything left to finish it: the questions stay, marked
    as an internal error. Never call this while another server may be
    answering on the same database.
    """

    with session_factory.begin() as session:
        acquire_workspace_write_intent(session)
        pending = session.scalars(
            update(AskTurn)
            .where(AskTurn.status == ASK_TURN_PENDING)
            .values(status=ASK_TURN_FAILED, error_code=ASK_TURN_SERVER_RESTARTED)
            .returning(AskTurn)
        ).all()
        for turn in pending:
            settle_steering(turn, "unknown")
        ended = len(pending)
    if ended:
        logger.warning("Ended %d Ask turns interrupted by a server restart", ended)
    return ended


def ask_session_for_turn_insert_statement(ask_session_id: UUID, workspace_id: UUID):
    return (
        select_in_workspace(
            AskSession,
            workspace_id=workspace_id,
            criteria=(AskSession.id == ask_session_id,),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def settle_steering(turn: AskTurn, status: str) -> None:
    """Give every instruction still waiting its final status."""

    if any(message["status"] == "waiting" for message in turn.steering_messages):
        turn.steering_messages = [
            {**message, "status": status} if message["status"] == "waiting" else message
            for message in turn.steering_messages
        ]


class AskTurnStore:
    def __init__(
        self, audit_service: AuditService, sessions: AskSessionService
    ) -> None:
        self.audit_service = audit_service
        self._sessions = sessions

    def submit_turn(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        ask_session_id: UUID,
        request: AskQuestion,
        actor_kind: str,
        actor_id: str,
        previous_user_message: str | None = None,
    ) -> UUID:
        """Queue the question as a ``pending`` turn and return its id.

        ``previous_user_message`` is the stopped question a replacement
        continues; the combined text obeys the same limits.
        """

        validate_request_inputs(request)
        question = _validate_user_message(request.user_message)
        if previous_user_message:
            question = _validate_user_message(f"{previous_user_message}\n\n{question}")
        # The session row lock orders every turn write in the session, so
        # turn_no allocation cannot collide. On SQLite the writer lock comes
        # first, before the guarded read.
        acquire_workspace_write_intent(session)
        ask_session = self._lock_session_for_turn_insert(
            session=session,
            ask_session_id=ask_session_id,
            workspace_id=workspace_id,
        )
        if ask_session is None:
            raise ReadNotFoundError("ask session not found")
        latest = session.execute(
            select(AskTurn.id, AskTurn.turn_no)
            .where(
                AskTurn.workspace_id == workspace_id,
                AskTurn.ask_session_id == ask_session.id,
            )
            .order_by(AskTurn.turn_no.desc())
            .limit(1)
        ).first()
        if (
            isinstance(request, AskTurnCreate)
            and "after_turn_id" in request.model_fields_set
            and request.after_turn_id != (latest.id if latest is not None else None)
        ):
            raise RejectedByPolicyError(
                "the session has turns this question was not asked after",
                error_code="ask_session_changed",
            )
        turn = AskTurn(
            workspace_id=ask_session.workspace_id,
            ask_session_id=ask_session.id,
            turn_no=(latest.turn_no if latest is not None else 0) + 1,
            user_message=question,
            assistant_message=None,
            status=ASK_TURN_PENDING,
            request_id=request.request_id,
            selection=(
                request.selection.model_dump(mode="json")
                if request.selection is not None
                else None
            ),
            topic_tags=ask_session.default_topic_tags,
            recommended_placement_tree_node_id=(
                ask_session.default_context_parent_tree_node_id
            ),
        )
        session.add(turn)
        session.flush()
        ask_session.updated_at = datetime.now(UTC)
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="ask.turn.create",
            target_kind="ask_turn",
            target_id=turn.id,
            payload={"ask_session_id": str(ask_session.id), "turn_no": turn.turn_no},
            event_summary=f"Ask turn created in {ask_session.title}",
        )
        return turn.id

    def finish_turn(
        self,
        session: Session,
        workspace_id: UUID,
        prepared: PreparedTurn,
        composed: ComposedTurn,
    ) -> None:
        """Store the answer if the turn is still pending.

        Another ending (stop, startup recovery) may have been stored first;
        then nothing is written. Read the turn with ``get_turn``.
        """

        acquire_workspace_write_intent(session)
        ask_session = self._lock_session_for_turn_insert(
            session=session,
            ask_session_id=prepared.ask_session_id,
            workspace_id=workspace_id,
        )
        if ask_session is None:
            raise ReadNotFoundError("ask session not found")
        if not self._end_pending_turn(
            session, workspace_id, prepared.turn_id, ASK_TURN_COMPLETED
        ):
            return
        turn = self._reload_turn(session, workspace_id, prepared.turn_id)
        retrieved = prepared.retrieved
        warning_codes = [
            code
            for code in composed.warnings
            if code in ASK_FORWARDABLE_WARNINGS or code in ASK_SELF_WARNINGS
        ]
        if not retrieved.found:
            if KEYWORD_LANE_UNAVAILABLE_WARNING not in warning_codes:
                gap = record_coverage_gap(
                    session,
                    workspace_id=workspace_id,
                    query=prepared.question,
                    surface="ask",
                    tried_lanes=list(retrieved.tried_lanes),
                    nearest_candidates=list(retrieved.nearest_candidates),
                )
                if gap is not None:
                    warning_codes.append("coverage_gap_recorded")
        elif not retrieved.pack:
            warning_codes.append("evidence_not_citable")
        else:
            resolve_coverage_gap(
                session,
                workspace_id=workspace_id,
                query=prepared.question,
            )
        turn.assistant_message = composed.answer
        turn.placement_confidence = "medium" if retrieved.found else "low"
        turn.retrieval_trace = list(retrieved.trace)
        turn.tried_lanes = list(retrieved.tried_lanes)
        turn.fallback_disclosure = retrieved.fallback
        turn.warning_codes = warning_codes
        turn.input_evidence = prepared.evidence.to_record()
        turn.generation = composed.generation
        for kind, cited_ids in (
            ("context_page", retrieved.context_ids),
            ("library_artifact", retrieved.artifact_ids),
        ):
            for rank, item_id in enumerate(cited_ids):
                session.add(
                    AskTurnCitation(
                        workspace_id=turn.workspace_id,
                        ask_turn_id=turn.id,
                        target_kind=kind,
                        target_id=item_id,
                        rank=rank,
                    )
                )
        session.flush()
        self._sessions.refresh_title(
            session=session,
            ask_session=ask_session,
        )
        ask_session.updated_at = datetime.now(UTC)

    def fail_turn(
        self,
        session: Session,
        workspace_id: UUID,
        turn_id: UUID,
        error_code: str,
    ) -> bool:
        """End a pending turn as failed, keeping its question."""

        acquire_workspace_write_intent(session)
        ask_session_id = session.scalar(
            select(AskTurn.ask_session_id).where(
                AskTurn.id == turn_id, AskTurn.workspace_id == workspace_id
            )
        )
        if ask_session_id is None:
            return False
        self._lock_session_for_turn_insert(
            session=session, ask_session_id=ask_session_id, workspace_id=workspace_id
        )
        return self._end_pending_turn(
            session, workspace_id, turn_id, ASK_TURN_FAILED, error_code
        )

    @staticmethod
    def _end_pending_turn(
        session: Session,
        workspace_id: UUID,
        turn_id: UUID,
        status: str,
        error_code: str | None = None,
    ) -> bool:
        """Move a pending turn to ``status``; False if it had already ended."""

        result = session.execute(
            update(AskTurn)
            .where(
                AskTurn.workspace_id == workspace_id,
                AskTurn.id == turn_id,
                AskTurn.status == ASK_TURN_PENDING,
            )
            .values(status=status, error_code=error_code)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    @staticmethod
    def _reload_turn(session: Session, workspace_id: UUID, turn_id: UUID) -> AskTurn:
        # The status was just changed by an UPDATE the identity map did not see.
        return session.scalars(
            select(AskTurn)
            .where(AskTurn.workspace_id == workspace_id, AskTurn.id == turn_id)
            .execution_options(populate_existing=True)
        ).one()

    @staticmethod
    def _lock_session_for_turn_insert(
        session: Session,
        ask_session_id: UUID,
        workspace_id: UUID,
    ) -> AskSession | None:
        return session.scalar(
            ask_session_for_turn_insert_statement(ask_session_id, workspace_id)
        )
