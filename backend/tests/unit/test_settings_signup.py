from __future__ import annotations

import pytest

from backend.app.settings import Settings, default_signup_enabled


@pytest.mark.parametrize(
    ("app_mode", "expected"),
    [
        ("solo_local", True),
        ("solo_remote", False),
        ("team", False),
        ("unknown", False),
    ],
)
def test_default_signup_enabled_is_local_only(app_mode: str, expected: bool):
    assert default_signup_enabled(app_mode) is expected


@pytest.mark.parametrize(
    ("app_mode", "expected"),
    [("solo_local", True), ("solo_remote", False), ("team", False)],
)
def test_unset_signup_uses_mode_default(monkeypatch, app_mode: str, expected: bool):
    monkeypatch.delenv("DTXWIKI_SIGNUP_ENABLED", raising=False)

    settings = Settings(app_mode=app_mode)

    assert settings.signup_enabled is None
    assert settings.effective_signup_enabled is expected


@pytest.mark.parametrize("app_mode", ["solo_local", "solo_remote", "team"])
@pytest.mark.parametrize("explicit", [False, True])
def test_explicit_signup_value_wins_for_every_mode(app_mode: str, explicit: bool):
    settings = Settings(app_mode=app_mode, signup_enabled=explicit)

    assert settings.signup_enabled is explicit
    assert settings.effective_signup_enabled is explicit


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
def test_explicit_signup_environment_value_wins(monkeypatch, raw: str, expected: bool):
    monkeypatch.setenv("DTXWIKI_APP_MODE", "team")
    monkeypatch.setenv("DTXWIKI_SIGNUP_ENABLED", raw)

    settings = Settings()

    assert settings.signup_enabled is expected
    assert settings.effective_signup_enabled is expected


def test_startup_keeps_unset_remote_signup_fail_closed(monkeypatch):
    monkeypatch.delenv("DTXWIKI_SIGNUP_ENABLED", raising=False)
    settings = Settings(app_mode="team", session_secret="s" * 64)

    with pytest.warns(UserWarning, match="production guards"):
        settings.validate_for_startup()

    assert settings.signup_enabled is None
    assert settings.effective_signup_enabled is False
