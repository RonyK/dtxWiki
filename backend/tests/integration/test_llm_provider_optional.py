import asyncio
import os

import pytest

from backend.llm.adapter_registry import LLMAdapterRegistry
from backend.llm.adapters import build_builtin_llm_adapters
from backend.llm.transport import HttpxLLMTransport
from backend.spi.llm import (
    CanonicalTextRequest,
    ExecutionContext,
    LLMModelCapabilities,
    TextMessage,
)

RUN_REAL_LLM_TESTS = os.getenv("DTXWIKI_RUN_REAL_LLM_TESTS") == "1"


def _key(*names: str) -> str | None:
    return next((os.environ[name] for name in names if os.getenv(name)), None)


ANTHROPIC_KEYS = ("DTXWIKI_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
OPENAI_KEYS = ("DTXWIKI_OPENAI_API_KEY", "OPENAI_API_KEY")


# Opt-in only: each case spends a few tokens on a real provider account. The
# Chat Completions case is the wire of DTXWIKI_LLM_PROVIDER=openai.
@pytest.mark.parametrize(
    ("adapter_id", "model", "key_names"),
    [
        (
            "builtin.anthropic.messages",
            "claude-sonnet-4-6",
            ANTHROPIC_KEYS,
        ),
        (
            "builtin.openai.responses",
            "gpt-4o-mini",
            OPENAI_KEYS,
        ),
        (
            "builtin.openai.chat_completions",
            "gpt-4o-mini",
            OPENAI_KEYS,
        ),
        (
            "builtin.google.gemini_generate_content",
            os.getenv("DTXWIKI_GEMINI_TEST_MODEL", "gemini-2.5-flash"),
            ("GEMINI_API_KEY",),
        ),
    ],
    ids=["anthropic", "openai-responses", "openai-chat", "gemini"],
)
def test_builtin_adapter_real_key_smoke(adapter_id, model, key_names):
    key = _key(*key_names)
    if not RUN_REAL_LLM_TESTS or not key:
        pytest.skip("requires DTXWIKI_RUN_REAL_LLM_TESTS=1 and the provider API key")
    port = LLMAdapterRegistry(build_builtin_llm_adapters()).create(
        adapter_id,
        base_url=None,
        config={},
        credential={"api_key": key},
        model=model,
        capabilities=LLMModelCapabilities(),
        transport=HttpxLLMTransport(),
    )

    result = asyncio.run(
        port.generate(
            CanonicalTextRequest(
                model=model,
                messages=(TextMessage(role="user", text="Reply with one word: ok"),),
                max_output_tokens=64,
            ),
            ExecutionContext(timeout_seconds=60),
        )
    )

    assert result.termination == "completed"
    assert result.text.strip()
    assert result.usage.input_tokens and result.usage.output_tokens
