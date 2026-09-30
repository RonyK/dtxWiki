"""Structure-aware Markdown chunker with contextual chunk headers.

Splits a Markdown body along its heading hierarchy (the highest-leverage move,
verified best practice), preserving char offsets into the original so a hit can
be highlighted / cited. Each chunk carries a ``heading_path`` breadcrumb
(``Title › Section › Subsection``) that is prepended to the embedding input
(contextual chunk header) — see ``embedding_input`` — without being counted in
the offsets. Oversize sections are windowed with overlap at paragraph/sentence
boundaries. Tuning is two settings (target/overlap); no heuristic framework.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.utils.markdown_outline import parse_outline

_BREADCRUMB = " › "  # " › "

DEFAULT_TARGET_CHARS = 1500
DEFAULT_OVERLAP_CHARS = 200


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading_path: str
    char_start: int
    char_end: int
    text: str
    token_count: int


def embedding_input(chunk: Chunk) -> str:
    """The text actually embedded: the breadcrumb prepended to the chunk body
    (contextual chunk header). The stored text + offsets stay original."""
    if chunk.heading_path:
        return f"{chunk.heading_path}\n\n{chunk.text}"
    return chunk.text


def chunk_markdown(
    body: str,
    *,
    title: str | None = None,
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    chunks: list[Chunk] = []
    ordinal = 0
    outline = parse_outline(body)
    heading_sections = [
        section for section in outline.sections if section.kind == "heading"
    ]
    first_heading_start = (
        heading_sections[0].char_start if heading_sections else len(body)
    )
    legacy_sections: list[tuple[str, int, int]] = []
    if first_heading_start > 0 or not heading_sections:
        legacy_sections.append((title or "", 0, first_heading_start))
    for section in heading_sections:
        parts = ([title] if title else []) + list(section.heading_path)
        heading_path = _BREADCRUMB.join(part for part in parts if part)
        # Deliberately retain the flat span. _windows() requires disjoint
        # sections; subtree_char_end is reserved for future section editing.
        legacy_sections.append(
            (heading_path, section.char_start, section.char_end)
        )

    for heading_path, start, end in legacy_sections:
        section = body[start:end]
        for sub_start, sub_end in _windows(section, target_chars, overlap_chars):
            abs_start = start + sub_start
            abs_end = start + sub_end
            text = body[abs_start:abs_end]
            if not text.strip():
                continue
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    heading_path=heading_path,
                    char_start=abs_start,
                    char_end=abs_end,
                    text=text,
                    token_count=len(text.split()),
                )
            )
            ordinal += 1
    if not chunks and body.strip():
        chunks.append(
            Chunk(0, title or "", 0, len(body), body, len(body.split()))
        )
    return chunks


def _windows(text: str, target: int, overlap: int):
    n = len(text)
    if n <= target:
        yield (0, n)
        return
    start = 0
    while start < n:
        end = min(start + target, n)
        if end < n:
            lo = max(start + target - overlap, start + 1)
            boundary = _find_boundary(text, lo, end)
            if boundary > start:
                end = boundary
        yield (start, end)
        if end >= n:
            break
        start = max(end - overlap, start + 1)


def _find_boundary(text: str, lo: int, hi: int) -> int:
    window = text[lo:hi]
    for separator in ("\n\n", "\n", ". ", " "):
        position = window.rfind(separator)
        if position != -1:
            return lo + position + len(separator)
    return hi
