"""The server-wide answer model selected by ``DTXWIKI_LLM_PROVIDER``.

This is the owner-only Ask's instance configuration. It maps each accepted
value to a built-in adapter with a fixed model and temperature 0. A failed call
is not sent again: the provider may already have processed (and charged for)
it, so Ask answers from the evidence pack and says so instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from backend.llm.generation import BlockingTextGenerator, TextGenerator
from backend.llm.transport import HttpxLLMTransport
from backend.spi.llm import (
    ExecutionContext,
    LLMModelCapabilities,
    LLMTransport,
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
        port=port,
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


def llm_provider_target(
    settings: Settings, adapters: LLMAdapterRegistry
) -> dict[str, str] | None:
    """Identify the server's answer model for Ask's conversation history.

    An earlier answer is shown only to the same adapter, model and endpoint,
    so changing the server's model starts a new history instead of sending
    the conversation somewhere else.
    """

    answer_model = _ANSWER_MODELS.get(settings.llm_provider.strip().lower())
    if answer_model is None:
        return None
    return {
        "kind": "server",
        "adapter_id": answer_model.adapter_id,
        "model": answer_model.model,
        "endpoint": adapters.descriptor(answer_model.adapter_id).default_base_url or "",
    }


__all__ = [
    "build_llm_provider",
    "llm_provider_target",
    "resolve_llm_provider",
]
