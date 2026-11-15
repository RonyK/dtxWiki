"""Source-aware Markdown destination decoding and link rewrites.

The parser's one-pass escape rules establish URL identity. Source offsets keep
the untouched URL suffix and Markdown syntax independent of that identity.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from string import punctuation
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.common.utils import UNESCAPE_ALL_RE, replaceEntityPattern

from backend.context_media.domain.markdown_media import SourceSpan, _rendered_page_body


@dataclass(frozen=True)
class DecodedDestination:
    text: str
    offsets: tuple[int, ...]

    @property
    def suffix_start(self) -> int:
        return min(
            (
                index
                for index in (self.text.find("?"), self.text.find("#"))
                if index >= 0
            ),
            default=len(self.text),
        )


def decode_destination(raw: str, *, markdown: bool = True) -> DecodedDestination:
    """Match MarkdownIt's unescapeAll in one pass, retaining source boundaries.

    Autolink URLs and citation addresses have their own literal grammars. In
    particular, escaped entities must never acquire a second decoding pass.
    """
    if not markdown:
        return DecodedDestination(raw, tuple(range(len(raw) + 1)))
    pieces = []
    offsets = []
    cursor = 0
    for match in UNESCAPE_ALL_RE.finditer(raw):
        pieces.append(raw[cursor : match.start()])
        offsets.extend(range(cursor, match.start()))
        decoded = match.group(1) or replaceEntityPattern(match.group(), match.group(2))
        pieces.append(decoded)
        offsets.extend([match.start()] * len(decoded))
        cursor = match.end()
    pieces.append(raw[cursor:])
    offsets.extend(range(cursor, len(raw) + 1))
    return DecodedDestination("".join(pieces), tuple(offsets))


@dataclass(frozen=True)
class LinkSource:
    kind: str
    syntax_span: SourceSpan
    destination_span: SourceSpan
    raw_destination: str
    decoded: DecodedDestination
    uses: int = 1


@dataclass(frozen=True)
class LinkRewrite:
    source: LinkSource
    href: str
    label: str
    preserve_suffix: bool = True


def escape_link_label(title: str) -> str:
    """Serialize a literal label with CommonMark ASCII-punctuation escapes.

    Escape the complete punctuation alphabet rather than guessing which
    delimiters the page's inline extensions recognize, including math dollars.
    This affects only label syntax; it never changes the destination URL.
    """
    return "".join("\\" + char if char in punctuation else char for char in title)


def _encode_destination(href: str) -> str:
    return (
        href.replace("&", "&amp;")
        .replace("\\", "\\\\")
        .replace("<", "%3C")
        .replace(">", "%3E")
    )


def _destination_replacement(change: LinkRewrite) -> tuple[str, str]:
    source = change.source
    original, target = urlsplit(source.decoded.text), urlsplit(change.href)
    if (
        change.preserve_suffix
        and source.kind == "markdown"
        and (original.query, original.fragment) == (target.query, target.fragment)
    ):
        start = source.decoded.suffix_start
        raw_suffix = source.raw_destination[source.decoded.offsets[start] :]
        prefix = target._replace(query="", fragment="").geturl()
        return _encode_destination(prefix) + raw_suffix, prefix + source.decoded.text[
            start:
        ]
    return _encode_destination(change.href), change.href


def _unescaped_bang_before(body: str, start: int) -> bool:
    if not start or body[start - 1] != "!":
        return False
    cursor = start - 2
    while cursor >= 0 and body[cursor] == "\\":
        cursor -= 1
    return (start - 2 - cursor) % 2 == 0


def _link_image_inventory(parser: MarkdownIt, body: str):
    links = Counter()
    images = Counter()
    rendered, _ = _rendered_page_body(body)
    for token in parser.parse(rendered):
        for child in token.children or ():
            if child.type == "link_open":
                links[child.attrGet("href")] += 1
            elif child.type == "image":
                images[child.attrGet("src")] += 1
    return links, images


def apply_link_rewrites(
    body: str, changes: list[LinkRewrite], *, parser: MarkdownIt
) -> tuple[str, tuple[int, ...]]:
    """Emit one source patch batch and check link/image destination inventory.

    This CommonMark check detects changed link/image destinations and counts.
    It does not prove label or inline-extension equivalence; literal-label
    serialization and the actual reader's contract fixtures cover those.

    Ambiguous overlapping patches or an unexpected parser result retain the
    source, matching the existing authored-link writer's safe fallback.
    """
    if not changes:
        return body, ()
    expected_links, expected_images = _link_image_inventory(parser, body)
    edits = []
    for index, change in enumerate(changes):
        source = change.source
        span = source.destination_span
        if span.text(body) != source.raw_destination:
            return body, ()
        start, end = span.char_start, span.char_end
        if source.kind == "citation":
            replacement, href = change.href, change.href
        else:
            replacement, href = _destination_replacement(change)
        if source.kind in {"wikilink", "autolink", "literal_autolink"}:
            span = source.syntax_span
            start, end = span.char_start, span.char_end
            destination = replacement
            if any(character in destination for character in " ()\t\r\n"):
                destination = f"<{destination}>"
            replacement = f"[{escape_link_label(change.label)}]({destination})"
            if _unescaped_bang_before(body, start):
                start -= 1
                replacement = r"\!" + replacement
        if source.kind != "citation":
            if source.kind not in {"wikilink", "literal_autolink"}:
                expected_links[parser.normalizeLink(source.decoded.text)] -= source.uses
            expected_links[parser.normalizeLink(href)] += source.uses
        edits.append((start, end, replacement, index))
    parts = []
    cursor = 0
    accepted = []
    for start, end, replacement, index in sorted(edits):
        if start < cursor:
            return body, ()
        parts.extend((body[cursor:start], replacement))
        cursor = end
        accepted.append(index)
    parts.append(body[cursor:])
    rewritten = "".join(parts)
    links, images = _link_image_inventory(parser, rewritten)
    if (
        any(count < 0 for count in expected_links.values())
        or +expected_links != links
        or expected_images != images
    ):
        return body, ()
    return rewritten, tuple(accepted)
