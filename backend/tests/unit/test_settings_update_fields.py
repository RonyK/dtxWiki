"""The self-update Settings fields must be real fields (guard (h) / config show)."""

from backend.app.settings import Settings
from backend.services import deployment

UPDATE_ENV_KEYS = {
    "DTXWIKI_NO_UPDATE_NOTIFIER",
    "DTXWIKI_UPDATE_INDEX_URL",
    "DTXWIKI_UPDATE_CHANNEL",
    "DTXWIKI_UPDATE_KEEP_BACKUPS",
}


def test_update_field_defaults():
    settings = Settings()
    assert settings.no_update_notifier is False
    assert settings.update_index_url is None
    assert settings.update_channel == "stable"
    assert settings.update_keep_backups == 5


def test_update_fields_parse_from_env(monkeypatch):
    monkeypatch.setenv("DTXWIKI_NO_UPDATE_NOTIFIER", "true")
    monkeypatch.setenv("DTXWIKI_UPDATE_INDEX_URL", "https://pypi.internal/simple")
    monkeypatch.setenv("DTXWIKI_UPDATE_CHANNEL", "prerelease")
    monkeypatch.setenv("DTXWIKI_UPDATE_KEEP_BACKUPS", "3")
    settings = Settings()
    assert settings.no_update_notifier is True
    assert settings.update_index_url == "https://pypi.internal/simple"
    assert settings.update_channel == "prerelease"
    assert settings.update_keep_backups == 3


def test_update_keys_are_known_to_guard_h():
    # guard (h) treats `DTXWIKI_<FIELD>` as known iff the field exists; the new
    # fields must therefore not be flagged as "unknown DTXWIKI_ env" in production.
    known = {f"DTXWIKI_{name.upper()}" for name in Settings.model_fields}
    assert UPDATE_ENV_KEYS <= known


def test_update_keys_in_valid_env_keys():
    assert UPDATE_ENV_KEYS <= deployment.valid_env_keys()


def test_update_channel_coerces_unknown_to_stable():
    # Runtime is lenient (a stray value must not crash startup) but normalizes.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        settings = Settings(update_channel="beta")
    assert settings.update_channel == "stable"


def test_update_keep_backups_coerces_below_one():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        settings = Settings(update_keep_backups=0)
    assert settings.update_keep_backups == 1


def test_config_set_rejects_bad_channel():
    import pytest

    from backend.cli import prod_setup

    with pytest.raises(ValueError):
        prod_setup.validate_config_value("DTXWIKI_UPDATE_CHANNEL", "beta")


def test_config_set_rejects_nonpositive_keep_backups():
    import pytest

    from backend.cli import prod_setup

    with pytest.raises(ValueError):
        prod_setup.validate_config_value("DTXWIKI_UPDATE_KEEP_BACKUPS", "0")


def test_dead_source_upload_limit_method_removed():
    # F1: this method referenced the renamed Workspace.source_policy column and
    # would AttributeError; it had no callers and must stay removed.
    from backend.services.settings_service import SettingsService

    assert not hasattr(SettingsService, "source_upload_limit_for_workspace")
