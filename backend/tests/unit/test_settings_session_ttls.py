import pytest
from pydantic import ValidationError

from backend.app import settings as settings_module
from backend.app.settings import Settings
from backend.services import session_service


def test_remembered_session_lifetime_defaults_and_independent_normal_ttl():
    settings = Settings(session_ttl_seconds=123)

    assert settings.session_ttl_seconds == 123
    assert settings.auth_reauthentication_max_age_seconds == 3600
    assert settings.session_remembered_ttl_seconds == 60 * 60 * 24 * 30
    assert settings.session_remembered_refresh_threshold_seconds == 60 * 60 * 24 * 7
    assert settings.session_remembered_absolute_ttl_seconds == 60 * 60 * 24 * 90


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"session_remembered_ttl_seconds": 0},
            "session_remembered_ttl_seconds",
        ),
        (
            {"session_remembered_refresh_threshold_seconds": 0},
            "session_remembered_refresh_threshold_seconds",
        ),
        (
            {"session_remembered_absolute_ttl_seconds": 0},
            "session_remembered_absolute_ttl_seconds",
        ),
        (
            {
                "session_remembered_ttl_seconds": 100,
                "session_remembered_refresh_threshold_seconds": 100,
            },
            "DTXWIKI_SESSION_REMEMBERED_REFRESH_THRESHOLD_SECONDS",
        ),
        (
            {
                "session_remembered_ttl_seconds": 101,
                "session_remembered_refresh_threshold_seconds": 10,
                "session_remembered_absolute_ttl_seconds": 100,
            },
            "DTXWIKI_SESSION_REMEMBERED_TTL_SECONDS",
        ),
    ],
)
def test_remembered_session_lifetime_validation(overrides, message):
    with pytest.raises(ValidationError, match=message):
        Settings(**overrides)


def test_normal_session_ttl_override_has_no_remembered_lifetime_relationship():
    settings = Settings(
        session_ttl_seconds=60 * 60 * 24 * 365,
        session_remembered_ttl_seconds=100,
        session_remembered_refresh_threshold_seconds=10,
        session_remembered_absolute_ttl_seconds=200,
    )

    assert settings.session_ttl_seconds == 60 * 60 * 24 * 365


def test_remembered_session_lifetimes_load_from_exact_environment_keys(monkeypatch):
    monkeypatch.setenv("DTXWIKI_AUTH_REAUTHENTICATION_MAX_AGE_SECONDS", "2400")
    monkeypatch.setenv("DTXWIKI_SESSION_REMEMBERED_TTL_SECONDS", "300")
    monkeypatch.setenv(
        "DTXWIKI_SESSION_REMEMBERED_REFRESH_THRESHOLD_SECONDS",
        "30",
    )
    monkeypatch.setenv("DTXWIKI_SESSION_REMEMBERED_ABSOLUTE_TTL_SECONDS", "900")

    settings = Settings()

    assert settings.auth_reauthentication_max_age_seconds == 2400
    assert settings.session_remembered_ttl_seconds == 300
    assert settings.session_remembered_refresh_threshold_seconds == 30
    assert settings.session_remembered_absolute_ttl_seconds == 900


@pytest.mark.parametrize("value", [0, -1])
def test_ordinary_session_ttl_is_rejected_at_construction(value):
    """SessionTokenCodec.__post_init__ rejects a non-positive ttl, but it runs
    inside the get_session_service request dependency. Without a field
    constraint the app boots cleanly and then 500s every authenticated
    request, with no startup diagnostic pointing at the variable."""

    with pytest.raises(ValidationError, match="session_ttl_seconds"):
        Settings(session_ttl_seconds=value)
    with pytest.raises(ValidationError, match="auth_reauthentication_max_age_seconds"):
        Settings(auth_reauthentication_max_age_seconds=value)


def test_session_lifetime_defaults_have_one_definition():
    """The codec's fallbacks and the operator-visible Settings defaults must
    be the same objects, not two literals that can drift apart silently."""

    assert (
        session_service.DEFAULT_SESSION_TTL_SECONDS
        is settings_module.SESSION_TTL_DEFAULT_SECONDS
    )
    assert (
        session_service.DEFAULT_REMEMBERED_SESSION_TTL_SECONDS
        is settings_module.SESSION_REMEMBERED_TTL_DEFAULT_SECONDS
    )
    assert (
        session_service.DEFAULT_REMEMBERED_SESSION_REFRESH_THRESHOLD_SECONDS
        is settings_module.SESSION_REMEMBERED_REFRESH_THRESHOLD_DEFAULT_SECONDS
    )
    assert (
        session_service.DEFAULT_REMEMBERED_SESSION_ABSOLUTE_TTL_SECONDS
        is settings_module.SESSION_REMEMBERED_ABSOLUTE_TTL_DEFAULT_SECONDS
    )
    assert Settings().session_ttl_seconds == settings_module.SESSION_TTL_DEFAULT_SECONDS
