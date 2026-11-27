from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any, cast

from backend.llm.binding import ModelBinding
from backend.services.ask_types import (
    LLM_ANSWER_INCOMPLETE_WARNING,
    LLM_ANSWER_REFUSED_WARNING,
    LLM_COMPOSER_RETRYABLE_WARNING,
    LLM_PROVIDER_UNAVAILABLE_WARNING,
    AskEvidence,
    AskGenerationError,
    ComposedTurn,
    PreparedTurn,
    RetrievedEvidence,
)
from backend.spi.llm import (
    CanonicalTextRequest,
    CanonicalTextResult,
    ExecutionContext,
    LLMAdapterError,
    SteerInput,
    SteerUpdate,
    TextMessage,
    TokenUsage,
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
_COMPOSER_SYSTEM = (
    "Answer only from the supplied evidence pack and cite its items by their "
    "list number. Earlier messages in this conversation only tell you what the "
    "question refers to; numbers in them are not citations. If the pack does "
    "not support an answer, say that you do not know. If the question could "
    "mean more than one thing, ask which one is meant."
)
_MODEL_NETWORK_TIMEOUT_SECONDS = 120.0
_MODEL_DEADLINE_SECONDS = 180.0
logger = logging.getLogger(__name__)


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
    binding: ModelBinding,
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


class AskComposer:
    @staticmethod
    def compose_evidence(prepared: PreparedTurn) -> ComposedTurn:
        """Answer with the search result itself, without a model."""
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
        binding: ModelBinding | None,
        *,
        on_delta: Callable[[str], Awaitable[None]],
        next_steer: Callable[[], Awaitable[SteerInput]] | None = None,
        on_steer: Callable[[SteerUpdate], Awaitable[None]] | None = None,
    ) -> ComposedTurn:
        """Ask the model, streaming only provisional text.

        Any failure of the model ends the turn as failed (the asker can search
        again without a model); the default model follows the same rule.
        """
        retrieved = prepared.retrieved
        if binding is None or not retrieved.pack:
            return self.compose_evidence(prepared)
        request = CanonicalTextRequest(
            system=_COMPOSER_SYSTEM,
            model=binding.model,
            messages=_composer_messages(prepared, _pack_text(retrieved)),
            max_output_tokens=binding.max_output_tokens,
            temperature=binding.temperature,
        )
        try:
            async with asyncio.timeout(_MODEL_DEADLINE_SECONDS):
                result = await binding.generate(
                    request,
                    ExecutionContext(timeout_seconds=_MODEL_NETWORK_TIMEOUT_SECONDS),
                    on_delta=_from_caller(on_delta),
                    next_steer=_from_caller(next_steer),
                    on_steer=_from_caller(on_steer),
                )
        except _CallerError as failure:
            # The application's own callback failed, not the model.
            raise failure.error from None
        except TimeoutError:
            raise AskGenerationError(LLM_COMPOSER_RETRYABLE_WARNING) from None
        except LLMAdapterError as exc:
            # The asker sees the normalized warning; the administrator who is
            # asked to check needs the adapter's code.
            logger.warning("Ask model call failed (%s)", exc.code)
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
