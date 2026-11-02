"""Extract citation markers only from eligible rendered Markdown text."""

import hashlib
import re
from bisect import bisect_right

from backend.context_media.domain.markdown_media import _is_escaped, _rendered_page_body
from backend.domain.document_reference import CitationOccurrence
from backend.domain.input_policy import InputPolicyError
from backend.services.context_links import _plain_text_nodes, extract_links
from backend.services.read_errors import ReadValidationError

_MARKER = re.compile(r"@cite\{([^}\n]*)(\}|$)", re.MULTILINE)
MAX_CITATION_OCCURRENCES = 5000


class CitationSyntaxError(ReadValidationError, InputPolicyError):
    pass


def citation_token_spans(body: str) -> tuple[tuple[int, int], ...]:
    """Reserve marker bytes for citations before ordinary link normalization."""
    return tuple(match.span() for match in _MARKER.finditer(body))


def is_legacy_citation_key(value: str) -> bool:
    """Preserve historical keys and their anchors, never guess document IDs.

    A colon or slash before the fragment identifies an address-shaped input;
    malformed URLs and index refs cannot become legacy keys by adding #.
    """
    return bool(re.fullmatch(r"[^\s:/{}#?]+(?:#[^\s{}]*)?", value))


def extract_inline_citations(
    body: str, *, strict: bool = True
) -> tuple[CitationOccurrence, ...]:
    rendered, offset = _rendered_page_body(body)
    candidates = [
        m for m in _MARKER.finditer(rendered) if not _is_escaped(rendered, m.start())
    ]
    if not candidates:
        return ()
    wiki_spans = [
        (link.span.char_start, link.span.char_end)
        for link in extract_links(body)
        if link.kind == "wikilink"
    ]
    wiki_starts = [start for start, _end in wiki_spans]
    filtered = []
    for match in candidates:
        position = match.start() + offset
        index = bisect_right(wiki_starts, position) - 1
        if index < 0 or position >= wiki_spans[index][1]:
            filtered.append(match)
    candidates = filtered
    salt = hashlib.sha256(body.encode()).hexdigest()
    while f"dtxcitationprobe{salt}" in body:
        salt += "x"
    # Markers surround the original syntax: probing must not create original
    # text-node eligibility (e.g. inside an image, existing link or code).
    parts, cursor = [], 0
    for i, m in enumerate(candidates):
        parts.extend(
            (
                rendered[cursor : m.start()],
                f"dtxcitationprobe{salt}a{i}x",
                m[0],
                f"dtxcitationprobe{salt}b{i}x",
            )
        )
        cursor = m.end()
    parts.append(rendered[cursor:])
    nodes = _plain_text_nodes("".join(parts))
    qualified = re.compile(
        rf"dtxcitationprobe{salt}a(\d+)x[\s\S]*?"
        rf"dtxcitationprobe{salt}b\1x"
    )
    eligible = sorted(
        {int(match[1]) for node in nodes for match in qualified.finditer(node)}
    )
    # Reuse the common extractor's HTML/math/code exclusions as well as its
    # Markdown profile; a single probe handles all candidates in one parse.
    probe_parts, cursor = [], 0
    for i in eligible:
        m = candidates[i]
        probe_parts.extend(
            (
                rendered[cursor : m.start()],
                f"[citation](https://dtxcitationprobe{salt}.invalid/{i})",
            )
        )
        cursor = m.end()
    probe_parts.append(rendered[cursor:])
    hrefs = {link.href for link in extract_links("".join(probe_parts))}
    result = []
    for i in eligible:
        m = candidates[i]
        if f"https://dtxcitationprobe{salt}.invalid/{i}" not in hrefs:
            continue
        if m[2] != "}" or not m[1]:
            if strict:
                raise CitationSyntaxError(
                    "An inline citation needs a complete internal document address"
                )
            continue
        result.append(CitationOccurrence(m[1], m.start() + offset, m.end() + offset))
    if strict and len(result) > MAX_CITATION_OCCURRENCES:
        raise CitationSyntaxError(
            "A document supports at most 5000 inline citation occurrences"
        )
    return tuple(result)
