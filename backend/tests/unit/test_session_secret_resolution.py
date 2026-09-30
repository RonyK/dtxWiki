"""Session secret resolution (0.1.1 contract C2).

DTXWIKI_SESSION_SECRET, then DTXWIKI_SESSION_SECRET_FILE, then -- solo_local
only -- a secret generated once into the data directory; remote modes with
neither refuse to start, and published or placeholder values are refused in
every mode.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

import backend.app.settings as settings_module
from backend.app.settings import SESSION_SECRET_FILE_NAME, Settings
from backend.tests.utils.environment import clear_ambient_dtxwiki_env

REMOTE_SECRET = "remote-session-secret-" + "r" * 40
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    clear_ambient_dtxwiki_env(monkeypatch)
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "data"))


def _data_dir_secret(tmp_path: Path) -> Path:
    return tmp_path / "data" / SESSION_SECRET_FILE_NAME


@POSIX_ONLY
def test_solo_local_generates_a_private_data_dir_secret_once(tmp_path):
    first = Settings(app_mode="solo_local").effective_session_secret

    secret_file = _data_dir_secret(tmp_path)
    assert secret_file.read_text(encoding="utf-8").strip() == first
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    # secrets.token_urlsafe(64): 64 random bytes, 86 characters.
    assert len(first) >= 80
    # A second process (a new Settings) reuses it instead of rotating it,
    # which would sign every browser out on each restart.
    assert Settings(app_mode="solo_local").effective_session_secret == first


def test_solo_local_secret_is_not_the_published_default(tmp_path):
    secret = Settings(app_mode="solo_local").effective_session_secret

    assert secret != "dtxwiki-development-session-secret"


@POSIX_ONLY
def test_existing_data_dir_secret_is_tightened_to_owner_only(tmp_path):
    secret_file = _data_dir_secret(tmp_path)
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("kept-local-secret-value\n", encoding="utf-8")
    os.chmod(secret_file, 0o644)

    assert Settings().effective_session_secret == "kept-local-secret-value"
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600


def test_validation_alone_never_writes_the_data_dir(tmp_path):
    # bootstrap() validates before honouring DTXWIKI_AUTO_BOOTSTRAP=false and
    # must leave the data directory untouched.
    Settings(app_mode="solo_local").validate_session_secret_invariant()

    assert not (tmp_path / "data").exists()


def test_unusable_data_dir_secret_names_the_file(tmp_path):
    secret_file = _data_dir_secret(tmp_path)
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("   \n", encoding="utf-8")

    with pytest.raises(RuntimeError) as excinfo:
        Settings().validate_session_secret_invariant()

    assert str(secret_file) in str(excinfo.value)
    assert "Delete it" in str(excinfo.value)


def test_concurrently_published_secret_wins(tmp_path, monkeypatch):
    """Two first starts racing: whichever secret lands first is used by both."""

    secret_file = _data_dir_secret(tmp_path)
    real_link = os.link

    def lose_the_race(source, destination):
        Path(destination).write_text("the-other-process-secret\n", encoding="utf-8")
        return real_link(source, destination)

    monkeypatch.setattr(settings_module.os, "link", lose_the_race)

    assert Settings().effective_session_secret == "the-other-process-secret"
    assert secret_file.read_text(encoding="utf-8") == "the-other-process-secret\n"


def test_secret_file_is_used_in_team_mode(tmp_path):
    secret_file = tmp_path / "session_secret"
    secret_file.write_text(f"\n  {REMOTE_SECRET}  \n", encoding="utf-8")

    settings = Settings(app_mode="team", session_secret_file=str(secret_file))
    settings.validate_session_secret_invariant()

    assert settings.effective_session_secret == REMOTE_SECRET
    assert settings.resolved_session_secret.source == "file"


def test_secret_file_is_read_from_the_environment(tmp_path, monkeypatch):
    secret_file = tmp_path / "session_secret"
    secret_file.write_text(REMOTE_SECRET, encoding="utf-8")
    monkeypatch.setenv("DTXWIKI_APP_MODE", "team")
    monkeypatch.setenv("DTXWIKI_SESSION_SECRET", "")
    monkeypatch.setenv("DTXWIKI_SESSION_SECRET_FILE", str(secret_file))

    assert Settings().effective_session_secret == REMOTE_SECRET


def test_explicit_secret_wins_over_the_file(tmp_path):
    secret_file = tmp_path / "session_secret"
    secret_file.write_text("file-secret-" + "f" * 40, encoding="utf-8")

    settings = Settings(
        app_mode="team",
        session_secret=REMOTE_SECRET,
        session_secret_file=str(secret_file),
    )

    assert settings.effective_session_secret == REMOTE_SECRET
    assert settings.resolved_session_secret.source == "environment"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda path: None, "does not exist"),
        (lambda path: path.write_text(" \n", encoding="utf-8"), "it is empty"),
        (lambda path: path.mkdir(), "not a regular file"),
    ],
    ids=["missing", "empty", "directory"],
)
def test_unusable_secret_file_is_a_startup_error_naming_the_path(
    tmp_path, setup, reason
):
    secret_file = tmp_path / "session_secret"
    setup(secret_file)
    settings = Settings(app_mode="team", session_secret_file=str(secret_file))

    with pytest.raises(RuntimeError) as excinfo:
        settings.validate_for_startup()

    assert str(secret_file) in str(excinfo.value)
    assert reason in str(excinfo.value)


@pytest.mark.parametrize("mode", ["team", "solo_remote"])
def test_remote_mode_without_any_secret_refuses_to_start(mode, tmp_path):
    with pytest.raises(RuntimeError, match="DTXWIKI_SESSION_SECRET"):
        Settings(app_mode=mode).validate_for_startup()
    # No silent fallback to a generated secret either.
    assert not _data_dir_secret(tmp_path).exists()


PLACEHOLDERS = [
    "dtxwiki-development-session-secret",
    "<replace-with-64-byte-secret>",
    "REPLACE-WITH-a-real-secret-of-at-least-sixty-four-chars",
    "please-change-me-before-going-live-0123456789abcdef",
    "<generated>",
]


@pytest.mark.parametrize("mode", ["solo_local", "solo_remote", "team"])
@pytest.mark.parametrize("value", PLACEHOLDERS)
def test_placeholder_secret_is_refused_in_every_mode(mode, value):
    settings = Settings(app_mode=mode, session_secret=value)

    with pytest.raises(RuntimeError, match="DTXWIKI_SESSION_SECRET"):
        settings.validate_for_startup()


@pytest.mark.parametrize("value", PLACEHOLDERS)
def test_placeholder_in_secret_file_is_refused(tmp_path, value):
    secret_file = tmp_path / "session_secret"
    secret_file.write_text(value, encoding="utf-8")

    with pytest.raises(RuntimeError, match="DTXWIKI_SESSION_SECRET_FILE"):
        Settings(
            app_mode="solo_local", session_secret_file=str(secret_file)
        ).validate_for_startup()


@pytest.mark.parametrize("mode", ["solo_remote", "team"])
def test_short_secret_is_refused_in_remote_modes(mode):
    short = "s" * 31

    with pytest.raises(RuntimeError, match="shorter than 32"):
        Settings(app_mode=mode, session_secret=short).validate_for_startup()
    Settings(app_mode=mode, session_secret=short + "s").validate_for_startup()


def test_short_explicit_secret_is_accepted_for_local_use():
    settings = Settings(app_mode="solo_local", session_secret="local-dev-secret")
    settings.validate_for_startup()

    assert settings.effective_session_secret == "local-dev-secret"


def test_production_requires_an_operator_provided_secret(tmp_path):
    """Production never falls back to the generated solo_local secret."""

    settings = Settings(
        app_env="production",
        app_mode="solo_local",
        public_base_url="https://wiki.example.com",
        auto_bootstrap=False,
    )

    with pytest.raises(RuntimeError, match="DTXWIKI_SESSION_SECRET_FILE"):
        settings.validate_production_invariants(check_key_file=False)


def test_production_accepts_a_secret_file(tmp_path):
    secret_file = tmp_path / "session_secret"
    secret_file.write_text(REMOTE_SECRET, encoding="utf-8")
    settings = Settings(
        app_env="production",
        app_mode="team",
        public_base_url="https://wiki.example.com",
        session_secret_file=str(secret_file),
        auto_bootstrap=False,
    )

    # Everything but the JWT key file, which this test does not provide.
    settings.validate_production_invariants(check_key_file=False)
    assert settings.effective_session_secret == REMOTE_SECRET
