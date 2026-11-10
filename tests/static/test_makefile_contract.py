import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_frontend_ci_stops_on_shared_static_contract_failure(tmp_path):
    """A green Vitest run cannot hide the shared UI contracts in another CI job."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    frontend_steps = workflow["jobs"]["frontend"]["steps"]
    assert any(
        step.get("run") == "VENV=.venv make check-frontend"
        for step in frontend_steps
    )

    invocation_log = tmp_path / "invocations"
    tools_dir = tmp_path / "bin"
    tools_dir.mkdir()
    # Stub the gate's subprocesses, rather than running the suites recursively.
    # The static checker fails while every other subprocess would succeed.
    probe = os.linesep.join(
        [
            f"#!{sys.executable}",
            "import os, sys",
            "with open(os.environ['GATE_INVOCATIONS'], 'a') as log:",
            "    print(os.path.basename(sys.argv[0]), *sys.argv[1:], file=log)",
            "sys.exit(23 if 'test-static' in sys.argv[1:] else 0)",
            "",
        ]
    )
    for name in ("make", "npm", "node"):
        tool = tools_dir / name
        tool.write_text(probe)
        tool.chmod(0o755)

    make = shutil.which("make")
    assert make is not None
    result = subprocess.run(
        [
            make,
            "-f",
            str(ROOT / "Makefile"),
            "check-frontend",
            f"MAKE={tools_dir / 'make'}",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tools_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "GATE_INVOCATIONS": str(invocation_log),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert invocation_log.exists(), result.stdout + result.stderr
    assert invocation_log.read_text().splitlines() == [
        "make verify-generated-schemas",
        "make test-static",
    ], result.stdout + result.stderr


def test_public_makefile_keeps_registry_push_out_of_tracked_contract():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "192.168." not in makefile
    assert "DOCKER_REGISTRY" not in makefile
    assert "docker-push" not in makefile
    assert "docker-build-push" not in makefile
    assert "DOCKER_IMAGE ?= dtxwiki:$(DOCKER_TAG)" in makefile
    assert "-include Makefile.local" in makefile


def test_private_makefile_overlay_is_ignored_and_documented():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    readme_dev = (ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")

    assert "Makefile.local" in gitignore
    assert "Makefile.local" in readme_dev
    assert "registry.example.internal" in readme_dev
    assert "192.168." not in readme_dev


def test_local_dev_docs_use_postgres_only_compose_targets():
    checked_paths = [
        ROOT / "README.md",
        ROOT / "DEVELOPMENT.md",
        ROOT / "infra/local/.env.example",
        ROOT / "infra/local/docker-compose.yml",
    ]

    offenders = []
    for path in checked_paths:
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "make up",
            "make down",
            "PostgreSQL/Redis/MinIO",
            "DTXWIKI_REDIS_",
            "DTXWIKI_MINIO_",
            "MINIO_ROOT",
        ):
            if forbidden in text:
                offenders.append(f"{path.relative_to(ROOT)}: {forbidden}")

    assert offenders == []


def test_backend_target_runs_sqlite_media_lane_sequentially():
    """The known SQLite media race must not return to the xdist lane unnoticed."""

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert (
        "SQLITE_MEDIA_SERIAL_TESTS := "
        "backend/tests/integration/test_media_blob_gc.py "
        "backend/tests/integration/test_media_install_workflow.py"
    ) in makefile
    assert "SQLITE_MEDIA_PARALLEL_IGNORES := $(foreach test," in makefile
    assert (
        "pytest backend/tests -q -n $(PYTEST_WORKERS) --dist loadgroup "
        '-m "not release_scale and not release_migration" '
        "$(SQLITE_MEDIA_PARALLEL_IGNORES)"
    ) in makefile
    assert "pytest $(SQLITE_MEDIA_SERIAL_TESTS) -q" in makefile
    assert 'pytest backend/tests -q -m "not release_migration"' in makefile
    assert (
        "test-migrations:\n"
        "\t$(VENV_PYTHON) -m pytest backend/tests -q -n $(PYTEST_WORKERS) "
        "--dist loadgroup -m release_migration"
    ) in makefile
