"""A model ready to answer one question."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from backend.spi.llm import (
    CanonicalTextRequest,
    CanonicalTextResult,
    ExecutionContext,
    GenerationFeatures,
    NextSteer,
    OnDelta,
    OnSteer,
    SteerableTextPort,
    StreamingTextPort,
    TextGenerationPort,
)


@dataclass(frozen=True)
class ModelBinding:
    """A port bound to one model, with an identity safe to store with history.

    The server's default model and every LLM connection reach Ask as one of
    these, so every answer takes the same path; how live it can be is what
    the port's features offer.
    """

    port: TextGenerationPort = field(repr=False)
    target: dict[str, str]
    model: str
    max_output_tokens: int
    context_window: int | None
    temperature: float | None = None

    @property
    def features(self) -> GenerationFeatures:
        return self.port.features

    async def generate(
        self,
        request: CanonicalTextRequest,
        context: ExecutionContext,
        *,
        on_delta: OnDelta,
        next_steer: NextSteer | None = None,
        on_steer: OnSteer | None = None,
    ) -> CanonicalTextResult:
        """Generate once, as live as the port's features allow.

        Instructions are taken only when the port steers and both steering
        callbacks are given; text reaches ``on_delta`` only when it streams.
        """

        if self.features.steering and next_steer and on_steer:
            return await cast(SteerableTextPort, self.port).stream_with_steering(
                request,
                context,
                on_delta=on_delta,
                next_steer=next_steer,
                on_steer=on_steer,
            )
        if self.features.streaming:
            return await cast(StreamingTextPort, self.port).stream(
                request, context, on_delta=on_delta
            )
        return await self.port.generate(request, context)
