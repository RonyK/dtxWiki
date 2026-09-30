"""A subprocess env built by hand is how the real data directory gets written.

The repo-root conftest redirects ``DTXWIKI_DATA_DIR`` per process, and
``pytest_runtest_setup`` fails any test that would resolve to the real
installation -- but neither can see inside a child process. A test that builds
its child's environment with a hand-rolled ``{k: v for k, v in os.environ
.items() if not k.startswith("DTXWIKI_")}`` strips the redirect along with the
configuration, and the child bootstraps into the developer's own
``~/.dtxwiki``. That is exactly the pollution this suite was changed to stop,
and it is invisible at runtime: the child succeeds, the test passes, and six
hundred stray directories accumulate.

``backend/tests/utils/environment.child_env_without_dtxwiki_config()`` does the
same strip and puts ``DTXWIKI_DATA_DIR`` back. This guard requires every such
comprehension to go through it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.static._python_ast import ROOT, parse_python_file

SEARCH_ROOTS = (Path("backend/tests"), Path("tests"))

# The helper itself is the one place the pattern is allowed to live.
ALLOWED_FILES = frozenset({Path("backend/tests/utils/environment.py")})


def _strips_dtxwiki_env(tree: ast.Module) -> bool:
    """True when the module filters ``os.environ`` on a ``DTXWIKI_`` prefix.

    Matches the comprehension shape only -- a named, unambiguous AST node --
    rather than trying to decide what any env-shaped dict is for.
    """

    for node in ast.walk(tree):
        if not isinstance(node, ast.DictComp | ast.SetComp | ast.ListComp):
            continue
        iterates_environ = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "items"
            and isinstance(call.func.value, ast.Attribute)
            and call.func.value.attr == "environ"
            for generator in node.generators
            for call in ast.walk(generator.iter)
        )
        if not iterates_environ:
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "startswith"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == "DTXWIKI_"
            ):
                return True
    return False


def _offending_files(root: Path = ROOT) -> list[str]:
    offenders: list[str] = []
    for search_root in SEARCH_ROOTS:
        for path in sorted((root / search_root).rglob("*.py")):
            relative = path.relative_to(root)
            if relative in ALLOWED_FILES:
                continue
            if _strips_dtxwiki_env(parse_python_file(path, root=root)):
                offenders.append(relative.as_posix())
    return offenders


def test_subprocess_envs_go_through_the_shared_helper():
    offenders = _offending_files()

    assert offenders == [], (
        "These files strip DTXWIKI_* out of a child process environment by "
        "hand, which also removes the data-directory redirect and sends the "
        "child at the real ~/.dtxwiki. Use "
        "backend.tests.utils.environment.child_env_without_dtxwiki_config() "
        f"instead: {offenders}"
    )


@pytest.mark.counterexample(reason="a hand-rolled strip must be rejected")
def test_guard_rejects_a_hand_rolled_strip(tmp_path):
    fixture = tmp_path / "backend" / "tests" / "cli"
    fixture.mkdir(parents=True)
    (fixture / "test_spawns_a_child.py").write_text(
        "import os\n"
        "import subprocess\n"
        "\n"
        "def test_child():\n"
        "    env = {\n"
        "        key: value\n"
        "        for key, value in os.environ.items()\n"
        '        if not key.startswith("DTXWIKI_")\n'
        "    }\n"
        "    subprocess.run(['true'], env=env, check=True)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()

    assert _offending_files(root=tmp_path) == [
        "backend/tests/cli/test_spawns_a_child.py"
    ]
