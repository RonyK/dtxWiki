"""The default release gate must run from a clean clone with tracked inputs.

Private evidence checks are opt-in and require explicit input paths. Static
inspection and a clean-checkout execution cover the default input set.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from backend.tests.utils.environment import child_env_without_dtxwiki_config

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "release_gate_check.sh"

# `$ROOT_DIR/<path>` occurrences that are deliberately allowed to be absent: the
# interpreter probe is guarded by `[ -x ... ]` and falls back to python3/python.
INTERPRETER_PROBES = frozenset(
    {
        ".venv/bin/python",
        ".venv/Scripts/python.exe",
    }
)

# Helpers the public half invokes by repo-relative path after `cd "$ROOT_DIR"`.
# They are not `$ROOT_DIR/`-prefixed, so the sweep below cannot see them.
PUBLIC_GATE_HELPERS = (
    "scripts/check-i18n-coverage.mjs",
    "tests/packaging/test_evidence_redaction.py",
)

_DEFAULT_PATH_RE = re.compile(r"\$\{[A-Za-z0-9_]+:-\$ROOT_DIR/([^}\"]+)\}")
_LITERAL_PATH_RE = re.compile(r"\$ROOT_DIR/([A-Za-z0-9._/-]+)")


def _gate_text() -> str:
    return GATE.read_text(encoding="utf-8")


def _default_inputs() -> list[str]:
    text = _gate_text()
    paths = set(_DEFAULT_PATH_RE.findall(text))
    paths |= set(_LITERAL_PATH_RE.findall(text))
    return sorted(path for path in paths if path not in INTERPRETER_PROBES)


def _is_ignored(path: str) -> bool:
    return (
        subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=ROOT,
            capture_output=True,
        ).returncode
        == 0
    )


def _is_tracked(path: str) -> bool:
    return (
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "-z", "--", path],
            cwd=ROOT,
            capture_output=True,
        ).returncode
        == 0
    )


def test_public_gate_reads_only_inputs_a_clean_clone_has() -> None:
    """Static twin of the clean-checkout run: fast, and needs no node/pytest."""

    candidates = _default_inputs() + list(PUBLIC_GATE_HELPERS)
    assert candidates, "no default inputs parsed from the gate (regex drift?)"

    offenders = sorted(
        path for path in candidates if _is_ignored(path) or not _is_tracked(path)
    )
    assert offenders == [], (
        "scripts/release_gate_check.sh reads inputs by default that a clean "
        f"clone does not have: {offenders}. Private acceptance evidence "
        "(operator logs, acceptance overviews and remote client setup) "
        "must be supplied explicitly via --with-evidence, never defaulted to a "
        "repository path that is git-ignored or untracked."
    )


def test_public_gate_names_no_private_evidence_default() -> None:
    """The specific paths that made the gate unrunnable must not come back."""

    forbidden = ("doc/Epic-0", "doc/setup", "internal/audit")
    defaults = _default_inputs()
    offenders = sorted(
        path
        for path in defaults
        if any(path.startswith(prefix) for prefix in forbidden)
    )
    assert offenders == [], (
        "The release gate must not default any input to private evidence "
        f"paths: {offenders}."
    )


def test_evidence_gate_refuses_to_run_without_explicit_inputs(tmp_path) -> None:
    """Half-configured is a usage error (exit 2), not a release verdict."""

    env = child_env_without_dtxwiki_config()
    result = subprocess.run(
        ["bash", str(GATE), "--evidence-dir", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2, (
        "A partially configured evidence gate must exit 2 (not configured), "
        f"which is distinct from 1 (evidence rejected). Got {result.returncode}:"
        f"\n{result.stderr}"
    )
    assert "--setup-doc" in result.stderr
    assert "--phase5-overview" in result.stderr
    assert "--sprint5-overview" in result.stderr
    assert "no repository defaults" in result.stderr


def test_evidence_gate_is_opt_in_and_skipped_by_default(tmp_path) -> None:
    env = child_env_without_dtxwiki_config()
    env["DTXWIKI_PYTHON"] = sys.executable
    result = subprocess.run(
        ["bash", str(GATE)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert "SKIP: release evidence checks" in result.stdout, result.stdout
    assert "missing file:" not in result.stderr, result.stderr


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required for the gate's i18n coverage check",
)
def test_public_gate_runs_on_a_clean_checkout(tmp_path) -> None:
    """The load-bearing one: build the file set a clone gets, then run the gate.

    `git archive HEAD` gives the committed file set; working-tree edits to
    tracked files are laid over it so the test judges the code as it will be
    committed rather than the last commit.
    """

    clean = tmp_path / "clean"
    clean.mkdir()
    archive = subprocess.run(
        ["git", "archive", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(clean, filter="data")

    modified = subprocess.run(
        ["git", "diff", "--name-only", "-z", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    for rel in (entry for entry in modified.stdout.split("\0") if entry):
        source = ROOT / rel
        target = clean / rel
        if not source.is_file():
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        target.chmod(source.stat().st_mode)

    env = child_env_without_dtxwiki_config()
    env["DTXWIKI_PYTHON"] = sys.executable
    result = subprocess.run(
        ["bash", str(clean / "scripts/release_gate_check.sh")],
        cwd=clean,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert "missing file:" not in result.stderr, (
        "The default release gate demands an input a clean checkout does not "
        f"have:\n{result.stderr}"
    )
    assert result.returncode == 0, result.stderr
    assert "Release gate check: OK" in result.stdout
