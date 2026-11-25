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
    LLMAdapterError,
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
    context: ExecutionContext
    temperature: float | None = None
    # Bounds the whole call. ``context`` bounds each network operation only,
    # so a provider that keeps sending could otherwise hold the caller.
    deadline_seconds: float | None = None

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
        return asyncio.run(self._generate(request))

    async def _generate(self, request: CanonicalTextRequest) -> CanonicalTextResult:
        if self.deadline_seconds is None:
            return await self.port.generate(request, self.context)
        try:
            return await asyncio.wait_for(
                self.port.generate(request, self.context), self.deadline_seconds
            )
        except TimeoutError:
            # The request may have reached the provider; it is not resent.
            raise LLMAdapterError("timeout", delivery="unknown") from None


__all__ = ["BlockingTextGenerator", "TextGenerator"]
