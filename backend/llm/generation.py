"""Synchronous text generation for request-thread code."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from backend.spi.llm import (
    CanonicalTextRequest,
    CanonicalTextResult,
    ExecutionContext,
    LLMModelCapabilities,
    TextGenerationPort,
    TextMessage,
)


class TextGenerator(Protocol):
    """A model bound to one connection, as the Ask composer uses it."""

    def generate(
        self,
        *,
        system: str | None,
        messages: Sequence[TextMessage],
        max_output_tokens: int,
    ) -> CanonicalTextResult: ...


@dataclass(frozen=True)
class BlockingTextGenerator:
    """Runs one adapter for one model from synchronous code.

    Each call runs on its own event loop, so it must come from a thread with
    no running loop, such as the threadpool behind a sync FastAPI route.
    """

    port: TextGenerationPort
    model: str
    capabilities: LLMModelCapabilities
    context: ExecutionContext
    temperature: float | None = None

    def generate(
        self,
        *,
        system: str | None,
        messages: Sequence[TextMessage],
        max_output_tokens: int,
    ) -> CanonicalTextResult:
        request = CanonicalTextRequest(
            model=self.model,
            messages=tuple(messages),
            max_output_tokens=max_output_tokens,
            system=system,
            temperature=self.temperature,
        )
        self.capabilities.require_supported(request)
        return asyncio.run(self.port.generate(request, self.context))


__all__ = ["BlockingTextGenerator", "TextGenerator"]
