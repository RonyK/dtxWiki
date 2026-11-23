"""Built-in LLM adapters shipped with dtxWiki.

They are ordinary registrations: Core treats them exactly like plugin
adapters. Importing this package registers nothing; composition does.
"""

from backend.llm.adapters import (
    anthropic_messages,
    chat_completions,
    gemini_generate_content,
    openai_responses,
)
from backend.spi.llm import LLMAdapterFactory


def build_builtin_llm_adapters() -> tuple[LLMAdapterFactory, ...]:
    return (
        anthropic_messages.FACTORY,
        openai_responses.FACTORY,
        chat_completions.OPENAI_FACTORY,
        chat_completions.COMPATIBLE_FACTORY,
        gemini_generate_content.FACTORY,
    )


__all__ = ["build_builtin_llm_adapters"]
