import logging
import re
from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from backend.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMRequest,
)
from backend.models.ask import (
    AskSession,
    AskTurn,
    AskTurnCitation,
    ContextIngressProposal,
)
from backend.models.context import ContextPage
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.tree import TreePolicy, WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.ask import (
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
)
from backend.services.audit_service import AuditService
from backend.services.coverage_service import record_coverage_gap, resolve_coverage_gap
from backend.services.db_write_intent import acquire_write_intent
from backend.services.graph_service import GraphService
from backend.services.library_canonical import (
    canonical_library_artifact_belongs_to_item,
    get_canonical_library_version,
    get_canonical_markdown_artifact,
)
from backend.services.list_pagination import (
    _decode_keyset_cursor,
    _encode_keyset_cursor,
)
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
from backend.services.read_errors import ReadNotFoundError
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
from backend.utils.uuid import parse_uuid_or_none

USER_MESSAGE_MAX_CHARS = 8000
ASK_TITLE_MAX_WORDS = 8
# Search-lane advisory codes that are meaningful on the Ask surface too: the
# keyword-lane degradation and the restricted-Library exclusion notice are both
# trust disclosures about the evidence the answer was built on. Codes whose
# affordance lives only on the search response (e.g. disambiguation candidates)
# are NOT forwarded — they would render an actionless banner on an Ask turn.
ASK_FORWARDABLE_WARNINGS = frozenset(
    {KEYWORD_LANE_UNAVAILABLE_WARNING, "permission_limited"}
)
# Codes Ask raises about its OWN answer pipeline rather than about a search
# lane. They travel back on the same list so they reach the turn unfiltered.
LLM_PROVIDER_UNAVAILABLE_WARNING = "llm_provider_unavailable"
LLM_COMPOSER_FAILED_WARNING = "llm_composer_failed"
# The workspace asked for a retrieval mode this deployment cannot serve. Already
# registered in WARNING_CODES with no producer -- reserved for exactly this.
UNSUPPORTED_RETRIEVAL_MODE_FALLBACK_WARNING = "unsupported_retrieval_mode_fallback"
ASK_SELF_WARNINGS = frozenset(
    {
        LLM_PROVIDER_UNAVAILABLE_WARNING,
        LLM_COMPOSER_FAILED_WARNING,
        UNSUPPORTED_RETRIEVAL_MODE_FALLBACK_WARNING,
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
    "Answer only from the supplied evidence pack. Cite evidence items by their "
    "list number. If the pack does not support an answer, say that you do not know."
)
logger = logging.getLogger(__name__)


def ask_session_for_turn_insert_statement(ask_session_id: UUID):
    return (
        select(AskSession)
        .where(AskSession.id == ask_session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


class InvalidUserMessageError(ValueError):
    """Raised when user_message fails validation."""


def _validate_user_message(raw: str) -> str:
    if not isinstance(raw, str):
        raise InvalidUserMessageError("user_message must be a string")
    stripped = raw.strip()
    if not stripped:
        raise InvalidUserMessageError("user_message must not be empty")
    if len(stripped) > USER_MESSAGE_MAX_CHARS:
        raise InvalidUserMessageError(
            f"user_message exceeds {USER_MESSAGE_MAX_CHARS} characters"
        )
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
    return snippet[:96]


class AskService:
    def __init__(
        self,
        search_service: SearchService | None = None,
        audit_service: AuditService | None = None,
        tree_read_service: TreeReadService | None = None,
        provider: LLMProvider | None = None,
        graph_service: GraphService | None = None,
        provider_unavailable_reason: str | None = None,
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

    def create_turn(
        self,
        session: Session,
        workspace_id: UUID,
        ask_session_id: UUID,
        request: AskTurnCreate,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> tuple[AskTurnDetail, list[str]]:
        validate_request_inputs(request)
        validated_message = _validate_user_message(request.user_message)
        ask_session = self._require_session(session, workspace_id, ask_session_id)
        (
            answer,
            context_ids,
            artifact_ids,
            trace,
            tried_lanes,
            fallback,
            nearest_candidates,
            search_warnings,
        ) = self._answer(
            session=session,
            workspace_id=workspace_id,
            question=validated_message,
            actor=actor,
        )
        warning_codes = [
            code
            for code in search_warnings
            if code in ASK_FORWARDABLE_WARNINGS or code in ASK_SELF_WARNINGS
        ]
        if not trace:
            if KEYWORD_LANE_UNAVAILABLE_WARNING not in warning_codes:
                gap = record_coverage_gap(
                    session,
                    workspace_id=workspace_id,
                    query=validated_message,
                    surface="ask",
                    tried_lanes=tried_lanes,
                    nearest_candidates=nearest_candidates,
                )
                if gap is not None:
                    warning_codes.append("coverage_gap_recorded")
        elif not context_ids and not artifact_ids:
            warning_codes.append("evidence_not_citable")
        else:
            resolve_coverage_gap(
                session,
                workspace_id=workspace_id,
                query=validated_message,
            )
        # SQLite: FOR UPDATE is a no-op, so take write intent before the locked
        # read and the turn_no allocation below -- the branches above do not all
        # emit DML, and without it two turns in one session both allocate the
        # same turn_no and collide on the (ask_session_id, turn_no) unique index.
        # Deliberately here rather than at the top of the transaction: the
        # retrieval and answer composition above can take seconds, and SQLite's
        # writer lock is database-wide, so acquiring it earlier would stall every
        # other writer for the length of an LLM call.
        acquire_write_intent(session)
        ask_session = self._lock_session_for_turn_insert(
            session=session,
            ask_session_id=ask_session_id,
        )
        if ask_session is None or ask_session.workspace_id != workspace_id:
            raise ReadNotFoundError("ask session not found")
        turn_no = (
            int(
                session.scalar(
                    select(func.max(AskTurn.turn_no)).where(
                        AskTurn.ask_session_id == ask_session.id
                    )
                )
                or 0
            )
            + 1
        )
        turn = AskTurn(
            workspace_id=ask_session.workspace_id,
            ask_session_id=ask_session.id,
            turn_no=turn_no,
            user_message=validated_message,
            assistant_message=answer,
            topic_tags=ask_session.default_topic_tags,
            recommended_placement_tree_node_id=(
                ask_session.default_context_parent_tree_node_id
            ),
            placement_confidence="medium" if trace else "low",
            retrieval_trace=trace,
            tried_lanes=tried_lanes,
            fallback_disclosure=fallback,
            warning_codes=warning_codes,
        )
        session.add(turn)
        session.flush()
        for kind, cited_ids in (
            ("context_page", context_ids),
            ("library_artifact", artifact_ids),
        ):
            seen: set[UUID] = set()
            for rank, item_id in enumerate(
                item for item in cited_ids if not (item in seen or seen.add(item))
            ):
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

    @staticmethod
    def _lock_session_for_turn_insert(
        session: Session,
        ask_session_id: UUID,
    ) -> AskSession | None:
        return session.scalar(ask_session_for_turn_insert_statement(ask_session_id))

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

    def _answer(
        self,
        session: Session,
        workspace_id: UUID,
        question: str,
        actor: ActorContext | None = None,
    ) -> tuple[
        str,
        list[UUID],
        list[UUID],
        list[dict[str, str]],
        list[str],
        bool,
        list[dict[str, str]],
        list[str],
    ]:
        mode, honored = self._best_retrieval_mode(
            self._workspace_retrieval_mode(session, workspace_id)
        )
        context_payload = self.search_service.search(
            session=session,
            workspace_id=workspace_id,
            query=question,
            scope="context_page",
            retrieval_mode=mode,
            limit=3,
            actor=actor,
        )
        context = context_payload.ranked_results
        tried_lanes = list(context_payload.tried_lanes)
        search_warnings = list(context_payload.warnings)
        if not honored:
            search_warnings = [
                *search_warnings,
                UNSUPPORTED_RETRIEVAL_MODE_FALLBACK_WARNING,
            ]
        fallback = False
        nearest_candidates = list(context_payload.nearest_candidates)
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
        language = _workspace_answer_language(session, workspace_id)
        if not results:
            return (
                (
                    "일치하는 Context 또는 Library 근거를 찾지 못했습니다."
                    if language == "ko"
                    else "No matching Context or Library evidence was found."
                ),
                [],
                [],
                [],
                tried_lanes,
                fallback,
                nearest_candidates,
                search_warnings,
            )
        lines = [
            "컨텍스트 우선 검색 요약:"
            if language == "ko"
            else "Context-first retrieval summary:"
        ]
        context_ids: list[UUID] = []
        artifact_ids: list[UUID] = []
        trace: list[dict[str, str]] = []
        for index, result in enumerate(results, start=1):
            lines.append(f"{index}. {result.title}: {result.snippet}")
            # All search lanes expose canonical document identity. Library item
            # hits still map to the extracted artifact AskTurn cites.
            evidence_id = self._result_document_id(result) or result.id
            if result.type == "context_page":
                page_id = evidence_id
                if page_id is not None and page_id not in context_ids:
                    context_ids.append(page_id)
            elif result.type in ("library_artifact", "library_item"):
                artifact_id = self._library_artifact_id(
                    session,
                    workspace_id=workspace_id,
                    result=result,
                )
                if artifact_id is not None and artifact_id not in artifact_ids:
                    artifact_ids.append(artifact_id)
                    evidence_id = artifact_id
            trace.append(
                {
                    "id": str(evidence_id),
                    "type": result.type,
                    "title": result.title,
                    "score": f"{result.score:.2f}",
                    "match_reason": result.match_reason or "",
                    "concept_path": result.concept_path or "",
                }
            )
        pack_text = "\n".join(lines)
        answer = pack_text
        if self.provider_unavailable_reason:
            # Configured but unbuildable provider: a persistent, operator-owned
            # condition. Disclose it instead of silently serving a different
            # answer style than the workspace is configured for.
            search_warnings = [*search_warnings, LLM_PROVIDER_UNAVAILABLE_WARNING]
        if self.provider is not None:
            try:
                response = self.provider.chat(
                    LLMRequest(
                        messages=[
                            LLMMessage(role="system", content=_COMPOSER_SYSTEM),
                            LLMMessage(
                                role="user",
                                content=(f"{question}\n\n[Evidence pack]\n{pack_text}"),
                            ),
                        ],
                        max_tokens=1024,
                    )
                )
                if not response.truncated and response.content.strip():
                    answer = response.content.strip()
                else:
                    search_warnings = [
                        *search_warnings,
                        LLM_COMPOSER_FAILED_WARNING,
                    ]
            except LLMProviderError:
                # Transient upstream failure (auth, 5xx, timeout). The pack is a
                # valid answer, but the user must know it is not the composed one.
                logger.warning("Ask composer call failed; using deterministic pack")
                search_warnings = [*search_warnings, LLM_COMPOSER_FAILED_WARNING]
            except Exception:
                # Not LLM_COMPOSER_FAILED_WARNING: that one tells the user
                # "retrying may compose a full answer", which is true of an
                # upstream hiccup and never true of a bug in our own composer.
                # An unexpected exception here is the server's problem, so say
                # the thing that is actionable — ask an admin.
                logger.warning(
                    "Ask composer failed unexpectedly; using deterministic pack",
                    exc_info=True,
                )
                search_warnings = [
                    *search_warnings,
                    LLM_PROVIDER_UNAVAILABLE_WARNING,
                ]
        return (
            answer,
            context_ids,
            artifact_ids,
            trace,
            tried_lanes,
            fallback,
            nearest_candidates,
            search_warnings,
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
        item = get_in_workspace(
            session,
            LibraryItem,
            workspace_id=workspace_id,
            resource_id=item_id,
        )
        if item is None:
            return None
        version = get_canonical_library_version(session, item)
        if version is None:
            return None
        artifact = get_canonical_markdown_artifact(session, version)
        return artifact.id if artifact else None

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
        next_title = (f"{anchor}: {intent}" if anchor else intent)[:256]
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
        evidence_visible = not revalidate_evidence or ask_turn_evidence_visible(
            session, cited_page_ids, cited_artifact_ids, actor, ask_session.workspace_id
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
            created_at=turn.created_at,
            tree_path=tree_path,
            retrieval_trace=retrieval_trace,
            tried_lanes=turn.tried_lanes or [],
            fallback_disclosure=turn.fallback_disclosure,
            warning_codes=turn.warning_codes or [],
            graph_context=turn_graph_context,
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


def ask_turn_evidence_visible(
    session: Session,
    cited_page_ids: list[UUID],
    cited_artifact_ids: list[UUID],
    actor: ActorContext | None,
    workspace_id: UUID,
) -> bool:
    """Whether ``actor`` can see every document a turn's answer cites.

    Used to re-validate an Ask turn's stored answer/citations/trace (and any
    ingress proposal cloned from it) against the CURRENT actor. A DANGLING
    citation (the cited page/artifact no longer exists) fails CLOSED: the
    stored answer text was composed from that evidence and we can no longer
    confirm the current actor was ever allowed to see it, so the turn must
    redact rather than replay. Every cited document must also belong to the
    turn's own ``workspace_id`` — a malformed/migrated citation resolving to a
    visible entity in another workspace must not authorize replay (Codex R12).
    """

    for page_id in cited_page_ids:
        page = get_in_workspace(
            session,
            ContextPage,
            workspace_id=workspace_id,
            resource_id=page_id,
        )
        if not context_page_visible(page, actor, workspace_id=workspace_id):
            return False
    if cited_artifact_ids:
        unique_artifact_ids = set(cited_artifact_ids)
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
                LibraryArtifact.id.in_(unique_artifact_ids),
                LibraryItem.workspace_id == workspace_id,
            )
        ).all()
        # A cited artifact that no longer resolves to a live item is dangling.
        if len(rows) < len(unique_artifact_ids):
            return False
        for (
            artifact_id,
            item,
        ) in rows:
            # The stored answer paraphrases this evidence. If its source was
            # archived/hidden or the artifact left the canonical ready attempt, the
            # content must not replay — fail closed (Codex round-11).
            if item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
                return False
            if not canonical_library_artifact_belongs_to_item(
                session,
                item=item,
                artifact_id=artifact_id,
            ):
                return False
            if not actor_can_read_library_trust_level(item.trust_level, actor):
                return False
    return True


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
