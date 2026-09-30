"""The wheel's manifest must describe what the wheel actually imports.

pyproject.toml already states this policy for `httpcore` and `idna`: a
distribution imported directly is declared, never inherited through whatever
happens to require it today. Nothing enforced it, so three more accumulated --
`jsonschema` (declared only in the `dev` extra while
backend/mcp/protocol/output_validation.py imports it at runtime), plus
`starlette` and `anyio` in no group at all. All three resolved anyway, because
`mcp` and `fastapi` pull them in; the guarantee was accidental, and an upstream
release that drops one turns it into an ImportError at startup.

The scan is deliberately limited to EAGER imports -- module scope, not inside a
function -- because that is what fails at import time. backend/converters/'s
h5py/pyarrow/pypdf are imported inside their converters on purpose and degrade
to `converter_dependency_missing`; they are optional and must stay undeclared.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from collections import defaultdict
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

# Shipped by the wheel (`[tool.setuptools.packages.find] include = ["backend*"]`)
# or provided by the interpreter.
FIRST_PARTY = frozenset({"backend"})


def _normalize(name: str) -> str:
    """PEP 503 normalization, enough for the forms used in our manifest."""

    return name.lower().replace("_", "-").replace(".", "-")


def _requirement_name(requirement: str) -> str:
    head = requirement.strip()
    for separator in ("[", ">", "<", "=", "!", "~", ";", " "):
        head = head.split(separator)[0]
    return _normalize(head)


def declared_distributions() -> set[str]:
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {_requirement_name(r) for r in manifest["project"]["dependencies"]}


def _eager_import_roots(tree: ast.Module) -> set[str]:
    """Top-level module names imported when the module is imported.

    Everything inside a `def`/`async def` is skipped -- that is a deliberate
    deferral, which is how the optional converters and
    backend/scripts/generate_workspace_lookup_baseline.py's `tests` import stay
    legal. `if TYPE_CHECKING:` is skipped for the same reason: it never runs.
    """

    roots: set[str] = set()

    def walk(nodes) -> None:
        for node in nodes:
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # A relative import (level > 0) is first-party by construction.
                if node.level == 0 and node.module:
                    roots.add(node.module.split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            elif isinstance(node, ast.If) and _is_type_checking(node.test):
                walk(node.orelse)
            elif isinstance(node, (ast.If, ast.Try, ast.ClassDef, ast.With)):
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(node, field, []) or [])
                for handler in getattr(node, "handlers", []):
                    walk(handler.body)

    walk(tree.body)
    return roots


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def eager_third_party_importers() -> dict[str, list[str]]:
    """{imported top-level module: [repo-relative importing files]}."""

    importers: dict[str, list[str]] = defaultdict(list)
    for path in sorted(BACKEND.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if relative.parts[1] == "tests":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for root in _eager_import_roots(tree):
            if root in sys.stdlib_module_names or root in FIRST_PARTY:
                continue
            importers[root].append(str(relative))
    return dict(importers)


def test_every_eagerly_imported_distribution_is_declared():
    declared = declared_distributions()
    provided = packages_distributions()

    undeclared = {}
    for module, files in sorted(eager_third_party_importers().items()):
        distributions = provided.get(module)
        if not distributions:
            continue  # covered by the unresolvable test below
        if any(_normalize(d) in declared for d in distributions):
            continue
        undeclared[module] = (sorted(distributions), files[:3])

    assert not undeclared, (
        "backend/** imports these at module scope, but no distribution "
        "providing them is in [project.dependencies]. They resolve today only "
        "as transitives of something else, which is exactly what the httpcore/"
        "idna comments in pyproject.toml forbid relying on:\n"
        + "\n".join(
            f"  {module}: provided by {dists}, imported by {files}"
            for module, (dists, files) in undeclared.items()
        )
    )


def test_no_packaged_module_eagerly_imports_the_test_tree():
    """`tests` is not in the wheel, so importing it at module scope makes the
    packaged module unimportable.

    This was a live failure: `import
    backend.scripts.generate_workspace_lookup_baseline` raised
    ModuleNotFoundError from any cwd outside this checkout, which the desktop
    PyInstaller build reaches through --collect-submodules=backend.
    """

    importers = eager_third_party_importers()
    offenders = sorted(importers.get("tests", []))

    assert not offenders, (
        "these ship inside the wheel but import the test tree at module "
        f"scope, so they cannot be imported from an install: {offenders}"
    )


def test_every_eagerly_imported_module_resolves_to_an_installed_distribution():
    """A module no installed distribution provides is either a typo or a
    vendored package the wheel does not carry -- both are packaging bugs."""

    provided = packages_distributions()
    unresolvable = {
        module: files[:3]
        for module, files in sorted(eager_third_party_importers().items())
        if not provided.get(module)
    }

    assert not unresolvable, (
        "no installed distribution provides these, so nothing can declare "
        f"them: {unresolvable}"
    )
