from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID

from sqlalchemy import exists, func, select, tuple_, update
from sqlalchemy.orm import Session, sessionmaker

from backend.domain.input_policy import (
    InputPolicyError,
    fit_generated_title,
    normalize_ask_user_message,
)
from backend.llm.generation import TextGenerator
from backend.llm.streaming import SteerInput, SteerUpdate, generate_stream
from backend.models.ask import (
    ASK_TURN_COMPLETED,
    ASK_TURN_FAILED,
    ASK_TURN_PENDING,
    AskSession,
    AskTurn,
    AskTurnCitation,
    ContextIngressProposal,
)
from backend.models.chunk import ContentChunk
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.tree import TreePolicy, WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace, select_in_workspace
from backend.schemas.ask import (
    AskQuestion,
    AskSessionCreate,
    AskSessionDetail,
    AskSessionListData,
    AskSessionRef,
    AskTurnCreate,
    AskTurnDetail,
)
from backend.schemas.input_policy import validate_request_inputs
from backend.schemas.read import GraphNeighborEntry, RankedResult
from backend.services.actor_visible_read import (
    actor_can_read_library_trust_level,
    context_page_visible,
    context_version_is_visible,
)
from backend.services.ask_connection_service import AskGenerationBinding
from backend.services.audit_service import AuditService
from backend.services.context_link_targets import ask_context_link_targets
from backend.services.coverage_service import record_coverage_gap, resolve_coverage_gap
from backend.services.db_write_intent import acquire_workspace_write_intent
from backend.services.graph_service import GraphService
from backend.services.library_canonical import (
    canonical_library_artifact_belongs_to_item,
    get_canonical_library_version,
    get_canonical_markdown_artifact,
    get_effective_library_markdown,
)
from backend.services.list_pagination import (
    _decode_keyset_cursor,
    _encode_keyset_cursor,
)
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError
from backend.services.retrieval.identity import ranked_result_document_id
from backend.services.search_service import (
    KEYWORD_LANE_UNAVAILABLE_WARNING,
    SearchService,
)
from backend.services.tree_read import TreeReadService
from backend.services.tree_service import (
    create_tree_node,
    resolve_tree_parent,
)
from backend.spi.llm import (
    CanonicalTextRequest,
    CanonicalTextResult,
    ExecutionContext,
    LLMAdapterError,
    TextMessage,
    TokenUsage,
)
from backend.utils.uuid import parse_uuid_or_none

