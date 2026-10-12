"""Validate default, accepted values and environment binding for MCP HTTP mode."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.settings import Settings


def test_default_is_streamable():
    """The default HTTP mode is ``streamable``."""
    settings = Settings()

    assert settings.mcp_http_mode == "streamable"


def test_invalid_value_rejected():
    """Invalid mode values fail pydantic ``Literal`` validation at construction."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(mcp_http_mode="invalid")

    # Pin the failure-mode keyword so a future schema change (e.g. switching
    # to a free-form ``str`` field) does not silently hide the regression.
    assert "mcp_http_mode" in str(exc_info.value)


def test_streamable_value_accepted():
    """``streamable`` is a valid Literal member."""
    settings = Settings(mcp_http_mode="streamable")

    assert settings.mcp_http_mode == "streamable"


def test_auto_value_accepted():
    """``auto`` is a valid Literal member."""
    settings = Settings(mcp_http_mode="auto")

    assert settings.mcp_http_mode == "auto"


def test_mcp_http_mode_env_backed(monkeypatch):
    """``DTXWIKI_MCP_HTTP_MODE`` binds the field through ``env_prefix``."""
    monkeypatch.setenv("DTXWIKI_MCP_HTTP_MODE", "streamable")

    settings = Settings()

    assert settings.mcp_http_mode == "streamable"
