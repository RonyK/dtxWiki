"""A model ready to answer one question."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.spi.llm import TextGenerationPort


@dataclass(frozen=True)
class ModelBinding:
    """A port bound to one model, with an identity safe to store with history.

    The server's default model and every LLM connection reach Ask as one of
    these, so every answer takes the same streaming, cancellable path.
    """

    port: TextGenerationPort = field(repr=False)
    target: dict[str, str]
    model: str
    max_output_tokens: int
    context_window: int | None
    steer: bool
    temperature: float | None = None
