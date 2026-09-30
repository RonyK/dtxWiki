import os

import pytest

from backend.llm import AnthropicLLM, LLMMessage, LLMRequest, OpenAILLM

RUN_REAL_LLM_TESTS = os.getenv("DTXWIKI_RUN_REAL_LLM_TESTS") == "1"


@pytest.mark.skipif(
    not RUN_REAL_LLM_TESTS
    or not (os.getenv("DTXWIKI_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")),
    reason="requires DTXWIKI_RUN_REAL_LLM_TESTS=1 and Anthropic API key",
)
def test_anthropic_real_key_smoke():
    provider = AnthropicLLM(
        api_key=os.getenv("DTXWIKI_ANTHROPIC_API_KEY")
        or os.environ["ANTHROPIC_API_KEY"],
    )

    response = provider.chat(
        LLMRequest(
            messages=[LLMMessage(role="user", content="Reply with one word: ok")],
            max_tokens=8,
        )
    )

    assert response.provider == "anthropic"
    assert response.content


@pytest.mark.skipif(
    not RUN_REAL_LLM_TESTS
    or not (os.getenv("DTXWIKI_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")),
    reason="requires DTXWIKI_RUN_REAL_LLM_TESTS=1 and OpenAI API key",
)
def test_openai_real_key_smoke():
    provider = OpenAILLM(
        api_key=os.getenv("DTXWIKI_OPENAI_API_KEY") or os.environ["OPENAI_API_KEY"],
    )

    response = provider.chat(
        LLMRequest(
            messages=[LLMMessage(role="user", content="Reply with one word: ok")],
            max_tokens=8,
        )
    )

    assert response.provider == "openai"
    assert response.content
