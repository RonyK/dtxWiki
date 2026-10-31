"""Keep Graph View's Node 22.18 prerequisite aligned across build entry points."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _require_node_22_18(spec: object, source: str) -> None:
    assert spec == "22.18.0", f"{source} must use Node 22.18.0, got {spec!r}"


def test_all_workflow_node_jobs_use_the_graph_toolchain_version():
    node_jobs: set[tuple[str, str]] = set()
    for workflow_path in (*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")):
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        for job_name, job in workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                if not str(step.get("uses", "")).startswith("actions/setup-node@"):
                    continue
                node_jobs.add((workflow_path.name, job_name))
                _require_node_22_18(
                    step.get("with", {}).get("node-version"),
                    f"{workflow_path.name}:{job_name}",
                )

    assert node_jobs >= {
        ("ci.yml", "frontend"),
        ("ci.yml", "e2e"),
        ("e2e-browsers.yml", "exhaustive"),
        ("release.yml", "build"),
        ("release-desktop.yml", "build-installers"),
    }


def test_local_node_pin_and_package_engine_match_ci():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert (ROOT / ".nvmrc").read_text(encoding="utf-8").strip() == "22.18.0"
    assert package["engines"]["node"] == "22.18.x"
    assert lock["packages"][""]["engines"] == package["engines"]


def test_frontend_node_types_match_runtime():
    frontend = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert frontend["devDependencies"]["@types/node"] == "22.18.0"
    assert lock["packages"]["frontend"]["devDependencies"]["@types/node"] == "22.18.0"
    assert lock["packages"]["frontend/node_modules/@types/node"]["version"] == "22.18.0"


@pytest.mark.counterexample(
    "A different or floating Node version violates the exact Graph toolchain pin"
)
@pytest.mark.parametrize("version", ["20", "22.18", "22.18.x", "22.18.1"])
def test_workflow_node_version_drift_is_rejected(version):
    with pytest.raises(AssertionError, match="must use Node 22.18"):
        _require_node_22_18(version, "fixture:e2e")
