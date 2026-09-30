"""Keep converter retry provenance and the legacy fallback allowlist aligned."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from backend.services.conversion_failure_policy import (
    LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES,
)

ROOT = Path(__file__).resolve().parents[2]
CONVERTER_ROOT = ROOT / "backend" / "converters"


def _literal_conversion_error_retryability() -> dict[str, frozenset[bool | None]]:
    observed: defaultdict[str, set[bool | None]] = defaultdict(set)
    for path in sorted(CONVERTER_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if name != "ConversionError":
                continue
            code = node.args[0] if node.args else None
            if code is None:
                code = next(
                    (
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg == "code"
                    ),
                    None,
                )
            if not isinstance(code, ast.Constant) or not isinstance(code.value, str):
                continue
            retryable: bool | None = False
            for keyword in node.keywords:
                if keyword.arg != "retryable":
                    continue
                if (
                    not isinstance(keyword.value, ast.Constant)
                    or type(keyword.value.value) is not bool
                ):
                    retryable = None
                else:
                    retryable = keyword.value.value
                break
            if retryable is not None:
                observed[code.value].add(retryable)
    return {code: frozenset(values) for code, values in observed.items()}


def test_converter_retryability_matches_legacy_manual_retry_allowlist() -> None:
    observed = _literal_conversion_error_retryability()
    retryable_codes = {code for code, values in observed.items() if True in values}
    false_only_codes = {code for code, values in observed.items() if values == {False}}
    dynamic_codes = {code for code, values in observed.items() if None in values}
    missing_retryable_codes = retryable_codes - LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES

    assert retryable_codes <= LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES, (
        "A converter started emitting a retryable error without registering its "
        f"legacy manual-retry behavior: {sorted(missing_retryable_codes)}"
    )
    assert false_only_codes.isdisjoint(LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES), (
        "The legacy allowlist would turn a retryable=False converter failure into "
        "a manual retry: "
        f"{sorted(false_only_codes & LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES)}"
    )
    assert dynamic_codes.isdisjoint(LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES), (
        "A converter code with runtime-dependent retryability cannot be classified "
        "from a legacy error code: "
        f"{sorted(dynamic_codes & LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES)}"
    )


def test_input_limit_exceeded_is_statically_nonretryable() -> None:
    observed = _literal_conversion_error_retryability()

    assert observed["input_limit_exceeded"] == {False}
    assert "input_limit_exceeded" not in LEGACY_MANUAL_RETRY_ALLOWED_ERROR_CODES
