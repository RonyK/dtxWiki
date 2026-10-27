"""Index refs: compact, system-internal targets in a get_index response.

``dtxwiki://index/<kind>/<uuid>`` names a target inside the workspace the
caller passed explicitly. It carries no workspace and is never a browser or
answer link; readers resolve it only within that workspace's scoped reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

IndexRefKind = Literal["folder", "context", "library", "agent"]

INDEX_REF_PREFIX = "dtxwiki://index/"
_INDEX_REF = re.compile(
    r"dtxwiki://index/(folder|context|library|agent)/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_MARKUP = re.compile(r"([\\\[\]<>])")
PAGE_INPUT_REF_GUIDANCE = (
    "Index refs work only in read_concept (context/library) and get_index "
    "(folder). Pass the page UUID or read_concept's target_id/stable_url."
)


class IndexRefInputError(ValueError):
    """An index ref sent to a tool input that does not take one."""


@dataclass(frozen=True)
class IndexRef:
    kind: IndexRefKind
    target_id: UUID


def format_index_ref(kind: IndexRefKind, target_id: UUID | str) -> str:
    return f"{INDEX_REF_PREFIX}{kind}/{str(target_id).lower()}"


def parse_index_ref(value: object) -> IndexRef | None:
    """Exact form only: no trimming, decoding, case folding or extra parts."""
    if not isinstance(value, str):
        return None
    match = _INDEX_REF.fullmatch(value)
    if match is None:
        return None
    return IndexRef(match[1], UUID(match[2]))  # type: ignore[arg-type]


def looks_like_index_ref(value: object) -> bool:
    return isinstance(value, str) and value.startswith(INDEX_REF_PREFIX)


def index_line(ref: str, title: str, *, suffix: str = "", summary: str = "") -> str:
    """One index entry; the ref is always the first field of the line.

    Titles and summaries are untrusted text. Collapsing them to a single line
    keeps them from starting a new entry, the fixed first field keeps any
    ref-shaped text inside them from being read as the entry's ref, and
    escaping link and HTML syntax keeps a Markdown consumer from rendering
    them as a link or tag.
    """
    line = f"- {ref} {plain_text(title)}{suffix}"
    summary = plain_text(summary)
    return f"{line} — {summary}" if summary else line


def one_line(text: str) -> str:
    """Untrusted text as one line: controls and line breaks become spaces."""
    return " ".join(_CONTROL.sub(" ", text).split())


def plain_text(text: str) -> str:
    """One line with link and HTML syntax escaped, for Markdown consumers."""
    return _MARKUP.sub(r"\\\1", one_line(text))