USER_MESSAGE_MAX_CHARS = 8000
ASK_TITLE_MAX_WORDS = 8
# Search-lane advisory codes that are meaningful on the Ask surface too: the
# keyword-lane degradation and the restricted-Library exclusion notice are both
# trust disclosures about the evidence the answer was built on. Codes whose
# affordance lives only on the search response (e.g. unsupported search filters)
# are NOT forwarded — they would render an actionless banner on an Ask turn.
ASK_FORWARDABLE_WARNINGS = frozenset(
    {KEYWORD_LANE_UNAVAILABLE_WARNING, "permission_limited"}
)
# Codes Ask raises about its OWN answer pipeline rather than about a search
# lane. They travel back on the same list so they reach the turn unfiltered.
# The answer-model warnings tell the reader when to contact an administrator,
# when to try again, and when the model returned an unusable or declined reply.
LLM_PROVIDER_UNAVAILABLE_WARNING = "llm_provider_unavailable"
LLM_COMPOSER_RETRYABLE_WARNING = "llm_composer_retryable"
LLM_ANSWER_INCOMPLETE_WARNING = "llm_answer_incomplete"
LLM_ANSWER_REFUSED_WARNING = "llm_answer_refused"
# The workspace asked for a retrieval mode this deployment cannot serve. Already
# registered in WARNING_CODES with no producer -- reserved for exactly this.
UNSUPPORTED_RETRIEVAL_MODE_FALLBACK_WARNING = "unsupported_retrieval_mode_fallback"
ASK_SELF_WARNINGS = frozenset(
    {
        LLM_PROVIDER_UNAVAILABLE_WARNING,
        LLM_COMPOSER_RETRYABLE_WARNING,
        LLM_ANSWER_INCOMPLETE_WARNING,
        LLM_ANSWER_REFUSED_WARNING,
        UNSUPPORTED_RETRIEVAL_MODE_FALLBACK_WARNING,
    }
)
# A reply that is not a completed answer, by how it ended. Anything else
# (including completed but blank) is incomplete.
_TERMINATION_WARNINGS: Mapping[str, str] = MappingProxyType(
    {
        "refused": LLM_ANSWER_REFUSED_WARNING,
        "blocked": LLM_ANSWER_REFUSED_WARNING,
    }
)
# A failed call, by its normalized code. Codes not listed need an
# administrator (credentials, permissions, model, quota, request shape).
_ERROR_WARNINGS: Mapping[str, str] = MappingProxyType(
    {
        "rate_limited": LLM_COMPOSER_RETRYABLE_WARNING,
        "provider_unavailable": LLM_COMPOSER_RETRYABLE_WARNING,
        "timeout": LLM_COMPOSER_RETRYABLE_WARNING,
        "network_error": LLM_COMPOSER_RETRYABLE_WARNING,
        "invalid_response": LLM_ANSWER_INCOMPLETE_WARNING,
    }
)
PROMPT_INJECTION_PATTERNS = (
    "<|im_start|>",
    "<|im_end|>",
    "</system>",
    "<|system|>",
    "<|assistant|>",
    "<|user|>",
)
# "</s>" doubles as the HTML strikethrough close tag, so it is only treated
# as a sentencepiece EOS marker when it does not close an earlier <s> tag.
_EOS_MARKER_RE = re.compile(r"</s>")
_STRIKE_OPEN_RE = re.compile(r"<s(?:\s[^>]*)?>")
_COMPOSER_SYSTEM = (
    "Answer only from the supplied evidence pack and cite its items by their "
    "list number. Earlier messages in this conversation only tell you what the "
    "question refers to; numbers in them are not citations. If the pack does "
    "not support an answer, say that you do not know. If the question could "
    "mean more than one thing, ask which one is meant."
)
# Earlier turns shown to the model with a new question. Kept small: models
# lose facts in long inputs, and each earlier answer already used its own pack.
ASK_HISTORY_MAX_PAIRS = 4
# The model's input limit is not known here, so earlier turns get a fixed,
# conservative share.
ASK_HISTORY_MAX_CHARS = 16_000
# Rows read at a time while looking for the history.
_HISTORY_BATCH = 20
# Library sources checked per query, well under any database's bind limit.
_VISIBILITY_BATCH = 500
# Why a turn ended as failed. Only these codes reach clients.
ASK_TURN_INTERNAL_ERROR = "internal_error"
ASK_TURN_ACCESS_REVOKED = "access_revoked"
ASK_TURN_EVIDENCE_UNAVAILABLE = "evidence_unavailable"
ASK_TURN_SERVER_RESTARTED = "server_restarted"
ASK_MODEL_ERROR_CODES = frozenset(
    {
        LLM_PROVIDER_UNAVAILABLE_WARNING,
        LLM_COMPOSER_RETRYABLE_WARNING,
        LLM_ANSWER_INCOMPLETE_WARNING,
        LLM_ANSWER_REFUSED_WARNING,
    }
)
_MODEL_NETWORK_TIMEOUT_SECONDS = 120.0
_MODEL_DEADLINE_SECONDS = 180.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AskEvidence:
    """Sources an answer was given, as stored in ``AskTurn.input_evidence``."""

    # (page_id, version_id): the version whose text the model was given.
    context: frozenset[tuple[UUID, UUID]] = frozenset()
    library: frozenset[UUID] = frozenset()

    def union(self, other: AskEvidence) -> AskEvidence:
        return AskEvidence(self.context | other.context, self.library | other.library)

    def to_record(self) -> dict[str, list[dict[str, str]]]:
        return {
            "context": [
                {"page_id": str(page_id), "version_id": str(version_id)}
                for page_id, version_id in sorted(
                    self.context, key=lambda ref: (str(ref[0]), str(ref[1]))
                )
            ],
            "library": [
                {"artifact_id": str(artifact_id)}
                for artifact_id in sorted(self.library, key=str)
            ],
        }

    @classmethod
    def from_record(cls, record: object) -> AskEvidence | None:
        """Parse a stored record; None if it is malformed."""

        if not isinstance(record, Mapping):
            return None
        context = record.get("context")
        library = record.get("library")
        if not isinstance(context, list) or not isinstance(library, list):
            return None
        refs: set[tuple[UUID, UUID]] = set()
        for entry in context:
            if not isinstance(entry, Mapping):
                return None
            page_id = parse_uuid_or_none(entry.get("page_id"))
            version_id = parse_uuid_or_none(entry.get("version_id"))
            if page_id is None or version_id is None:
                return None
            refs.add((page_id, version_id))
        artifact_ids: set[UUID] = set()
        for entry in library:
            artifact_id = (
                parse_uuid_or_none(entry.get("artifact_id"))
                if isinstance(entry, Mapping)
                else None
            )
            if artifact_id is None:
                return None
            artifact_ids.add(artifact_id)
        return cls(frozenset(refs), frozenset(artifact_ids))


@dataclass(frozen=True)
class RetrievedEvidence:
    language: str
    # Numbered pack lines, proven sources only.
    pack: tuple[str, ...]
    context_ids: tuple[UUID, ...]
    artifact_ids: tuple[UUID, ...]
    evidence: AskEvidence
    trace: tuple[dict[str, str], ...]
    tried_lanes: tuple[str, ...]
    fallback: bool
    nearest_candidates: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]
    # Search returned hits, proven or not.
    found: bool


@dataclass(frozen=True)
class PreparedTurn:
    turn_id: UUID
    ask_session_id: UUID
    question: str
    retrieved: RetrievedEvidence
    # Earlier (question, answer) pairs shown to the model, oldest first.
    history: tuple[tuple[str, str], ...]
    # This turn's evidence plus the evidence of every earlier turn shown.
    evidence: AskEvidence


@dataclass(frozen=True)
class ComposedTurn:
    answer: str
    warnings: tuple[str, ...]
    generation: dict[str, Any]


def _usage_record(usage: TokenUsage) -> dict[str, int | None]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }


def _evidence_pack_heading(language: str) -> str:
    return (
        "컨텍스트 우선 검색 요약:"
        if language == "ko"
        else "Context-first retrieval summary:"
    )


def _pack_text(retrieved: RetrievedEvidence) -> str:
    return "\n".join((_evidence_pack_heading(retrieved.language), *retrieved.pack))


def _composer_messages(
    prepared: PreparedTurn, pack_text: str
) -> tuple[TextMessage, ...]:
    messages = [
        message
        for asked, answered in prepared.history
        for message in (
            TextMessage(role="user", text=asked),
            TextMessage(role="assistant", text=answered),
        )
    ]
    messages.append(
        TextMessage(
            role="user", text=f"{prepared.question}\n\n[Evidence pack]\n{pack_text}"
        )
    )
    return tuple(messages)


