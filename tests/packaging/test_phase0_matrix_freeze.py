"""Sprint_5 Phase_2 fix-round-3 (Bot-B5) — regression guard against the
re-introduction of Phase_0 client-matrix tentative language.

The Bot review enumerated 6 forbidden phrases (``treat as``, ``treated as``,
``likely``, ``If native``, ``If ... incomplete``, ``must be smoke-tested``,
``assume``, ``assumption``, ``to be confirmed``). Each is a Phase_0 §0
"freeze must be auditable" violation because it allows a row to be technically
present without committing to either a MEASURED or DEFERRED classification.

ADR A6 pair: ``test_matrix_has_no_tentative_phrases`` (negative, keyword pin)
+ ``test_matrix_has_at_least_one_measured_row`` (positive control). If the
matrix file is replaced by an empty stub, the positive control fails so the
guard cannot be silently neutralised.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = ROOT / "tests/fixtures/client-matrix.md"

# Sprint_5 Phase_2 fix-round-3 (Bot-B5) — tentative-language pattern set. The
# patterns are case-sensitive to avoid false positives on legitimate technical
# language ("likely" vs "Likely" in a markdown header is the same offence, but
# camelCase identifiers like ``ifNativeFlag`` would otherwise trigger).
_TENTATIVE_PATTERNS: tuple[str, ...] = (
    r"\btreat as\b",
    r"\btreated as\b",
    r"\blikely\b",
    r"\bIf native\b",
    r"\bmust be smoke-tested\b",
    r"\bIf [^\n]{0,40}\bincomplete\b",
    r"\bassume\b",
    r"\bassumption\b",
    r"\bto be confirmed\b",
)


def _matrix_text() -> str:
    assert MATRIX_PATH.exists(), (
        f"Phase_0 client matrix is missing at {MATRIX_PATH}; "
        "Bot-B5 fix-round-3 expected this file to exist on the current branch."
    )
    return MATRIX_PATH.read_text(encoding="utf-8")


def test_matrix_has_no_tentative_phrases():
    """Bot-B5 negative pin: none of the tentative phrases may appear anywhere
    in the matrix file body. The file's preamble references the phrase set by
    pointing at the original Bot review doc rather than quoting it inline."""
    text = _matrix_text()
    offending: list[tuple[str, str]] = []
    for pattern in _TENTATIVE_PATTERNS:
        for match in re.finditer(pattern, text):
            line_no = text.count("\n", 0, match.start()) + 1
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            if line_end == -1:
                line_end = len(text)
            offending.append(
                (f"{MATRIX_PATH.name}:{line_no}", text[line_start:line_end])
            )
    assert not offending, (
        "tentative phrases re-introduced into client-matrix.md "
        "(Bot-B5):\n" + "\n".join(f"  {loc}: {line!r}" for loc, line in offending)
    )


def test_matrix_has_at_least_one_measured_row():
    """ADR A6 positive control: an empty stub would silently pass the
    no-tentative-phrases check, so the matrix must contain at least one cell
    explicitly classified as MEASURED and at least one as DEFERRED. This
    keeps the guard from rotting into a no-op.
    """
    text = _matrix_text()
    assert re.search(r"\bMEASURED\b", text), (
        "matrix file present but contains no MEASURED row; freeze guard "
        "cannot distinguish a real classification from an empty stub."
    )
    assert re.search(r"\bDEFERRED\b", text), (
        "matrix file present but contains no DEFERRED row; the classifier "
        "must be visible so future readers know which cells need re-measurement."
    )


@pytest.mark.parametrize("pattern", _TENTATIVE_PATTERNS)
def test_tentative_pattern_compiles(pattern):
    """Smoke check on the pattern set itself (positive control on the guard
    machinery — a typo in the regex set would otherwise silently allow
    re-introduction of a forbidden phrase)."""
    re.compile(pattern)
