"""Shared passive-profile checks for spreadsheet OOXML formulas."""

from __future__ import annotations

import re
from xml.etree import ElementTree

_EXTERNAL_FORMULA_FUNCTION = re.compile(
    r"(?<![A-Z0-9_.])(?:_XLFN\.)?"
    r"(?:WEBSERVICE|IMAGE|RTD|CALL|REGISTER\.ID|EXEC|SQL\.REQUEST)\s*\(",
    re.IGNORECASE,
)
_DDE_REFERENCE = re.compile(r"\|[^!\r\n]{0,512}!")
_EXTERNAL_WORKBOOK_REFERENCE = re.compile(r"\[[^\]\r\n]{1,255}\][^!\r\n]{0,255}!")
_SPREADSHEET_FORMULA_ELEMENTS = frozenset(
    {"definedName", "f", "formula", "formula1", "formula2"}
)


def contains_active_spreadsheet_formula(root: ElementTree.Element) -> bool:
    """Return whether an OOXML tree contains an external-capable formula.

    Ordinary formulas remain admissible and are omitted by the converter. This
    check is deliberately limited to formula constructs that can initiate an
    external lookup, native/add-in call, DDE exchange, or workbook link.
    """

    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in _SPREADSHEET_FORMULA_ELEMENTS:
            continue
        formula = node.text or ""
        if (
            _EXTERNAL_FORMULA_FUNCTION.search(formula)
            or _DDE_REFERENCE.search(formula)
            or _EXTERNAL_WORKBOOK_REFERENCE.search(formula)
        ):
            return True
    return False


__all__ = ["contains_active_spreadsheet_formula"]
