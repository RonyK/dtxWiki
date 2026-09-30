from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from backend.scripts import generate_workspace_lookup_baseline as generator
from tests.static import test_workspace_scoped_lookup_gate as gate

_EXISTING = "backend/existing.py|read|get|ContextPage|aaaaaaaaaaaaaaaa"
_NEW = "backend/new.py|read|get|ContextPage|bbbbbbbbbbbbbbbb"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_generator_refuses_to_bless_new_lookup_fingerprints(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({_EXISTING: 1}) + "\n", encoding="utf-8")
    # Patch the gate module, not `generator`: main() imports these two names
    # from it on every call rather than at module scope, because `tests` is not
    # in the wheel and an eager import makes the packaged module unimportable
    # (see the comment in main()). Patching the source is what that seam needs.
    monkeypatch.setattr(gate, "BASELINE_PATH", baseline)
    monkeypatch.setattr(
        gate,
        "workspace_lookup_call_sites",
        lambda: Counter({_EXISTING: 1, _NEW: 1}),
    )
    monkeypatch.setattr(sys, "argv", ["generator", "--write"])

    assert generator.main() == 1
    assert json.loads(baseline.read_text(encoding="utf-8")) == {_EXISTING: 1}


def test_generator_allows_reviewed_format_migration(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({_EXISTING: 1}) + "\n", encoding="utf-8")
    monkeypatch.setattr(gate, "BASELINE_PATH", baseline)
    monkeypatch.setattr(
        gate,
        "workspace_lookup_call_sites",
        lambda: Counter({_EXISTING: 1, _NEW: 1}),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["generator", "--write", "--allow-additions"],
    )

    assert generator.main() == 0
    assert json.loads(baseline.read_text(encoding="utf-8")) == {
        _EXISTING: 1,
        _NEW: 1,
    }


def test_generator_can_be_executed_by_file_path():
    result = subprocess.run(
        [
            sys.executable,
            str(
                _REPO_ROOT
                / "backend"
                / "scripts"
                / "generate_workspace_lookup_baseline.py"
            ),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "scoped keys," in result.stdout
    assert "lookup occurrences" in result.stdout
