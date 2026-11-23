"""The server-wide answer model selected by ``DTXWIKI_LLM_PROVIDER``.

This is the owner-only Ask's instance configuration. It maps each accepted
value to a built-in adapter and keeps what that path has always done: a fixed
model per value, temperature 0 and up to two retries of transient failures.
The retries live here, around the adapter, because an adapter makes exactly
one call per request.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from backend.llm.generation import BlockingTextGenerator, TextGenerator
from backend.llm.transport import HttpxLLMTransport
from backend.spi.llm import (
    CanonicalTextRequest,
    CanonicalTextResult,
    ExecutionContext,
    LLMAdapterError,
    LLMModelCapabilities,
    LLMTransport,
    TextGenerationPort,
)

if TYPE_CHECKING:
    from backend.app.settings import Settings
    from backend.llm.adapter_registry import LLMAdapterRegistry


@dataclass(frozen=True)
class _AnswerModel:
    adapter_id: str
    api_key_setting: str
    model: str


_ANSWER_MODELS = MappingProxyType(
    {
        "anthropic": _AnswerModel(
            adapter_id="builtin.anthropic.messages",
            api_key_setting="anthropic_api_key",
            model="claude-sonnet-4-6",
        ),
        "openai": _AnswerModel(
            adapter_id="builtin.openai.chat_completions",
            api_key_setting="openai_api_key",
            model="gpt-4o-mini",
        ),
    }
)
_CAPABILITIES = LLMModelCapabilities(options=frozenset({"temperature"}))
_TEMPERATURE = 0.0
_TIMEOUT_SECONDS = 120.0

# HTTP error statuses this path has always retried.
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504, 529})
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BASE_DELAY = 0.5
DEFAULT_RETRY_MAX_DELAY = 8.0


class RetryingTextGeneration:
    """Retries a port on transient failures with jittered backoff."""

    def __init__(
        self,
        port: TextGenerationPort,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay: float = DEFAULT_RETRY_BASE_DELAY,
        max_delay: float = DEFAULT_RETRY_MAX_DELAY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._port = port
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self._jitter = jitter

    async def generate(
        self, request: CanonicalTextRequest, context: ExecutionContext
    ) -> CanonicalTextResult:
        attempt = 0
        while True:
            try:
                return await self._port.generate(request, context)
            except LLMAdapterError as exc:
                if attempt >= self._max_retries or not _retryable(exc):
                    raise
                await self._sleep(self._delay(attempt, exc.retry_after_seconds))
                attempt += 1

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        delay = min(self._max_delay, self._base_delay * (2**attempt))
        delay *= 0.5 + self._jitter() / 2
        if retry_after is not None:
            delay = max(delay, min(retry_after, self._max_delay))
        return delay


def _retryable(error: LLMAdapterError) -> bool:
    status_code = error.status_code
    # A successful status confirms delivery even if the body read failed.
    if status_code is None or 200 <= status_code < 300:
        return error.code in ("timeout", "network_error")
    return status_code in RETRYABLE_STATUS_CODES


def build_llm_provider(
    settings: Settings,
    adapters: LLMAdapterRegistry,
    *,
    transport: LLMTransport | None = None,
) -> TextGenerator | None:
    selection = settings.llm_provider.strip().lower()
    if selection in {"", "none"}:
        return None
    answer_model = _ANSWER_MODELS.get(selection)
    if answer_model is None:
        raise RuntimeError(
            "DTXWIKI_LLM_PROVIDER must be one of: none, " + ", ".join(_ANSWER_MODELS)
        )
    api_key = getattr(settings, answer_model.api_key_setting) or ""
    if not api_key:
        raise ValueError(
            f"DTXWIKI_LLM_PROVIDER={selection} requires an API key in "
            f"DTXWIKI_{answer_model.api_key_setting.upper()}"
        )
    port = adapters.create(
        answer_model.adapter_id,
        base_url=None,
        config={},
        credential={"api_key": api_key},
        model=answer_model.model,
        capabilities=_CAPABILITIES,
        transport=transport or HttpxLLMTransport(),
    )
    return BlockingTextGenerator(
        port=RetryingTextGeneration(port),
        model=answer_model.model,
        context=ExecutionContext(timeout_seconds=_TIMEOUT_SECONDS),
        temperature=_TEMPERATURE,
    )


def resolve_llm_provider(
    settings: Settings, adapters: LLMAdapterRegistry
) -> tuple[TextGenerator | None, str | None]:
    """Build the answer model, turning a misconfiguration into a reason string.

    Ask composes answers with the model when one is available and falls back
    to the deterministic evidence pack when it is not, so a bad setting must
    not take the whole Ask surface down. ``build_llm_provider`` raises so that
    setup/CLI paths still fail loudly; this wrapper is what the request path
    uses, and the returned reason is surfaced to the user as a warning.
    """

    try:
        return build_llm_provider(settings, adapters), None
    except (ValueError, RuntimeError) as exc:
        return None, str(exc)


__all__ = [
    "RETRYABLE_STATUS_CODES",
    "RetryingTextGeneration",
    "build_llm_provider",
    "resolve_llm_provider",
]
