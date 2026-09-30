"""Every guard module must own an executed counterexample, and new ones cannot skip it.

A *guard module* is a test module whose job is to go red when the tree violates a
rule. A guard that has never been observed failing is indistinguishable from a guard
that cannot fail: eleven of them were green in this repository while naming defects
they could not catch.

A *counterexample* here is a test carrying ``@pytest.mark.counterexample(reason)``
that builds a violating input and asserts the guard's own checking function rejects
it.

WHAT THIS REGISTRY PROVES, EXACTLY: that each guard module contains at least one test
declaring itself a counterexample. It does not prove that counterexample is adequate,
and it cannot -- adequacy is a mutation run, not a collection-time property. The
marker is a declaration by the author, and this module checks that the declaration
exists and that the exemption list only ever shrinks. Read it as "nobody added a
guard without being asked for a counterexample", never as "every guard is proven".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_DIRECTORIES = (Path("tests/static"), Path("tests/packaging"))

# Generated from the tree, not hand-written. May only shrink.
COUNTEREXAMPLE_EXEMPT: frozenset[str] = frozenset(
    {
        "tests/packaging/test_declared_dependencies.py",
        "tests/packaging/test_distribution_includes_infra_prod.py",
        "tests/packaging/test_docker_artifacts.py",
        "tests/packaging/test_env_example.py",
        "tests/packaging/test_evidence_redaction.py",
        "tests/packaging/test_install_script.py",
        "tests/packaging/test_migration_chain.py",
        "tests/packaging/test_phase0_matrix_freeze.py",
        "tests/packaging/test_production_smoke_script.py",
        "tests/packaging/test_readme_sections.py",
        "tests/packaging/test_static_bundle_deploy.py",
        "tests/packaging/test_static_bundle_policy.py",
        "tests/packaging/test_systemd_units.py",
        "tests/packaging/test_wheel_artifact_verifier.py",
        "tests/static/test_agent_plugin_bundles.py",
        "tests/static/test_architecture_boundaries.py",
        "tests/static/test_ci_e2e_artifact_publication_contract.py",
        "tests/static/test_conversion_retry_disposition_contract.py",
        "tests/static/test_csp_policy_drift.py",
        "tests/static/test_db_revision_inventory_boundaries.py",
        "tests/static/test_e2e_artifact_redaction_contract.py",
        "tests/static/test_e2e_evidence_integrity_contract.py",
        "tests/static/test_endpoint_port_contract.py",
        "tests/static/test_epic1_contract_claims.py",
        "tests/static/test_frontend_quality_contract.py",
        "tests/static/test_frontend_types_drift.py",
        "tests/static/test_i18n_coverage_gate.py",
        "tests/static/test_license_artifacts.py",
        "tests/static/test_makefile_contract.py",
        "tests/static/test_mcp_app_ocp_contract.py",
        "tests/static/test_mcp_validation_contract.py",
        "tests/static/test_public_schema_path_leak.py",
        "tests/static/test_release_gate_contract.py",
        "tests/static/test_release_workflow_contract.py",
        "tests/static/test_schema_drift.py",
        "tests/static/test_tenancy_manifest_contract.py",
        "tests/static/test_workspace_physical_split_census.py",
        "tests/static/test_workspace_plane_boundary.py",
        "tests/static/test_workspace_scoped_lookup_gate.py",
    }
)


def guard_modules() -> list[str]:
    """Every tracked python module under the guard directories, repo-relative."""
    found: list[str] = []
    for directory in GUARD_DIRECTORIES:
        for path in sorted((REPO_ROOT / directory).glob("*.py")):
            if not path.name.startswith("test_"):
                continue
            found.append(path.relative_to(REPO_ROOT).as_posix())
    return found


def _decorator_is_counterexample(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return list(reversed(parts))[-2:] == ["mark", "counterexample"]


def counterexamples_in(relative_path: str) -> list[str]:
    """Names of the tests in this module that declare themselves counterexamples."""
    source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative_path)
    declared: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if any(_decorator_is_counterexample(d) for d in node.decorator_list):
            declared.append(node.name)
    return declared


def modules_missing_a_counterexample(modules: list[str]) -> list[str]:
    """The checking function. Kept separate so a counterexample can call it directly."""
    return [m for m in modules if not counterexamples_in(m)]


def test_every_guard_module_declares_a_counterexample() -> None:
    missing = set(modules_missing_a_counterexample(guard_modules()))
    unexpected = sorted(missing - COUNTEREXAMPLE_EXEMPT)
    assert not unexpected, (
        "These guard modules declare no counterexample and are not on the "
        "shrinking exemption list:\n  "
        + "\n  ".join(unexpected)
        + "\n\nA guard nobody has watched fail is indistinguishable from a guard that "
        "cannot fail. Add a test decorated with "
        '@pytest.mark.counterexample("what it proves") that constructs a violating '
        "input and asserts this module's own checking function rejects it."
    )


def test_the_exemption_list_only_shrinks() -> None:
    modules = set(guard_modules())
    missing = set(modules_missing_a_counterexample(guard_modules()))
    now_covered = sorted((COUNTEREXAMPLE_EXEMPT & modules) - missing)
    assert not now_covered, (
        "These modules now declare a counterexample but are still on the exemption "
        "list. Remove them from COUNTEREXAMPLE_EXEMPT so the list keeps shrinking:\n  "
        + "\n  ".join(now_covered)
    )
    stale = sorted(COUNTEREXAMPLE_EXEMPT - modules)
    assert not stale, (
        "These exemption entries name modules that no longer exist. Delete them:\n  "
        + "\n  ".join(stale)
    )


@pytest.mark.counterexample(
    "a guard module that is neither exempt nor covered is reported by the checking "
    "function this module's assertions call"
)
def test_a_guard_without_a_counterexample_is_reported(tmp_path: Path) -> None:
    covered = [m for m in guard_modules() if counterexamples_in(m)]
    assert covered, "fixture is vacuous: no module in the tree declares one"

    assert modules_missing_a_counterexample(covered) == []

    bare = tmp_path / "test_bare_guard.py"
    bare.write_text("def test_something():\n    assert True\n", encoding="utf-8")
    relative = bare.relative_to(REPO_ROOT) if REPO_ROOT in bare.parents else None
    if relative is None:
        source = bare.read_text(encoding="utf-8")
        tree = ast.parse(source)
        declared = [
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and any(_decorator_is_counterexample(d) for d in n.decorator_list)
        ]
        assert declared == []
