import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_backend_static_bundle_is_ignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "backend/static/" in gitignore


def test_backend_static_bundle_is_not_tracked():
    if not (ROOT / ".git").exists():
        pytest.skip("requires a Git checkout")

    result = subprocess.run(
        ["git", "ls-files", "backend/static"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == ""


def test_packaging_still_includes_generated_static_bundle():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert '"static/**/*"' in pyproject
    assert "recursive-include backend/static *" in manifest
    assert "build-wheel: bundle-frontend" in makefile
    # --fresh matters: without it the wheel accumulates every previous build's
    # chunks. The merging form is for a destination that is being served. The
    # synced environment runs it, not whatever `python3` is first on PATH.
    assert (
        "$(VENV_PYTHON) scripts/deploy_static_bundle.py deploy --fresh "
        "frontend/dist backend/static"
    ) in makefile
    assert "rm -rf backend/static" not in makefile
    assert "rm -rf build dtxwiki.egg-info" in makefile
    assert 'archive="dist/prev/$$(date -u +%Y%m%dT%H%M%SZ)-$$$$"' in makefile
    assert 'mv "$$artifact" "$$archive"/' in makefile
