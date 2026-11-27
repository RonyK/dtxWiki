from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from backend.domain.input_policy import (
    InputPolicyError,
    normalize_ask_user_message,
)
from backend.models.workspace import Workspace
from backend.services.search_service import KEYWORD_LANE_UNAVAILABLE_WARNING
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
# Earlier turns shown to the model with a new question. Kept small: models
# lose facts in long inputs, and each earlier answer already used its own pack.
ASK_HISTORY_MAX_PAIRS = 4
# The model's input limit is not known here, so earlier turns get a fixed,
# conservative share.
ASK_HISTORY_MAX_CHARS = 16_000
# Why a turn ended as failed. Only these codes reach clients.
ASK_TURN_INTERNAL_ERROR = "internal_error"
ASK_TURN_SERVER_RESTARTED = "server_restarted"
ASK_MODEL_ERROR_CODES = frozenset(
    {
        LLM_PROVIDER_UNAVAILABLE_WARNING,
        LLM_COMPOSER_RETRYABLE_WARNING,
        LLM_ANSWER_INCOMPLETE_WARNING,
        LLM_ANSWER_REFUSED_WARNING,
    }
)


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


class AskGenerationError(RuntimeError):
    """A selected model failed. The original question may be searched again."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.error_code = code


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


def _workspace_answer_language(session: Session, workspace_id: UUID) -> str:
    getter = getattr(session, "get", None)
    workspace = getter(Workspace, workspace_id) if callable(getter) else None
    language = str(getattr(workspace, "default_language", "en") or "en").lower()
    return "ko" if language.startswith("ko") else "en"