def _generation_record(
    result: CanonicalTextResult, target: dict[str, str] | None, outcome: str
) -> dict[str, Any]:
    return {
        "target": target,
        "outcome": outcome,
        "model": result.model,
        "usage": _usage_record(result.usage),
        "provider_request_id": result.provider_request_id,
    }


def _history_within_context(
    history: tuple[tuple[str, str, AskEvidence], ...],
    question: str,
    retrieved: RetrievedEvidence,
    binding: AskGenerationBinding,
) -> tuple[tuple[str, str, AskEvidence], ...]:
    """Use UTF-8 size as a conservative estimate for optional history."""
    assert binding.context_window is not None
    remaining = (
        binding.context_window
        - binding.max_output_tokens
        - len((_COMPOSER_SYSTEM + question + _pack_text(retrieved)).encode("utf-8"))
        - 128  # Message framing and provider-specific formatting.
    )
    start = len(history)
    for asked, answered, _evidence in reversed(history):
        remaining -= len(asked.encode("utf-8")) + len(answered.encode("utf-8")) + 16
        if remaining < 0:
            break
        start -= 1
    return history[start:]


def _no_evidence_answer(language: str) -> str:
    return (
        "일치하는 Context 또는 Library 근거를 찾지 못했습니다."
        if language == "ko"
        else "No matching Context or Library evidence was found."
    )


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
            if any(
                message["status"] == "waiting" for message in turn.steering_messages
            ):
                turn.steering_messages = [
                    {**message, "status": "unknown"}
                    if message["status"] == "waiting"
                    else message
                    for message in turn.steering_messages
                ]
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


class AskAccessRevokedError(Exception):
    """The asker lost access while the answer was being written.

    The cause is the original authentication or policy refusal.
    """


class AskGenerationError(RuntimeError):
    """A selected model failed. The original question may be searched again."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.error_code = code


class _CallerError(Exception):
    """Carries an exception raised by a callback the model adapter called."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


def _from_caller[C: Callable[..., Awaitable[Any]]](callback: C | None) -> C | None:
    """Mark the callback's own failures so they are not taken for the model's."""

    if callback is None:
        return None

    async def call(*args: Any) -> Any:
        try:
            return await callback(*args)
        except Exception as exc:
            raise _CallerError(exc) from exc

    return cast(C, call)


class InvalidUserMessageError(ValueError):
    """Raised when user_message fails validation."""


def _validate_user_message(raw: str) -> str:
    try:
        stripped = normalize_ask_user_message(raw)
    except InputPolicyError as exc:
        raise InvalidUserMessageError(str(exc)) from exc
    cleaned = "".join(
        ch for ch in stripped if ch.isprintable() or ch in ("\n", "\t", "\r")
    ).strip()
    if not cleaned:
        raise InvalidUserMessageError("user_message must not be empty")
    lowered = cleaned.lower()
    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern in lowered:
            raise InvalidUserMessageError(
                "user_message contains forbidden prompt delimiter"
            )
    if _has_unmatched_eos_marker(lowered):
        raise InvalidUserMessageError(
            "user_message contains forbidden prompt delimiter"
        )
    return cleaned


def _has_unmatched_eos_marker(lowered: str) -> bool:
    """True when a "</s>" occurrence does not close an earlier <s> tag, i.e.
    it reads as a standalone EOS delimiter rather than HTML strikethrough."""
    events = sorted(
        [(match.start(), 1) for match in _STRIKE_OPEN_RE.finditer(lowered)]
        + [(match.start(), -1) for match in _EOS_MARKER_RE.finditer(lowered)]
    )
    depth = 0
    for _position, delta in events:
        depth += delta
        if depth < 0:
            return True
    return False


def _stable_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def derive_title_snippet(message: str, max_words: int = ASK_TITLE_MAX_WORDS) -> str:
    cleaned = re.sub(r"\s+", " ", message).strip(" \t\r\n#*_`>-")
    if not cleaned:
        return ""
    words = cleaned.split(" ")
    snippet = " ".join(words[:max_words]).strip(" .,:;!?")
    if len(words) > max_words:
        snippet = f"{snippet}..."
    return fit_generated_title(snippet, max_length=96) if snippet else ""


