"""The post-setup connection diagnostic must not report silence as success."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.cli import prod_setup
from backend.cli.commands import prod


def test_a_missing_smoke_script_is_skipped_not_passed(monkeypatch, capsys):
    """V15. "Nothing ran" and "everything passed" are different answers.

    This returned True when the script could not be found, so a packaging gap
    -- the one case where an operator most needs to hear that nothing was
    checked -- surfaced as a clean run.
    """

    monkeypatch.setattr(prod_setup, "share_file", lambda _path: None)

    outcome = prod._run_smoke("wiki.example.com")

    assert outcome == "skipped"
    assert outcome != "passed"
    assert "NOTHING was verified" in capsys.readouterr().err


def test_a_requested_ca_that_is_missing_fails_rather_than_falling_back(
    monkeypatch, tmp_path, capsys
):
    """A CA the operator named but that cannot be read is an error.

    Dropping it and re-running would verify against the system trust store
    instead -- a different question from the one that was asked -- and then
    report the answer as a pass.
    """

    script = tmp_path / "production_smoke.sh"
    script.write_text("exit 0\n", encoding="utf-8")
    monkeypatch.setattr(prod_setup, "share_file", lambda _path: script)

    def fail_if_called(*args, **kwargs):  # pragma: no cover - regression guard
        raise AssertionError("ran the diagnostic without the requested CA")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    outcome = prod._run_smoke(
        "wiki.internal", ca=Path(tmp_path / "absent-root-ca.crt")
    )

    assert outcome == "failed"
    assert "refusing to verify against a different trust store" in (
        capsys.readouterr().err
    )


def test_the_diagnostic_is_bounded_in_time(monkeypatch, tmp_path):
    """DNS included: an unreachable name must not hold the installer open."""

    script = tmp_path / "production_smoke.sh"
    script.write_text("exit 0\n", encoding="utf-8")
    monkeypatch.setattr(prod_setup, "share_file", lambda _path: script)

    seen: dict[str, object] = {}

    def capture(*args, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd="bash", timeout=1)

    monkeypatch.setattr(subprocess, "run", capture)

    outcome = prod._run_smoke("wiki.example.com")

    assert seen.get("timeout") == prod._SMOKE_TIMEOUT_SECONDS
    assert outcome == "failed"


@pytest.mark.counterexample(
    "_run_smoke returns 'skipped' for a missing script and 'passed' only for a "
    "clean run -- the outcome that used to be shared between them. Restoring "
    "the old `return True` makes the first half of this fail, and a diagnostic "
    "nobody has watched report 'nothing ran' is one you cannot trust when it "
    "says everything passed."
)
def test_skipped_and_passed_are_not_the_same_outcome(monkeypatch, tmp_path):
    monkeypatch.setattr(prod_setup, "share_file", lambda _path: None)
    assert prod._run_smoke("wiki.example.com") == "skipped"

    script = tmp_path / "production_smoke.sh"
    script.write_text("exit 0\n", encoding="utf-8")
    monkeypatch.setattr(prod_setup, "share_file", lambda _path: script)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        ),
    )
    assert prod._run_smoke("wiki.example.com") == "passed"
