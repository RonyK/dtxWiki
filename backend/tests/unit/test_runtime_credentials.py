"""Local initialization preserves signing identity across entry points."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.runtime_credentials import prepare_serving_credentials
from backend.app.settings import Settings
from backend.tests.utils.environment import clear_ambient_dtxwiki_env


@pytest.fixture
def local(tmp_path, monkeypatch):
    clear_ambient_dtxwiki_env(monkeypatch)
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def test_generated_key_and_session_are_durable_without_rotation(local):
    first = prepare_serving_credentials(Settings())
    key_path = Path(first.oauth_jwt_private_key_path)
    key_bytes = key_path.read_bytes()
    secret = first.effective_session_secret
    second = prepare_serving_credentials(Settings())
    assert second.oauth_jwt_private_key_path == str(key_path)
    assert key_path.read_bytes() == key_bytes
    assert second.effective_session_secret == secret
    if os.name != "nt":
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert stat.S_IMODE((local / "session_secret").stat().st_mode) == 0o600


def test_configured_credentials_keep_precedence(local):
    generated = prepare_serving_credentials(Settings())
    pem = Path(generated.oauth_jwt_private_key_path).read_text()
    configured = Settings(
        session_secret="explicit-session-secret-kept", oauth_jwt_private_key_pem=pem
    )
    result = prepare_serving_credentials(configured)
    assert result is configured
    assert result.oauth_jwt_private_key_pem == pem
    assert result.oauth_jwt_private_key_path is None
    assert result.effective_session_secret == "explicit-session-secret-kept"


def test_existing_desktop_key_is_reused(local):
    generated = prepare_serving_credentials(Settings())
    original = Path(generated.oauth_jwt_private_key_path)
    original.replace(local / "desktop-oauth-jwt.pem")
    result = prepare_serving_credentials(Settings())
    assert result.oauth_jwt_private_key_path == str(local / "desktop-oauth-jwt.pem")
    assert not original.exists()


def test_invalid_configured_key_is_not_replaced(local, tmp_path):
    path = tmp_path / "invalid.pem"
    path.write_text("existing-key-must-not-change")
    with pytest.raises(ValueError):
        prepare_serving_credentials(Settings(oauth_jwt_private_key_path=str(path)))
    assert path.read_text() == "existing-key-must-not-change"
    assert not (local / "oauth-jwt.pem").exists()


def test_real_init_validates_serving_configuration_before_schema(local, tmp_path):
    database = tmp_path / "must-stay-absent.sqlite"
    settings = Settings(database_url=f"sqlite:///{database}", cors_origins="*")
    packet = {
        "protocol": 2,
        "role": "init",
        "settings": settings.model_dump(mode="json"),
    }
    process = subprocess.Popen(
        [sys.executable, "-P", "-m", "backend.app.runtime_init"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[3],
    )
    try:
        process.stdin.write(json.dumps(packet) + "\n")
        process.stdin.flush()
        assert json.loads(process.stdout.readline()) == {
            "status": "registered",
            "role": "init",
        }
        process.stdin.write('{"command":"start"}\n')
        process.stdin.flush()
        assert process.wait(timeout=15) != 0
        assert "CORS_ORIGINS invalid" in process.stderr.read()
        assert not database.exists()
        assert not (local / "oauth-jwt.pem").exists()
        assert not (local / "session_secret").exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
