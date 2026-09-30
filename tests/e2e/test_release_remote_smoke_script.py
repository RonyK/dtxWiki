from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/release_remote_smoke.sh"
DOCKERFILE = ROOT / "Dockerfile"


def test_release_remote_smoke_script_has_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_release_remote_smoke_image_has_dockerfile():
    content = DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM python:3.12-slim" in content
    assert 'CMD ["dtxwiki", "serve"' in content


def test_release_remote_smoke_script_writes_remote_mode_marker():
    content = SCRIPT.read_text(encoding="utf-8")

    assert "DTXWIKI_REMOTE_SMOKE_MODE=remote" in content
    assert "DTXWIKI_REMOTE_CLIENT=$REMOTE_CLIENT" in content
    assert "DTXWIKI_REMOTE_HARNESS=docker-compose" in content
    assert "DTXWIKI_REMOTE_TRANSPORT=streamable" in content
    assert "DTXWIKI_REMOTE_TLS_VERIFY=ca-file" in content
    assert "--remote-smoke-mode=remote" in content


def test_release_remote_smoke_script_self_checks_required_markers():
    content = SCRIPT.read_text(encoding="utf-8")

    assert 'grep -Fxq "DTXWIKI_REMOTE_SMOKE_MODE=remote"' in content
    assert 'grep -Fxq "DTXWIKI_REMOTE_CLIENT=$REMOTE_CLIENT"' in content
    assert 'grep -Fxq "DTXWIKI_REMOTE_HARNESS=docker-compose"' in content
    assert 'grep -Fxq "DTXWIKI_REMOTE_TRANSPORT=streamable"' in content
    assert 'grep -Fxq "DTXWIKI_REMOTE_TLS_VERIFY=ca-file"' in content
    assert "DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok" in content
    assert 'grep -Fxq "DTXWIKI_REMOTE_COMPOSE_TEARDOWN=ok"' in content


def test_release_remote_smoke_script_self_checks_required_nodes():
    content = SCRIPT.read_text(encoding="utf-8")

    expected_nodes = [
        "test_pat_immediate_connect_list_tools",
        "test_pat_read_page_round_trip",
        "test_pat_write_agent_page_draft_round_trip",
        "test_oauth_full_flow_tool_round_trip",
        "test_oauth_refresh_rotation_and_reuse_denied_via_jti",
        "test_oauth_revoke_invalidates_access_jti",
        "test_cross_workspace_denied_with_keyword",
        "test_restricted_library_denied_with_keyword",
    ]
    for node in expected_nodes:
        assert node in content
    assert (
        'grep -Eq "remote_mcp_smoke.py::$node[[:space:]]+PASSED" '
        '"$EVIDENCE_FILE"'
    ) in content


def test_release_remote_smoke_script_self_checks_exact_pass_count():
    content = SCRIPT.read_text(encoding="utf-8")

    assert 'grep -Eq "(^|[=[:space:]])8 passed([,[:space:]]|$)"' in content


def test_release_remote_smoke_requires_docker_before_hostname(tmp_path):
    result = _run_with_minimal_path(tmp_path)

    assert result.returncode == 2
    assert "docker CLI is required" in result.stderr
    assert "dtxwiki.test must resolve" not in result.stderr


def test_release_remote_smoke_requires_compose_before_hostname_when_reusing_image(
    tmp_path,
):
    result = _run_with_minimal_path(
        tmp_path,
        extra_env={"DTXWIKI_REMOTE_E2E_BUILD": "0"},
    )

    assert result.returncode == 2
    assert "Docker Compose is required" in result.stderr
    assert "dtxwiki.test must resolve" not in result.stderr


def test_release_remote_smoke_preflight_reports_missing_prerequisites(tmp_path):
    result = _run_with_minimal_path(
        tmp_path,
        args=["--preflight"],
        extra_env={"DTXWIKI_PYTHON": str(tmp_path / "missing-python")},
    )

    assert result.returncode == 2
    assert "MISSING: docker CLI for image build" in result.stderr
    assert "OK: Dockerfile for image build" in result.stdout
    assert "MISSING: Docker Compose runtime" in result.stderr
    assert "MISSING: dtxwiki.test resolves to 127.0.0.1" in result.stderr
    assert "MISSING: host Python has pytest" in result.stderr
    assert "remote MCP smoke preflight: FAILED" in result.stderr


def test_release_remote_smoke_requires_pytest_before_build(tmp_path):
    bin_dir = _minimal_bin_dir(tmp_path)
    grep = shutil.which("grep")
    assert grep is not None
    _symlink_or_skip(bin_dir / "grep", grep)
    _write_executable(
        bin_dir / "docker",
        """#!/bin/sh
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then
  exit 0
fi
if [ "$1" = "build" ]; then
  echo "build should not run before pytest preflight" >&2
  exit 99
fi
exit 0
""",
    )
    _write_executable(
        bin_dir / "getent",
        """#!/bin/sh
if [ "$1" = "hosts" ] && [ "$2" = "dtxwiki.test" ]; then
  echo "127.0.0.1 dtxwiki.test"
  exit 0
fi
exit 1
""",
    )
    _write_executable(
        bin_dir / "python3",
        """#!/bin/sh
exit 1
""",
    )
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "DTXWIKI_PYTHON": str(bin_dir / "python3"),
    }

    result = subprocess.run(
        [shutil.which("bash") or "bash", str(SCRIPT), str(tmp_path / "out.log")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "pytest is required" in result.stderr
    assert "build should not run" not in result.stderr


def test_release_remote_smoke_requires_dockerfile_before_build(tmp_path):
    bin_dir = _minimal_bin_dir(tmp_path)
    grep = shutil.which("grep")
    assert grep is not None
    _symlink_or_skip(bin_dir / "grep", grep)
    _write_executable(
        bin_dir / "docker",
        """#!/bin/sh
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then
  exit 0
fi
if [ "$1" = "build" ]; then
  echo "build should not run without Dockerfile" >&2
  exit 99
fi
exit 0
""",
    )
    _write_executable(
        bin_dir / "getent",
        """#!/bin/sh
if [ "$1" = "hosts" ] && [ "$2" = "dtxwiki.test" ]; then
  echo "127.0.0.1 dtxwiki.test"
  exit 0
fi
exit 1
""",
    )
    _write_executable(
        bin_dir / "python3",
        """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "pytest" ] && [ "$3" = "--version" ]; then
  exit 0
fi
exit 1
""",
    )
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "DTXWIKI_PYTHON": str(bin_dir / "python3"),
        "DTXWIKI_REMOTE_E2E_DOCKERFILE": str(tmp_path / "missing-Dockerfile"),
    }

    result = subprocess.run(
        [shutil.which("bash") or "bash", str(SCRIPT), str(tmp_path / "out.log")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Dockerfile is required" in result.stderr
    assert "build should not run" not in result.stderr


def _run_with_minimal_path(
    tmp_path: Path,
    *,
    extra_env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = _minimal_bin_dir(tmp_path)
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        **(extra_env or {}),
    }
    command = [shutil.which("bash") or "bash", str(SCRIPT)]
    if args:
        command.extend(args)
    command.append(str(tmp_path / "remote-smoke.log"))
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _minimal_bin_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if shutil.which("bash") is None:
        pytest.skip("bash is required to exercise release_remote_smoke.sh")
    for command in ("awk", "cat", "dirname"):
        resolved = shutil.which(command)
        assert resolved is not None
        _symlink_or_skip(bin_dir / command, resolved)
    return bin_dir


def _symlink_or_skip(link: Path, target: str) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"cannot create symlinks on this host: {exc}")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
