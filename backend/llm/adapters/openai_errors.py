"""Error-body codes shared by the OpenAI-format APIs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.spi.llm import LLMErrorCode

# Spend and billing limits arrive as 429, but waiting does not lift them.
_ERROR_CODES: Mapping[str, LLMErrorCode] = {
    "insufficient_quota": "quota_exhausted",
    "credit_balance_exhausted": "quota_exhausted",
    "organization_spend_limit_exceeded": "quota_exhausted",
    "project_spend_limit_exceeded": "quota_exhausted",
    "organization_usage_limit_exceeded": "quota_exhausted",
    "rate_limit_exceeded": "rate_limited",
    "slow_down": "rate_limited",
    "model_not_found": "not_found",
    "invalid_prompt": "invalid_request",
    "invalid_api_key": "authentication_failed",
    "server_error": "provider_unavailable",
}
_ERROR_TYPES: Mapping[str, LLMErrorCode] = {
    "insufficient_quota": "quota_exhausted",
}


def refine_openai_error(error: Mapping[str, Any]) -> LLMErrorCode | None:
    for field_name, table in (("code", _ERROR_CODES), ("type", _ERROR_TYPES)):
        value = error.get(field_name)
        if isinstance(value, str) and value in table:
            return table[value]
    return None
