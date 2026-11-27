"""Optional streaming operations for the built-in text-generation adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from backend.spi.llm import (
    CanonicalTextRequest,
    CanonicalTextResult,
    ExecutionContext,
    TextGenerationPort,
)

_OPENAI_RESPONSES = "builtin.openai.responses"
_OPENAI_BASE_URL = "https://api.openai.com/v1"
# Published models in the GPT-6 family. Aliases outside this catalog and
# compatible gateways use the Ask application's next-turn behavior.
_STEER_MODELS = frozenset(
    {
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-6.1-sol",
        "gpt-6.1-terra",
        "gpt-6.2-astra",
        "gpt-6.2-sol",
    }
)


def supports_native_steer(adapter_id: str, base_url: str, model_id: str) -> bool:
    return (
        adapter_id == _OPENAI_RESPONSES
        and base_url == _OPENAI_BASE_URL
        and model_id in _STEER_MODELS
    )


@dataclass(frozen=True, slots=True)
class SteerInput:
    id: str
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SteerUpdate:
    id: str
    status: Literal["sent", "accepted", "applied", "failed", "unknown"]
    error_code: str | None = None


OnDelta = Callable[[str], Awaitable[None]]
NextSteer = Callable[[], Awaitable[SteerInput]]
OnSteer = Callable[[SteerUpdate], Awaitable[None]]


async def generate_stream(
    port: TextGenerationPort,
    request: CanonicalTextRequest,
    context: ExecutionContext,
    *,
    on_delta: OnDelta,
    next_steer: NextSteer | None = None,
    on_steer: OnSteer | None = None,
) -> CanonicalTextResult:
    """Generate once, forwarding genuine text deltas when the port supports them.

    ``next_steer`` peeks at the application's queued input. It must leave that
    input queued until ``on_steer`` receives ``sent``. A response can finish
    before the provider transport claims the input.
    """

    stream = getattr(port, "stream", None)
    if callable(stream):
        return await stream(
            request,
            context,
            on_delta=on_delta,
            next_steer=next_steer,
            on_steer=on_steer,
        )
    return await port.generate(request, context)