class AskService:
    def __init__(
        self,
        search_service: SearchService | None = None,
        audit_service: AuditService | None = None,
        tree_read_service: TreeReadService | None = None,
        provider: TextGenerator | None = None,
        graph_service: GraphService | None = None,
        provider_unavailable_reason: str | None = None,
        provider_target: Mapping[str, str] | None = None,
    ) -> None:
        self.search_service = search_service or SearchService()
        self.audit_service = audit_service or AuditService()
        self.tree_read_service = tree_read_service or TreeReadService()
        self.provider = provider
        self.graph_service = graph_service
        # Set when the configured LLM provider could not be built. Ask still
        # answers from the deterministic evidence pack; the reason is reported
        # so the operator (and the user) learn why the answer style changed
        # instead of the request failing.
        self.provider_unavailable_reason = provider_unavailable_reason
        # Which model answers: an answer is shown later only to the same model,
        # so the earlier conversation never goes to a different endpoint.
        self.provider_target = dict(provider_target) if provider_target else None

    def list_sessions(
        self,
        session: Session,
        workspace_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> AskSessionListData:
        base = select(AskSession).where(AskSession.workspace_id == workspace_id)
        total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
        # Keyset requires the ORDER BY columns to match the cursor WHERE columns.
        # The cursor compares (updated_at, id), so the ordering must be exactly
        # (updated_at desc, id desc) — id (a UUID) is already a unique tiebreak,
        # so a started_at term here would only break the cursor (skip/duplicate
        # rows when updated_at ties and started_at order != id order).
        stmt = base.order_by(
            AskSession.updated_at.desc(),
            AskSession.id.desc(),
        )
        decoded = _decode_keyset_cursor(cursor)
        if decoded is not None:
            cur_ts, cur_id = decoded
            stmt = stmt.where(
                tuple_(AskSession.updated_at, AskSession.id) < (cur_ts, cur_id)
            )
        sessions = session.scalars(stmt.limit(limit + 1)).all()
        page, has_more = sessions[:limit], len(sessions) > limit
        next_cursor = (
            _encode_keyset_cursor(page[-1].updated_at, page[-1].id)
            if has_more and page
            else None
        )
        return AskSessionListData(
            items=[self._session_ref(session, ask_session) for ask_session in page],
            date_buckets=date_buckets(page),
            next_cursor=next_cursor,
            total=total,
            has_more=has_more,
        )

    def create_session(
        self,
        session: Session,
        workspace_id: UUID,
        request: AskSessionCreate,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> AskSessionDetail:
        validate_request_inputs(request)
        now = datetime.now(UTC)
        day_node = self._ensure_ask_day_node(
            session=session,
            workspace_id=workspace_id,
            at=now,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        default_context_parent = resolve_tree_parent(
            session=session,
            workspace_id=workspace_id,
            section="context",
            parent_id=request.default_context_parent_tree_node_id,
            default_parent_slug="inbox",
        )
        ask_session = AskSession(
            workspace_id=workspace_id,
            title=(
                request.title.strip()
                if request.title and request.title.strip()
                else _default_ask_title(now)
            ),
            title_user_edited=bool(request.title and request.title.strip()),
            actor_kind=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            default_context_parent_tree_node_id=(
                default_context_parent.id if default_context_parent else None
            ),
            default_topic_tags=request.default_topic_tags,
            placement_tree_node_id=day_node.id,
        )
        session.add(ask_session)
        session.flush()
        session_node = create_tree_node(
            session,
            workspace_id=workspace_id,
            section="ask",
            node_kind="ask_session_ref",
            parent_id=day_node.id,
            title=ask_session.title,
            target_kind="ask_session",
            target_id=ask_session.id,
            primary_placement=True,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        ask_session.placement_tree_node_id = session_node.id
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="ask.session.create",
            target_kind="ask_session",
            target_id=ask_session.id,
            event_summary=f"Ask session created: {ask_session.title}",
        )
        return self._session_detail(session, ask_session, actor=actor)

    def get_session(
        self,
        session: Session,
        workspace_id: UUID,
        ask_session_id: UUID,
        actor: ActorContext | None = None,
    ) -> AskSessionDetail:
        return self._session_detail(
            session=session,
            ask_session=self._require_session(session, workspace_id, ask_session_id),
            actor=actor,
        )

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
        question = _validate_user_message(
            f"{previous_user_message}\n\n{request.user_message}"
            if previous_user_message
            else request.user_message
        )
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

    def prepare_turn(
        self,
        session: Session,
        workspace_id: UUID,
        turn_id: UUID,
        actor: ActorContext | None = None,
        *,
        binding: AskGenerationBinding | None = None,
        keyword_only: bool = False,
        use_legacy_provider: bool = True,
    ) -> PreparedTurn:
        """Find this question's evidence and the earlier turns to show."""

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
        use_model = binding is not None or (
            use_legacy_provider and self.provider is not None
        )
        history = (
            self._history(
                session,
                workspace_id,
                turn,
                actor,
                target=binding.target if binding is not None else self.provider_target,
                generated_only=binding is not None,
            )
            if use_model and retrieved.pack
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

    @staticmethod
    def compose_evidence(prepared: PreparedTurn) -> ComposedTurn:
        """Compose a local search result without an LLM generation call."""
        retrieved = prepared.retrieved
        return ComposedTurn(
            answer=(
                _pack_text(retrieved)
                if retrieved.pack
                else _no_evidence_answer(retrieved.language)
            ),
            warnings=retrieved.warnings,
            generation={
                "target": None,
                "outcome": "evidence_pack" if retrieved.pack else "no_evidence",
            },
        )

    async def stream_turn(
        self,
        prepared: PreparedTurn,
        binding: AskGenerationBinding | None,
        *,
        on_delta: Callable[[str], Awaitable[None]],
        next_steer: Callable[[], Awaitable[SteerInput]] | None = None,
        on_steer: Callable[[SteerUpdate], Awaitable[None]] | None = None,
    ) -> ComposedTurn:
        """Run the selected model, streaming only provisional text."""
        retrieved = prepared.retrieved
        if binding is None or not retrieved.pack:
            return self.compose_evidence(prepared)
        request = CanonicalTextRequest(
            system=_COMPOSER_SYSTEM,
            model=binding.model,
            messages=_composer_messages(prepared, _pack_text(retrieved)),
            max_output_tokens=binding.max_output_tokens,
        )
        steering = binding.steer
        try:
            async with asyncio.timeout(_MODEL_DEADLINE_SECONDS):
                result = await generate_stream(
                    binding.port,
                    request,
                    ExecutionContext(timeout_seconds=_MODEL_NETWORK_TIMEOUT_SECONDS),
                    on_delta=_from_caller(on_delta),
                    next_steer=_from_caller(next_steer) if steering else None,
                    on_steer=_from_caller(on_steer) if steering else None,
                )
        except _CallerError as failure:
            # The application's own callback failed, not the model.
            raise failure.error from None
        except TimeoutError:
            raise AskGenerationError(LLM_COMPOSER_RETRYABLE_WARNING) from None
        except LLMAdapterError as exc:
            raise AskGenerationError(
                _ERROR_WARNINGS.get(exc.code, LLM_PROVIDER_UNAVAILABLE_WARNING)
            ) from None
        except Exception:
            # Any other failure of the selected model's adapter is a model
            # failure like the ones above: the asker can search instead.
            logger.exception("Ask model adapter failed")
            raise AskGenerationError(LLM_PROVIDER_UNAVAILABLE_WARNING) from None
        if result.termination != "completed" or not result.text.strip():
            raise AskGenerationError(
                _TERMINATION_WARNINGS.get(
                    result.termination, LLM_ANSWER_INCOMPLETE_WARNING
                )
            )
        return ComposedTurn(
            answer=result.text.strip(),
            warnings=retrieved.warnings,
            generation=_generation_record(result, binding.target, "completed"),
        )

    def compose_turn(self, prepared: PreparedTurn) -> ComposedTurn:
        """Write the answer. Uses no database session."""

        retrieved = prepared.retrieved
        warnings = list(retrieved.warnings)
        target = self.provider_target
        if not retrieved.pack:
            return ComposedTurn(
                answer=_no_evidence_answer(retrieved.language),
                warnings=tuple(warnings),
                generation={"target": target, "outcome": "no_evidence"},
            )
        pack_text = _pack_text(retrieved)
        if self.provider_unavailable_reason:
            # Configured but unbuildable provider: a persistent, operator-owned
            # condition. Disclose it instead of silently serving a different
            # answer style than the workspace is configured for.
            warnings.append(LLM_PROVIDER_UNAVAILABLE_WARNING)
        if self.provider is None:
            return ComposedTurn(
                answer=pack_text,
                warnings=tuple(warnings),
                generation={"target": target, "outcome": "evidence_pack"},
            )
        try:
            result = self.provider.generate(
                system=_COMPOSER_SYSTEM,
                messages=_composer_messages(prepared, pack_text),
                max_output_tokens=1024,
            )
        except LLMAdapterError as exc:
            # The pack is a valid answer, but the user must know it is not
            # the composed one, and whether retrying or an admin can help.
            logger.warning(
                "Ask composer call failed (%s); using deterministic pack",
                exc.code,
            )
            warnings.append(
                _ERROR_WARNINGS.get(exc.code, LLM_PROVIDER_UNAVAILABLE_WARNING)
            )
            return ComposedTurn(
                answer=pack_text,
                warnings=tuple(warnings),
                generation={"target": target, "outcome": "evidence_pack"},
            )
        except Exception:
            # Unexpected composer errors need an administrator to inspect
            # the server log.
            logger.warning(
                "Ask composer failed unexpectedly; using deterministic pack",
                exc_info=True,
            )
            warnings.append(LLM_PROVIDER_UNAVAILABLE_WARNING)
            return ComposedTurn(
                answer=pack_text,
                warnings=tuple(warnings),
                generation={"target": target, "outcome": "evidence_pack"},
            )
        # Only a completed answer replaces the pack: truncated, refused or
        # filtered output is not an answer.
        if result.termination == "completed" and result.text.strip():
            return ComposedTurn(
                answer=result.text.strip(),
                warnings=tuple(warnings),
                generation=_generation_record(result, target, "completed"),
            )
        warnings.append(
            _TERMINATION_WARNINGS.get(result.termination, LLM_ANSWER_INCOMPLETE_WARNING)
        )
        return ComposedTurn(
            answer=pack_text,
            warnings=tuple(warnings),
            generation=_generation_record(result, target, "evidence_pack"),
        )

    def finish_turn(
        self,
        session: Session,
        workspace_id: UUID,
        prepared: PreparedTurn,
        composed: ComposedTurn,
        *,
        actor: ActorContext | None = None,
    ) -> tuple[AskTurnDetail, list[str]]:
        """Store the answer if the turn is still pending.

        Another ending (stop, startup recovery) may have been stored first;
        then nothing is written and the stored turn is returned.
        """

        acquire_workspace_write_intent(session)
        ask_session = self._lock_session_for_turn_insert(
            session=session,
            ask_session_id=prepared.ask_session_id,
            workspace_id=workspace_id,
        )
        if ask_session is None:
            raise ReadNotFoundError("ask session not found")
        if actor is not None and not ask_evidence_visible(
            session,
            context_refs=prepared.evidence.context,
            artifact_ids=prepared.evidence.library,
            actor=actor,
            workspace_id=workspace_id,
        ):
            # The asker lost access to evidence while the answer was being
            # written: keep the question, store no answer.
            return self._end_unanswered(
                session, workspace_id, prepared, ASK_TURN_EVIDENCE_UNAVAILABLE, actor
            )
        if not self._end_pending_turn(
            session, workspace_id, prepared.turn_id, ASK_TURN_COMPLETED
        ):
            return self._stored_turn(session, ask_session, prepared.turn_id, actor), []
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
        self._refresh_ask_title(
            session=session,
            ask_session=ask_session,
        )
        ask_session.updated_at = datetime.now(UTC)
        return (
            self._turn_detail(
                session=session,
                ask_session=ask_session,
                turn=turn,
                actor=actor,
                revalidate_evidence=False,
            ),
            warning_codes,
        )

    def _end_unanswered(
        self,
        session: Session,
        workspace_id: UUID,
        prepared: PreparedTurn,
        error_code: str,
        actor: ActorContext | None,
    ) -> tuple[AskTurnDetail, list[str]]:
        self.fail_turn(session, workspace_id, prepared.turn_id, error_code)
        ask_session = self._require_session(
            session, workspace_id, prepared.ask_session_id
        )
        return self._stored_turn(session, ask_session, prepared.turn_id, actor), []

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

    def _stored_turn(
        self,
        session: Session,
        ask_session: AskSession,
        turn_id: UUID,
        actor: ActorContext | None,
    ) -> AskTurnDetail:
        turn = self._reload_turn(session, ask_session.workspace_id, turn_id)
        return self._turn_detail(
            session=session,
            ask_session=ask_session,
            turn=turn,
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
        prepared = self.prepare_turn(
            session,
            workspace_id,
            turn_id,
            actor,
            keyword_only=True,
            use_legacy_provider=False,
        )
        retrieved = prepared.retrieved
        if not ask_evidence_visible(
            session,
            context_refs=retrieved.evidence.context,
            artifact_ids=retrieved.evidence.library,
            actor=actor,
            workspace_id=workspace_id,
        ):
            raise RejectedByPolicyError(
                "The search evidence is unavailable.",
                error_code=ASK_TURN_EVIDENCE_UNAVAILABLE,
            )
        composed = self.compose_evidence(prepared)
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
    def _reload_turn(session: Session, workspace_id: UUID, turn_id: UUID) -> AskTurn:
        # The status was just changed by an UPDATE the identity map did not see.
        return session.scalars(
            select(AskTurn)
            .where(AskTurn.workspace_id == workspace_id, AskTurn.id == turn_id)
            .execution_options(populate_existing=True)
        ).one()

    def _history(
        self,
        session: Session,
        workspace_id: UUID,
        turn: AskTurn,
        actor: ActorContext | None,
        *,
        target: dict[str, str] | None,
        generated_only: bool,
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
                if generated_only and generation.get("outcome") != "completed":
                    # A deliberate local search does not reset the conversation.
                    # An actual answer from a different destination still does.
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

    @staticmethod
    def _lock_session_for_turn_insert(
        session: Session,
        ask_session_id: UUID,
        workspace_id: UUID,
    ) -> AskSession | None:
        return session.scalar(
            ask_session_for_turn_insert_statement(ask_session_id, workspace_id)
        )

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

    def _ensure_ask_day_node(
        self,
        session: Session,
        workspace_id: UUID,
        at: datetime,
        actor_kind: str,
        actor_id: str,
    ) -> WorkspaceTreeNode:
        root = ensure_ask_root(
            session,
            workspace_id,
            actor_id=actor_id,
        )
        year = ensure_folder(
            session=session,
            workspace_id=workspace_id,
            parent=root,
            slug=f"ask-{at:%Y}",
            title=f"{at:%Y}",
            sort_key=f"{at:%Y}",
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        month = ensure_folder(
            session=session,
            workspace_id=workspace_id,
            parent=year,
            slug=f"ask-{at:%Y}-{at:%m}",
            title=f"{at:%m}",
            sort_key=f"{at:%m}",
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        return ensure_folder(
            session=session,
            workspace_id=workspace_id,
            parent=month,
            slug=f"ask-{at:%Y}-{at:%m}-{at:%d}",
            title=f"{at:%d}",
            sort_key=f"{at:%d}",
            actor_kind=actor_kind,
            actor_id=actor_id,
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

    def _session_ref(
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

    def _refresh_ask_title(
        self,
        session: Session,
        ask_session: AskSession,
    ) -> None:
        if ask_session.title_user_edited:
            return
        first_message = session.scalar(
            select(AskTurn.user_message)
            .where(AskTurn.ask_session_id == ask_session.id)
            .order_by(AskTurn.turn_no.asc())
            .limit(1)
        )
        if not first_message:
            return
        intent = derive_title_snippet(first_message)
        if not intent:
            return
        anchor = _dominant_context_anchor(session, ask_session)
        if anchor:
            anchor = fit_generated_title(anchor, max_length=256 - 2 - len(intent))
        next_title = fit_generated_title(f"{anchor}: {intent}" if anchor else intent)
        if next_title == ask_session.title:
            return
        ask_session.title = next_title
        _sync_ask_tree_node_title(session, ask_session)

    def _session_detail(
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
            **self._session_ref(session, ask_session).model_dump(),
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
        revalidate_evidence: bool = True,
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
        # whole evidence-derived payload rather than replay it. On the creation
        # return, the actor IS the author whose search already filtered the
        # evidence, so re-validation is skipped (redundant, and a divergence there
        # would falsely blank the author's own just-created answer).
        evidence_visible = not revalidate_evidence or ask_turn_visible(
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
            steer_supported=turn.steer_supported,
            steering_messages=turn.steering_messages or [],
            can_search=(
                turn.status == ASK_TURN_FAILED
                and turn.selection is not None
                and turn.error_code in ASK_MODEL_ERROR_CODES
            ),
            created_at=turn.created_at,
            started_at=turn.started_at,
            tree_path=tree_path,
            retrieval_trace=retrieval_trace,
            tried_lanes=turn.tried_lanes or [],
            fallback_disclosure=turn.fallback_disclosure,
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


def ask_turn_visible(
    session: Session,
    turn: AskTurn,
    citations: tuple[list[UUID], list[UUID]],
    actor: ActorContext | None,
    workspace_id: UUID,
    cache: dict[tuple[str, UUID, UUID | None], bool] | None = None,
) -> bool:
    """Whether ``actor`` may see the answer of ``turn``.

    A turn with ``input_evidence`` is checked against everything its model was
    given, earlier turns' evidence included: an answer that drew on a
    restricted source through the conversation is hidden with it. A malformed
    record is hidden. Older turns are checked against their citations.
    """

    if turn.input_evidence is not None:
        evidence = AskEvidence.from_record(turn.input_evidence)
        return evidence is not None and ask_evidence_visible(
            session,
            context_refs=evidence.context,
            artifact_ids=evidence.library,
            actor=actor,
            workspace_id=workspace_id,
            cache=cache,
        )
    cited_page_ids, cited_artifact_ids = citations
    return ask_evidence_visible(
        session,
        context_refs=[(page_id, None) for page_id in cited_page_ids],
        artifact_ids=cited_artifact_ids,
        actor=actor,
        workspace_id=workspace_id,
        cache=cache,
    )


def ask_evidence_visible(
    session: Session,
    *,
    context_refs: Iterable[tuple[UUID, UUID | None]],
    artifact_ids: Iterable[UUID],
    actor: ActorContext | None,
    workspace_id: UUID,
    cache: dict[tuple[str, UUID, UUID | None], bool] | None = None,
) -> bool:
    """Whether ``actor`` can see every source an Ask answer was built from.

    Used to re-validate an Ask turn's stored answer/citations/trace (and any
    ingress proposal cloned from it) against the CURRENT actor. A DANGLING
    citation (the cited page/artifact no longer exists) fails CLOSED: the
    stored answer text was composed from that evidence and we can no longer
    confirm the current actor was ever allowed to see it, so the turn must
    redact rather than replay. Every cited document must also belong to the
    turn's own ``workspace_id`` — a malformed/migrated citation resolving to a
    visible entity in another workspace must not authorize replay (Codex R12).

    ``cache`` lets one read of a session (one actor) check each source once:
    later turns repeat their history's sources.
    """

    cache = {} if cache is None else cache
    # Read once: the sources are walked twice below.
    library_ids = set(artifact_ids)
    for page_id, version_id in context_refs:
        key = ("context", page_id, version_id)
        if key not in cache:
            cache[key] = _context_ref_visible(
                session, page_id, version_id, actor, workspace_id
            )
        if not cache[key]:
            return False
    unchecked = [
        artifact_id
        for artifact_id in library_ids
        if ("library", artifact_id, None) not in cache
    ]
    for start in range(0, len(unchecked), _VISIBILITY_BATCH):
        batch = unchecked[start : start + _VISIBILITY_BATCH]
        visible = _library_artifacts_visible(session, batch, actor, workspace_id)
        for artifact_id in batch:
            cache[("library", artifact_id, None)] = artifact_id in visible
    return all(cache[("library", artifact_id, None)] for artifact_id in library_ids)


def _context_ref_visible(
    session: Session,
    page_id: UUID,
    version_id: UUID | None,
    actor: ActorContext | None,
    workspace_id: UUID,
) -> bool:
    page = get_in_workspace(
        session,
        ContextPage,
        workspace_id=workspace_id,
        resource_id=page_id,
    )
    if not context_page_visible(page, actor, workspace_id=workspace_id):
        return False
    if version_id is None:
        return True
    # The answer used this version's text. A version later closed (an undone
    # approval) is not readable just because the page still is.
    version = get_in_workspace(
        session,
        ContextPageVersion,
        workspace_id=workspace_id,
        resource_id=version_id,
    )
    return (
        version is not None
        and version.page_id == page_id
        and context_version_is_visible(version, actor)
    )


def _library_artifacts_visible(
    session: Session,
    artifact_ids: list[UUID],
    actor: ActorContext | None,
    workspace_id: UUID,
) -> set[UUID]:
    """The given artifacts ``actor`` may read; a missing one is not in it."""

    if not artifact_ids:
        return set()
    rows = session.execute(
        select(
            LibraryArtifact.id,
            LibraryItem,
        )
        .join(
            LibraryItemVersion,
            LibraryItemVersion.library_item_id == LibraryItem.id,
        )
        .join(
            LibraryArtifact,
            LibraryArtifact.library_item_version_id == LibraryItemVersion.id,
        )
        .where(
            LibraryArtifact.id.in_(artifact_ids),
            LibraryItem.workspace_id == workspace_id,
        )
    ).all()
    # The stored answer paraphrases this evidence. If its source was
    # archived/hidden or the artifact left the canonical ready attempt, the
    # content must not replay — fail closed (Codex round-11).
    return {
        artifact_id
        for artifact_id, item in rows
        if item.lifecycle_status not in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
        and canonical_library_artifact_belongs_to_item(
            session,
            item=item,
            artifact_id=artifact_id,
        )
        and actor_can_read_library_trust_level(item.trust_level, actor)
    }


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


def _restricted_answer_placeholder(language: str) -> str:
    return (
        "이 답변은 현재 접근 권한이 없는 근거를 인용하고 있어 표시할 수 없습니다."
        if language == "ko"
        else "This answer cites evidence you do not have access to and is hidden."
    )


def _workspace_answer_language(session: Session, workspace_id: UUID) -> str:
    getter = getattr(session, "get", None)
    workspace = getter(Workspace, workspace_id) if callable(getter) else None
    language = str(getattr(workspace, "default_language", "en") or "en").lower()
    return "ko" if language.startswith("ko") else "en"


def ensure_ask_root(
    session: Session,
    workspace_id: UUID,
    *,
    actor_id: str,
) -> WorkspaceTreeNode:
    workspace = session.scalar(
        select(Workspace).where(Workspace.id == workspace_id).with_for_update()
    )
    if workspace is None:
        raise ReadNotFoundError("workspace not found")

    root = session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.section == "ask",
            WorkspaceTreeNode.slug == "ask",
            WorkspaceTreeNode.parent_id.is_(None),
        )
    )
    if root is None:
        root = WorkspaceTreeNode(
            workspace_id=workspace_id,
            section="ask",
            node_kind="folder",
            parent_id=None,
            sort_key="0004",
            title="Ask",
            slug="ask",
            is_system_node=True,
            can_have_children=True,
            primary_placement=True,
            visibility_status="visible",
            created_by_actor_type="system",
            created_by_actor_id=parse_uuid_or_none(actor_id),
        )
        session.add(root)
        session.flush([root])

    policy = session.get(TreePolicy, (workspace_id, "ask"))
    if policy is None:
        policy = TreePolicy(
            workspace_id=workspace_id,
            section="ask",
            allow_user_folder_create=False,
            allow_page_as_parent=False,
            fixed_root_node_ids=[str(root.id)],
        )
        session.add(policy)
    else:
        policy.fixed_root_node_ids = [str(root.id)]
    return root


def ensure_folder(
    session: Session,
    workspace_id: UUID,
    parent: WorkspaceTreeNode,
    slug: str,
    title: str,
    sort_key: str,
    actor_kind: str,
    actor_id: str,
) -> WorkspaceTreeNode:
    node = session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.parent_id == parent.id,
            WorkspaceTreeNode.slug == slug,
            WorkspaceTreeNode.visibility_status == "visible",
        )
    )
    if node is not None:
        return node
    return create_tree_node(
        session,
        workspace_id=workspace_id,
        section="ask",
        node_kind="folder",
        parent_id=parent.id,
        title=title,
        slug=slug,
        sort_key=sort_key,
        actor_kind=actor_kind,
        actor_id=actor_id,
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


def _default_ask_title(at: datetime) -> str:
    # Include seconds (L3): two sessions created in the same minute would
    # otherwise share an identical "New Ask YYYY-MM-DD HH:MM" title until the
    # first turn refreshes it to a context-derived label.
    return f"New Ask {at:%Y-%m-%d %H:%M:%S}"


def _dominant_context_anchor(
    session: Session,
    ask_session: AskSession,
    min_turns: int = 2,
) -> str | None:
    rows = session.execute(
        select(AskTurnCitation.target_id, AskTurn.turn_no)
        .join(AskTurn, AskTurnCitation.ask_turn_id == AskTurn.id)
        .where(AskTurn.ask_session_id == ask_session.id)
        .where(AskTurnCitation.target_kind == "context_page")
        .order_by(AskTurn.turn_no.asc())
    ).all()
    cited_per_turn: Counter[UUID] = Counter()
    first_seen: dict[UUID, int] = {}
    seen_pairs: set[tuple[UUID, int]] = set()
    for page_id, turn_no in rows:
        if (page_id, turn_no) in seen_pairs:
            continue
        seen_pairs.add((page_id, turn_no))
        cited_per_turn[page_id] += 1
        first_seen.setdefault(page_id, turn_no)
    eligible = [
        (page_id, count)
        for page_id, count in cited_per_turn.items()
        if count >= min_turns
    ]
    if not eligible:
        return _dominant_topic_tag(ask_session.default_topic_tags)
    dominant_id, _ = sorted(
        eligible,
        key=lambda item: (-item[1], first_seen.get(item[0], 0), str(item[0])),
    )[0]
    page = get_in_workspace(
        session,
        ContextPage,
        workspace_id=ask_session.workspace_id,
        resource_id=dominant_id,
    )
    if page is None:
        return _dominant_topic_tag(ask_session.default_topic_tags)
    # H-01: the session title is returned to every workspace member by
    # list_sessions, so it must not embed a draft-only page's title (which a
    # viewer cannot read). Only a published page's title is safe as the anchor.
    if page.current_published_version_id is None:
        return _dominant_topic_tag(ask_session.default_topic_tags)
    return page.title


def _turn_citations_by_turn(
    session: Session,
    turn_ids: list[UUID],
) -> dict[UUID, tuple[list[UUID], list[UUID]]]:
    """Rank-ordered (context_page_ids, library_artifact_ids) per turn."""

    if not turn_ids:
        return {}
    rows = session.execute(
        select(
            AskTurnCitation.ask_turn_id,
            AskTurnCitation.target_kind,
            AskTurnCitation.target_id,
        )
        .where(AskTurnCitation.ask_turn_id.in_(turn_ids))
        .order_by(AskTurnCitation.ask_turn_id, AskTurnCitation.rank)
    ).all()
    citations: dict[UUID, tuple[list[UUID], list[UUID]]] = {}
    for turn_id, kind, target_id in rows:
        pages, artifacts = citations.setdefault(turn_id, ([], []))
        if kind == "context_page":
            pages.append(target_id)
        elif kind == "library_artifact":
            artifacts.append(target_id)
    return citations


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


def _dominant_topic_tag(topic_tags: list[str]) -> str | None:
    for tag in topic_tags:
        normalized = tag.strip()
        if normalized:
            return normalized
    return None


def _sync_ask_tree_node_title(session: Session, ask_session: AskSession) -> None:
    node = get_in_workspace(
        session,
        WorkspaceTreeNode,
        workspace_id=ask_session.workspace_id,
        resource_id=ask_session.placement_tree_node_id,
    )
    if node is None:
        return
    node.title = ask_session.title


def date_buckets(sessions: list[AskSession]) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for ask_session in sessions:
        key = ask_session.updated_at.date().isoformat()
        counts[key] = counts.get(key, 0) + 1
    return [
        {"date": date_value, "session_count": count}
        for date_value, count in sorted(counts.items(), reverse=True)
    ]
