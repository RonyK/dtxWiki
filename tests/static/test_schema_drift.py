import json
import os
import subprocess
from pathlib import Path

import pytest

from backend.scripts.export_mcp_schemas import build_mcp_schemas
from backend.scripts.export_openapi import build_openapi_schema

ROOT = Path(__file__).resolve().parents[2]
GENERATED_SCHEMA_PATHS = (
    "shared/schemas/rest/v1/openapi.json",
    "shared/schemas/mcp/v1",
)


def test_openapi_schema_artifact_is_current():
    expected = json.loads(
        (ROOT / "shared/schemas/rest/v1/openapi.json").read_text(encoding="utf-8")
    )

    assert build_openapi_schema() == expected


def _assert_mcp_artifacts_match(schema_dir: Path, generated: dict[str, dict]) -> None:
    stems = {path.stem for path in schema_dir.glob("*.json")}
    # Enumerating the directory, not just the registry, is what catches a
    # contract left behind by a removed tool; the stale/missing split tells the
    # operator to delete the JSON rather than re-register the tool they dropped.
    assert stems == set(generated), (
        "MCP schema artifact drift: "
        f"stale={sorted(stems - set(generated))} "
        f"missing={sorted(set(generated) - stems)}. Run "
        "`.venv/bin/python -m backend.scripts.export_mcp_schemas`."
    )
    for tool_name, schema in generated.items():
        expected = json.loads(
            (schema_dir / f"{tool_name}.json").read_text(encoding="utf-8")
        )
        assert schema == expected


def test_mcp_schema_artifacts_are_current():
    _assert_mcp_artifacts_match(ROOT / "shared/schemas/mcp/v1", build_mcp_schemas())


def test_mcp_artifact_check_rejects_contract_for_removed_tool(tmp_path):
    (tmp_path / "guide.json").write_text(json.dumps({"input": {}}), encoding="utf-8")
    (tmp_path / "write_page.json").write_text("{}", encoding="utf-8")

    with pytest.raises(AssertionError) as exc_info:
        _assert_mcp_artifacts_match(tmp_path, {"guide": {"input": {}}})

    assert "stale=['write_page']" in str(exc_info.value)


def _verify_generated_schemas_drift_check() -> str:
    # The check `make verify-generated-schemas` actually runs, lifted out of the
    # Makefile rather than retyped here, so the target and its test cannot drift
    # apart. The `$(MAKE) ...` lines are the recipe's other two steps.
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    header = "verify-generated-schemas:"
    assert header in lines, f"no `{header}` target in the Makefile"
    recipe: list[str] = []
    for line in lines[lines.index(header) + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line[1:].lstrip("@-+"))
    checks = [line for line in recipe if not line.startswith("$(MAKE)")]
    assert len(checks) == 1, f"expected one drift check in the recipe, got {checks}"
    for path in GENERATED_SCHEMA_PATHS:
        assert path in checks[0], f"drift check no longer covers {path}: {checks[0]}"
    # make writes a literal `$` as `$$`; the recipe is otherwise plain sh.
    return checks[0].replace("$$", "$")


def _sandboxed_env() -> dict[str, str]:
    # GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE in the ambient environment would
    # redirect these git calls at the real checkout -- and one of them stages
    # and commits. Strip them so the throwaway repo is the only repo in reach.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"))
    }


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            # A global commit hook or init template on the developer's machine
            # would fail these with a CalledProcessError that has nothing to do
            # with the gate under test.
            "-c",
            "core.hooksPath=",
            "-c",
            "init.templateDir=",
            *args,
        ],
        cwd=repo,
        env=_sandboxed_env(),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def committed_schema_repo(tmp_path: Path) -> Path:
    """A throwaway repo whose generated schema artifacts are committed and clean."""
    repo = tmp_path / "repo"
    (repo / "shared/schemas/mcp/v1").mkdir(parents=True)
    (repo / "shared/schemas/rest/v1").mkdir(parents=True)
    (repo / "shared/schemas/rest/v1/openapi.json").write_text("{}\n", encoding="utf-8")
    (repo / "shared/schemas/mcp/v1/guide.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "artifacts")
    return repo


def _run_drift_check(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "-c", _verify_generated_schemas_drift_check()],
        cwd=repo,
        env=_sandboxed_env(),
        capture_output=True,
        text=True,
    )


def test_generated_schema_gate_passes_on_a_clean_tree(committed_schema_repo):
    assert _run_drift_check(committed_schema_repo).returncode == 0


def test_generated_schema_gate_catches_an_untracked_new_contract(committed_schema_repo):
    # `git diff --exit-code` ignores untracked files, so registering a NEW tool
    # used to slip through: the exporter wrote its contract for the first time,
    # git diff saw nothing, and the gate certified the directory as current.
    probe = committed_schema_repo / "shared/schemas/mcp/v1/__probe_untracked.json"
    probe.write_text("{}\n", encoding="utf-8")

    result = _run_drift_check(committed_schema_repo)

    assert result.returncode != 0, (
        "make verify-generated-schemas is blind to an added, untracked contract: "
        f"{result.stdout}{result.stderr}"
    )
    assert "__probe_untracked.json" in result.stdout + result.stderr


def test_generated_schema_gate_catches_a_pruned_contract(committed_schema_repo):
    (committed_schema_repo / "shared/schemas/mcp/v1/guide.json").unlink()

    result = _run_drift_check(committed_schema_repo)

    assert result.returncode != 0
    assert "guide.json" in result.stdout + result.stderr


def test_generated_schema_gate_fails_when_git_itself_cannot_answer(tmp_path):
    """A gate that cannot read git must not report "current".

    The drift check reads `$(git status ...)` into a variable, and a command
    substitution discards git's exit status unless it is checked. Without that
    check, no .git (a source tarball, a docker build that excludes VCS metadata)
    or no git on PATH leaves `drift` EMPTY, the `if` does not fire, and the
    recipe exits 0 having verified nothing -- the same false-green shape the
    untracked-file fix was written to remove, re-entered through another door.
    The `git diff --exit-code` form this replaced exited 1 here.
    """

    not_a_repo = tmp_path / "not-a-repo"
    (not_a_repo / "shared/schemas/mcp/v1").mkdir(parents=True)
    (not_a_repo / "shared/schemas/rest/v1").mkdir(parents=True)
    (not_a_repo / "shared/schemas/rest/v1/openapi.json").write_text(
        "{}\n", encoding="utf-8"
    )

    result = _run_drift_check(not_a_repo)

    assert result.returncode != 0, (
        "the drift check certified the artifacts as current while git failed:\n"
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_generated_schema_gate_catches_a_rewritten_openapi_artifact(
    committed_schema_repo,
):
    (committed_schema_repo / "shared/schemas/rest/v1/openapi.json").write_text(
        '{"drift": true}\n', encoding="utf-8"
    )

    result = _run_drift_check(committed_schema_repo)

    assert result.returncode != 0
    assert "openapi.json" in result.stdout + result.stderr
