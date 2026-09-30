"""Unit tests for ``Settings.mcp_http_mode`` (Epic_0 / Sprint_5 / Phase_2 / Task 2.1).

Pins the contract defined in
``doc/design/mcp-transport-contract.md`` §2
(mode 분기 메커니즘). The field is the env-driven switch that selects the
``/mcp`` transport mode in subsequent Phase_2 tasks.

Negative-test rule (ADR A6 / Sprint_4 Fix-7): the invalid-value case is
paired with explicit positive controls (``streamable``, ``auto``) and pins
the failure mode via ``pydantic.ValidationError`` rather than a bare
``assert raises``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.settings import Settings


def test_default_is_streamable():
    """Default = ``streamable`` for the Sprint-6 MCP HTTP baseline."""
    settings = Settings()

    assert settings.mcp_http_mode == "streamable"


def test_invalid_value_rejected():
    """Invalid mode values fail pydantic ``Literal`` validation at construction.

    Phase_2 doc §2.1: "잘못된 값은 pydantic ``Literal`` validation 에서
    ``RuntimeError`` (Task 2.1 의 ``test_invalid_value_rejected``)".
    pydantic surfaces this as ``ValidationError`` at ``Settings(...)`` time;
    callers that wrap startup config-loading turn that into ``RuntimeError``.
    """
    with pytest.raises(ValidationError) as exc_info:
        Settings(mcp_http_mode="invalid")

    # Pin the failure-mode keyword so a future schema change (e.g. switching
    # to a free-form ``str`` field) does not silently hide the regression.
    assert "mcp_http_mode" in str(exc_info.value)


def test_streamable_value_accepted():
    """Positive control (ADR A6): ``streamable`` is a valid Literal member."""
    settings = Settings(mcp_http_mode="streamable")

    assert settings.mcp_http_mode == "streamable"


def test_auto_value_accepted():
    """Positive control (ADR A6): ``auto`` is a valid Literal member."""
    settings = Settings(mcp_http_mode="auto")

    assert settings.mcp_http_mode == "auto"


def test_mcp_http_mode_env_backed(monkeypatch):
    """``DTXWIKI_MCP_HTTP_MODE`` env var feeds the field via ``env_prefix``.

    Phase_2 doc §2.1: "``env_prefix='DTXWIKI_'`` 적용 → 실제 env 변수 =
    ``DTXWIKI_MCP_HTTP_MODE``". This is a positive control for the env-binding
    path (the field must be reachable via the documented env name, not only
    via constructor kwargs).
    """
    monkeypatch.setenv("DTXWIKI_MCP_HTTP_MODE", "streamable")

    settings = Settings()

    assert settings.mcp_http_mode == "streamable"
