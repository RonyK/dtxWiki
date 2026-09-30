"""Every MCP tool must tell the caller *which* field it rejected.

An MCP client is a program, not a person: `validation_failed` with no detail
gives an agent nothing to correct, so it either retries the identical call or
gives up. Pydantic already knows the field, the rule and the offending
location; `validation_error_envelope` is the only thing that carries them into
the envelope. This test keeps a handler from quietly dropping back to the bare
form the next time one is written.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = ROOT / "backend" / "mcp"
SCHEMA_DIRS = (ROOT / "backend" / "schemas" / "mcp",)
SCHEMA_FILES = (ROOT / "backend" / "schemas" / "context_write.py",)


def _catches_validation_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return False
    caught = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    return any(
        isinstance(node, ast.Name) and node.id == "ValidationError" for node in caught
    )


def _calls(handler: ast.ExceptHandler) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_every_mcp_tool_reports_which_field_failed_validation():
    violations: list[str] = []

    for source_file in sorted(MCP_DIR.rglob("*_tools.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            if not _catches_validation_error(handler):
                continue
            calls = _calls(handler)
            if "error_envelope" not in calls:
                # Not a caller-facing answer. `_parse_cite_verification` uses
                # this shape to treat an unparseable optional block as absent.
                continue
            where = f"{source_file.relative_to(ROOT).as_posix()}:{handler.lineno}"
            if handler.name is None:
                violations.append(f"{where} discards the exception (no `as exc`)")
            elif "validation_error_envelope" not in calls:
                violations.append(
                    f"{where} answers without validation_error_envelope, "
                    "so the caller is not told which field failed"
                )

    assert not violations, "MCP validation handlers hide the cause:\n" + "\n".join(
        violations
    )


def _interpolates(node: ast.AST) -> bool:
    """A message built from the value being rejected rather than a constant."""

    if isinstance(node, ast.JoinedStr):
        return any(isinstance(part, ast.FormattedValue) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr == "format"
    return False


def test_a_validator_message_never_carries_the_value_it_rejected():
    """`include_input=False` is a hard requirement, and this is its back door.

    `validation_error_envelope` strips pydantic's own copy of the rejected
    input so a 1 MB body_markdown is not mirrored into the response and the
    persisted trace. A validator that raises `ValueError(f"bad: {value}")`
    puts it straight back, because the envelope copies `msg` verbatim.
    """

    sources = [path for directory in SCHEMA_DIRS for path in directory.rglob("*.py")]
    sources.extend(SCHEMA_FILES)
    violations: list[str] = []

    for source_file in sorted(sources):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            name = getattr(node.exc.func, "id", None)
            if name not in {"ValueError", "AssertionError"}:
                continue
            if any(_interpolates(arg) for arg in node.exc.args):
                violations.append(
                    f"{source_file.relative_to(ROOT).as_posix()}:{node.lineno}"
                )

    assert not violations, (
        "MCP input validators must raise constant messages; these build one "
        "from the rejected value, which reaches the client and the trace:\n"
        + "\n".join(violations)
    )
