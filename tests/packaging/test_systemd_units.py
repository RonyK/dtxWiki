"""Tests for infra/prod systemd unit files (Task 4.4)."""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

INFRA_DIR = Path(__file__).resolve().parents[2] / "infra" / "prod"
BACKEND_UNIT = INFRA_DIR / "dtxwiki.service"
WORKER_UNIT = INFRA_DIR / "dtxwiki-worker.service"
UNITS = [BACKEND_UNIT, WORKER_UNIT]
# s-10: standard sandboxing that python, uvicorn and SQLite run under.
HARDENING = {
    "UMask": "0027",
    "StateDirectory": "dtxwiki",
    "StateDirectoryMode": "0750",
    "NoNewPrivileges": "yes",
    "CapabilityBoundingSet": "",
    "PrivateTmp": "yes",
    "PrivateDevices": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "ProtectControlGroups": "yes",
    "RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6",
    "RestrictNamespaces": "yes",
    "RestrictRealtime": "yes",
    "LockPersonality": "yes",
    "SystemCallArchitectures": "native",
    "ReadWritePaths": "/var/lib/dtxwiki",
}


def _section(unit: Path, name: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    current = None
    for raw in unit.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = line
            continue
        if current == name:
            key, _, value = line.partition("=")
            values.setdefault(key, []).append(value)
    return values


def test_units_exist():
    assert BACKEND_UNIT.is_file(), f"Missing: {BACKEND_UNIT}"
    assert WORKER_UNIT.is_file(), f"Missing: {WORKER_UNIT}"


def test_backend_unit_type_simple():
    content = BACKEND_UNIT.read_text()
    lines_with_type = [
        line for line in content.splitlines() if line.startswith("Type=")
    ]
    assert len(lines_with_type) == 1
    assert lines_with_type[0] == "Type=simple"


def test_backend_exec_start_uses_cli_not_module_main():
    content = BACKEND_UNIT.read_text()
    assert "python -m backend.app.main" not in content
    assert "dtxwiki serve" in content


def test_backend_worker_mode_is_restored_with_the_env_file_on_rollback():
    service = _section(BACKEND_UNIT, "[Service]")
    assert service["EnvironmentFile"] == ["/etc/dtxwiki/dtxwiki.env"]
    # An inline setting survives an env-file rollback and makes 0.1.2 reject
    # this unknown DTXWIKI_* key during its production startup check.
    assert all(
        "DTXWIKI_WORKER_MODE=" not in value
        for value in service.get("Environment", [])
    )


def test_backend_listens_on_loopback_by_default():
    # `dtxwiki setup production --bind-host` rewrites exactly this value.
    (exec_start,) = _section(BACKEND_UNIT, "[Service]")["ExecStart"]
    assert re.search(r"\s--host 127\.0\.0\.1\s", exec_start)


def test_worker_unit_has_no_idle_exit_after_zero():
    content = WORKER_UNIT.read_text()
    assert "--idle-exit-after 0" not in content


def test_worker_unit_uses_soft_backend_dependency():
    content = WORKER_UNIT.read_text()
    assert "Requires=dtxwiki.service" not in content
    assert "Wants=network-online.target dtxwiki.service" in content
    assert "After=network-online.target dtxwiki.service" in content


def test_worker_stops_and_restarts_with_the_backend():
    # An update or rollback stops/restarts dtxwiki; the worker must follow so it
    # never runs old code against a migrated database.
    assert _section(WORKER_UNIT, "[Unit]")["PartOf"] == ["dtxwiki.service"]


@pytest.mark.parametrize("unit", UNITS, ids=lambda path: path.name)
def test_units_are_sandboxed(unit: Path):
    service = _section(unit, "[Service]")
    assert {key: service.get(key) for key in HARDENING} == {
        key: [value] for key, value in HARDENING.items()
    }


def _verifiable_copy(unit: Path, directory: Path) -> Path:
    """The unit with paths that exist here (verify checks the executable)."""

    text = unit.read_text()
    text = re.sub(r"(?m)^ExecStart=\S+", "ExecStart=/bin/true", text)
    text = re.sub(r"(?m)^WorkingDirectory=.*$", "WorkingDirectory=/", text)
    copy = directory / unit.name
    copy.write_text(text)
    return copy


@pytest.mark.skipif(sys.platform != "linux", reason="systemd-analyze only on Linux")
def test_units_parse_via_systemd_analyze(tmp_path: Path):
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze is unavailable")
    copies = [_verifiable_copy(unit, tmp_path) for unit in UNITS]
    result = subprocess.run(
        ["systemd-analyze", "verify", *map(str, copies)],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    # verify reports unknown or misspelled directives as warnings, not errors.
    assert "Unknown key" not in output
    assert "Unknown section" not in output


def test_backend_single_exec_start():
    """ADR A12 single-process invariant."""
    content = BACKEND_UNIT.read_text()
    exec_starts = [
        line for line in content.splitlines() if line.startswith("ExecStart=")
    ]
    assert len(exec_starts) == 1
