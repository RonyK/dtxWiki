"""Tests for the production deployment runbook (DEPLOYMENT.md) section presence."""

from pathlib import Path

README = Path(__file__).resolve().parents[2] / "DEPLOYMENT.md"


def _content() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_exists():
    assert README.is_file()


def test_readme_has_eight_steps():
    content = _content()
    for step_num in range(1, 9):
        assert f"## Step {step_num}" in content, f"Missing ## Step {step_num}"


def test_readme_has_database_section():
    content = _content()
    # The runbook states the production DB stance (SQLite-only). This replaced
    # the old Sprint_5 PG-epilogue table the operator doc no longer carries.
    assert "## 데이터베이스" in content
    assert "SQLite 전용" in content


def test_readme_has_cross_platform_section():
    content = _content()
    assert r"%USERPROFILE%\.codex\config.toml" in content
    assert r"%APPDATA%\codex" not in content


def test_readme_declares_dual_license_artifacts():
    content = _content()
    assert "AGPL-3.0-only OR Elastic-2.0" in content
    for artifact in ("LICENSE", "LICENSE-AGPL-3.0", "LICENSE-ELv2", "NOTICE"):
        assert f"/app/{artifact}`" in content


def test_readme_keygen_step_present():
    content = _content()
    assert "dtxwiki keygen" in content
    assert "sudo install -o root -g dtxwiki -m 0640" in content
    assert "/var/lib/dtxwiki/private/jwt.pem" in content


def test_readme_manual_bootstrap_step_present():
    content = _content()
    assert "dtxwiki bootstrap" in content
    assert "DTXWIKI_AUTO_BOOTSTRAP=false" in content
    assert "DTXWIKI_AUTO_BOOTSTRAP=true" not in content


def test_readme_has_health_monitoring_guide():
    content = _content()
    assert "/health/live" in content
    assert "/health/ready" in content
    assert "deprecated" in content


def test_readme_uses_python312_required_by_package():
    content = _content()
    assert "python3.12" in content
    assert "python3.11 -m venv" not in content


def test_readme_uses_installed_production_smoke_path():
    content = _content()
    assert (
        "/opt/dtxwiki/.venv/share/dtxwiki/tests/packaging/production_smoke.sh"
        in content
    )


def test_readme_uses_caddy_reload_or_restart():
    content = _content()
    assert "systemctl reload-or-restart caddy" in content
    assert "systemctl reload caddy" not in content


def test_readme_certbot_is_noninteractive():
    content = _content()
    assert "--agree-tos" in content
    assert "-m admin@example.com" in content
    assert "--no-eff-email" in content
    assert "운영자 이메일" in content
