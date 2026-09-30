import pytest
from typer.testing import CliRunner

from backend.cli.main import app

pytestmark = pytest.mark.unit

runner = CliRunner()

_EMBEDDING_ENV = (
    "DTXWIKI_EMBEDDING_PROVIDER",
    "DTXWIKI_EMBEDDING_BASE_URL",
    "DTXWIKI_EMBEDDING_MODEL",
    "DTXWIKI_EMBEDDING_API_KEY",
    "DTXWIKI_EMBEDDING_DIM",
)


def _clear_env(monkeypatch):
    for key in _EMBEDDING_ENV:
        monkeypatch.delenv(key, raising=False)


def test_embedding_doctor_reports_none(monkeypatch):
    _clear_env(monkeypatch)
    result = runner.invoke(app, ["embedding", "doctor"])
    assert result.exit_code == 0
    assert "no embedding provider configured" in result.output


def test_embedding_doctor_probes_and_validates_dim(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DTXWIKI_EMBEDDING_PROVIDER", "deterministic")
    monkeypatch.setenv("DTXWIKI_EMBEDDING_DIM", "64")
    result = runner.invoke(app, ["embedding", "doctor"])
    assert result.exit_code == 0
    assert "OK" in result.output
    assert "64" in result.output


def test_embedding_doctor_reports_config_error(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DTXWIKI_EMBEDDING_PROVIDER", "openai_compatible")
    result = runner.invoke(app, ["embedding", "doctor"])
    assert result.exit_code == 1
    assert "config error" in result.output
