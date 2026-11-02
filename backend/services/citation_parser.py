"""Extract citation markers only from eligible rendered Markdown text."""

import hashlib
import re

from backend.context_media.domain.markdown_media import _is_escaped, _rendered_page_body
from backend.domain.document_reference import CitationOccurrence
from backend.services.context_links import _plain_text_nodes, extract_links

_MARKER = re.compile(r"@cite\{([^}\n]*)(\}|$)", re.MULTILINE)
MAX_CITATION_OCCURRENCES = 5000


class CitationSyntaxError(ValueError):
    pass


def extract_inline_citations(body: str) -> tuple[CitationOccurrence, ...]:
    rendered, offset = _rendered_page_body(body)
    candidates = [
        m for m in _MARKER.finditer(rendered) if not _is_escaped(rendered, m.start())
    ]
    if not candidates:
        return ()
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
            raise CitationSyntaxError(
                "An inline citation needs a complete internal document address"
            )
        result.append(CitationOccurrence(m[1], m.start() + offset, m.end() + offset))
    if len(result) > MAX_CITATION_OCCURRENCES:
        raise CitationSyntaxError(
            "A document supports at most 5000 inline citation occurrences"
        )
    return tuple(result)
