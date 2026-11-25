import logging

import pytest
from pydantic import ValidationError

from backend.app.settings import (
    OAUTH_ACCESS_TOKEN_TTL_MAX_SECONDS,
    OAUTH_ACCESS_TOKEN_TTL_MIN_SECONDS,
    OAUTH_REFRESH_TOKEN_TTL_DEFAULT_SECONDS,
    OAUTH_REFRESH_TOKEN_TTL_MAX_SECONDS,
    OAUTH_REFRESH_TOKEN_TTL_MIN_SECONDS,
    Settings,
    oauth_token_lifetime_report,
)
from backend.tests.conftest import captured_log_messages


@pytest.mark.parametrize(
    ("field_name", "value", "env_key", "minimum", "maximum"),
    [
        (
            "oauth_access_token_ttl_seconds",
            0,
            "DTXWIKI_OAUTH_ACCESS_TOKEN_TTL_SECONDS",
            OAUTH_ACCESS_TOKEN_TTL_MIN_SECONDS,
            OAUTH_ACCESS_TOKEN_TTL_MAX_SECONDS,
        ),
        (
            "oauth_access_token_ttl_seconds",
            OAUTH_ACCESS_TOKEN_TTL_MAX_SECONDS + 1,
            "DTXWIKI_OAUTH_ACCESS_TOKEN_TTL_SECONDS",
            OAUTH_ACCESS_TOKEN_TTL_MIN_SECONDS,
            OAUTH_ACCESS_TOKEN_TTL_MAX_SECONDS,
        ),
        (
            "oauth_refresh_token_ttl_seconds",
            -1,
            "DTXWIKI_OAUTH_REFRESH_TOKEN_TTL_SECONDS",
            OAUTH_REFRESH_TOKEN_TTL_MIN_SECONDS,
            OAUTH_REFRESH_TOKEN_TTL_MAX_SECONDS,
        ),
        (
            "oauth_refresh_token_ttl_seconds",
            OAUTH_REFRESH_TOKEN_TTL_MAX_SECONDS + 1,
            "DTXWIKI_OAUTH_REFRESH_TOKEN_TTL_SECONDS",
            OAUTH_REFRESH_TOKEN_TTL_MIN_SECONDS,
            OAUTH_REFRESH_TOKEN_TTL_MAX_SECONDS,
        ),
    ],
)
def test_oauth_ttl_out_of_range_is_readable(
    field_name,
    value,
    env_key,
    minimum,
    maximum,
):
    with pytest.raises(ValidationError) as excinfo:
        Settings(**{field_name: value})

    message = str(excinfo.value)
    assert env_key in message
    assert f"between {minimum} and {maximum} seconds" in message
    assert f"got {value}" in message


def test_refresh_ttl_default_is_30_days():
    # MCP-05: restored to the pre-branch 30-day default. 66cfe175 introduced
    # the ability to turn sliding off (a real feature) but silently widened
    # the idle window an unused connection stays resumable in along with it
    # (30d -> 180d); this pins the deliberate, documented value.
    assert Settings().oauth_refresh_token_ttl_seconds == (
        OAUTH_REFRESH_TOKEN_TTL_DEFAULT_SECONDS
    )
    assert OAUTH_REFRESH_TOKEN_TTL_DEFAULT_SECONDS == 2_592_000


def test_startup_log_reports_effective_ttls_and_sources(monkeypatch):
    monkeypatch.setenv("DTXWIKI_OAUTH_ACCESS_TOKEN_TTL_SECONDS", "7200")
    settings = Settings()

    # caplog reads through the root logger, which a full-suite run leaves
    # unusable -- see captured_log_messages' docstring.
    with captured_log_messages("dtxwiki.startup", logging.INFO) as messages:
        settings.validate_for_startup()

    message = messages[-1]
    assert "access_token_ttl_seconds=7200 source=environment override" in message
    assert (
        "refresh_token_ttl_seconds=2592000 source=built-in default"
        in message
    )
    assert (
        "refresh_token_sliding_enabled=True source=built-in default" in message
    )


def test_lifetime_report_includes_sliding_policy_source(monkeypatch):
    monkeypatch.setenv("DTXWIKI_OAUTH_REFRESH_TOKEN_SLIDING_ENABLED", "false")
    report = oauth_token_lifetime_report(Settings())

    assert report["refresh_sliding"] == {
        "enabled": False,
        "source": "environment override",
    }
