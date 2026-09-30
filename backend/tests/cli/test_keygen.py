"""Tests for dtxwiki keygen CLI command (Task 4.12)."""
import sys

import pytest
from typer.testing import CliRunner

from backend.cli.main import app

runner = CliRunner()


def test_keygen_creates_pkcs8_pem(tmp_path):
    out = tmp_path / "jwt.pem"
    result = runner.invoke(app, ["keygen", "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert out.is_file()
    content = out.read_bytes()
    assert b"PRIVATE KEY" in content


def test_keygen_chmod_600_on_unix(tmp_path):
    if sys.platform == "win32":
        pytest.skip("chmod 600 not enforced on Windows")
    out = tmp_path / "jwt.pem"
    runner.invoke(app, ["keygen", "--output", str(out)])
    mode = out.stat().st_mode & 0o777
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


def test_keygen_existing_file_protected_without_force(tmp_path):
    out = tmp_path / "jwt.pem"
    # First invocation — creates file
    result = runner.invoke(app, ["keygen", "--output", str(out)])
    assert result.exit_code == 0
    original_content = out.read_bytes()

    # Second invocation without --force — should fail
    result2 = runner.invoke(app, ["keygen", "--output", str(out)])
    assert result2.exit_code == 1
    assert out.read_bytes() == original_content  # unchanged


def test_keygen_force_overwrites(tmp_path):
    out = tmp_path / "jwt.pem"
    runner.invoke(app, ["keygen", "--output", str(out)])

    result = runner.invoke(app, ["keygen", "--output", str(out), "--force"])
    assert result.exit_code == 0
    # New key is generated (different content with overwhelming probability)
    new_content = out.read_bytes()
    assert b"PRIVATE KEY" in new_content


def test_keygen_emits_env_hint(tmp_path):
    out = tmp_path / "jwt.pem"
    result = runner.invoke(app, ["keygen", "--output", str(out)])
    assert result.exit_code == 0
    assert "DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH" in result.output
