"""The server-wide default answer model selected by ``DTXWIKI_LLM_PROVIDER``.

Each accepted value maps to a built-in adapter with a fixed model and
temperature 0. Ask runs it like any LLM connection (``ModelBinding``).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from backend.llm.binding import ModelBinding
from backend.llm.transport import HttpxLLMTransport
from backend.spi.llm import LLMModelCapabilities, LLMTransport

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
_MAX_OUTPUT_TOKENS = 1024


def build_default_binding(
    settings: Settings,
    adapters: LLMAdapterRegistry,
    *,
    transport: LLMTransport | None = None,
) -> ModelBinding | None:
    """The configured default model, or None; raises on a misconfiguration."""

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
    return ModelBinding(
        port=port,
        # An earlier answer is shown only to the same adapter, model and
        # endpoint, so changing the default model starts a new history.
        target={
            "kind": "server",
            "adapter_id": answer_model.adapter_id,
            "model": answer_model.model,
            "endpoint": adapters.descriptor(answer_model.adapter_id).default_base_url
            or "",
        },
        model=answer_model.model,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        context_window=None,
        steer=False,
        temperature=_TEMPERATURE,
    )


def resolve_default_binding(
    settings: Settings, adapters: LLMAdapterRegistry
) -> tuple[ModelBinding | None, str | None]:
    """Build the default model, turning a misconfiguration into a reason.

    A bad setting must not take Ask down: without the default model Ask answers
    with the evidence summary, and the reason is surfaced as a warning.
    """

    try:
        return build_default_binding(settings, adapters), None
    except (ValueError, RuntimeError) as exc:
        return None, str(exc)


__all__ = ["build_default_binding", "resolve_default_binding"]
