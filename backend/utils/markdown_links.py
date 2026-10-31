"""Extract links that the page Markdown renderer exposes to readers."""

from __future__ import annotations

import html
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Sequence
from copy import copy, deepcopy
from dataclasses import dataclass
from functools import lru_cache
from heapq import merge

from markdown_it import MarkdownIt
from markdown_it.common.utils import isMdAsciiPunct, isPunctChar, isWhiteSpace
from markdown_it.parser_inline import ParserInline
from markdown_it.rules_inline import StateInline
from markdown_it.rules_inline.autolink import autolink as parse_autolink
from markdown_it.rules_inline.link import link as parse_link
from markdown_it.rules_inline.state_inline import Scanned
from markdown_it.rules_inline.text import text as parse_text
from markdown_it.token import Token
from markdown_it.utils import EnvType

from backend.context_media.domain.rendered_markdown import (
    RENDERED_BLOCKS,
    RENDERED_MARKDOWN,
    RenderedMarkdownAnalysis,
)

_MARKDOWN_LINK_PARSER = RENDERED_MARKDOWN
_WIKILINK = re.compile(r"\[\[([^\]\n|]+)(?:\|([^\]\n]+))?\]\]")
_HTML_ENTITY = re.compile(
    r"&(?:#[xX][0-9A-Fa-f]{1,8}|#[0-9]{1,8}|[A-Za-z][A-Za-z0-9]{1,31});"
)
_ASCII_PUNCTUATION_ESCAPE = re.compile(
    r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])"
)
_TEXT_DECODE_UNIT = re.compile(
    rf"{_ASCII_PUNCTUATION_ESCAPE.pattern}|{_HTML_ENTITY.pattern}"
)
_CUSTOM_TOKEN_OPENER = re.compile(r"(?=(\[\[|==))")
_WIKILINK_TARGET_END = re.compile(r"[\]\n|]")
_WIKILINK_ALIAS_END = re.compile(r"[\]\n]")
_HIGHLIGHT_END = re.compile(r"[=\n]")
_INLINE_TEXT_SPLIT_MARKER = re.compile(r"[*_~]")
_REFERENCE_TEXT_SPLIT = re.compile(r"(?<!\[)\[[^\[\]\n]{1,999}\](?!\])")
_READER_URL_OPENER = re.compile(r"(?i)(https?://|www\.)")
_READER_URL_END = re.compile(r"[ \t\r\n]")
_RAW_READER_URL_END = re.compile(r"[\s<]")
_READER_EMAIL_LITERAL = re.compile(
    r"(?<![-.A-Za-z0-9_+])([-.A-Za-z0-9_+]+)@([-A-Za-z0-9_]+(?:\.[-A-Za-z0-9_]+)+)"
)
_READER_URL_TRAIL = re.compile(r"[!\"&'),.:;<>?\]}]+$")
_RAW_READER_URL_TRAIL = re.compile(r"[!\"')*,.:;?\]_~]+$")
_DOLLAR_RUN = re.compile(r"\$+")
_WIKILINK_WWW_LITERAL = re.compile(r"(?i)(?<![A-Za-z0-9])www\.")
_WIKILINK_EMAIL_LITERAL = re.compile(
    r"(?i)[A-Za-z0-9+._-]+@[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+"
)
_URL_SCHEME_SEPARATOR = re.compile(r"://")
# A source token starts after whitespace or angle syntax, and where bracket
# syntax hands the following character to a new construct.
_TOKEN_BREAK = re.compile(r"[\s<>]")
_BRACKET_CALL_BREAK = re.compile(r"\](?=[(\[])")


@dataclass(frozen=True, slots=True)
class RenderedMarkdownLink:
    destination: str
    source_start: int
    source_end: int


@dataclass(frozen=True, slots=True)
class RenderedWikilink:
    target: str
    source_start: int
    source_end: int


@dataclass(frozen=True, slots=True)
class _LinkCandidate:
    syntax_start: int
    syntax_end: int
    snippet_start: int
    snippet_end: int
    marker_position: int


@dataclass(frozen=True, slots=True)
class _BracketUnit:
    character: str
    source_start: int
    source_end: int
    marker_position: int
    rendered_prefix: str = ""


@dataclass(frozen=True, slots=True)
class _WikilinkCandidate:
    target: str
    alias: str | None
    decoded_source: str
    source_start: int
    source_end: int
    marker_position: int
    rendered_prefix: str = ""


def rendered_markdown_links(
    body: str,
    *,
    analysis: RenderedMarkdownAnalysis | None = None,
) -> tuple[RenderedMarkdownLink, ...]:
    """Return explicit authored links recognized by the page renderer.

    CommonMark itself decides whether bracket and angle syntax is a link. A
    source marker placed immediately before each parser-recognized candidate
    then binds the emitted ``link_open`` token back to its exact use site.
    This preserves code, comment, image, reference-link, and escape semantics
    without guessing from a repeated destination string.

    Raw HTML anchors are deliberately excluded: they are not authored
    Markdown relationships and HTML5 repair can change their DOM ownership.
    Raw tags do not maintain a second HTML5 state machine here; CommonMark
    token boundaries remain authoritative. Math is excluded separately. Bare
    GFM URL, ``www.``, and email literals are renderer conveniences rather than
    authored Graph relationships, so they are deliberately absent here.
    """

    shared_analysis = analysis or RenderedMarkdownAnalysis(body)
    if shared_analysis.markdown != body:
        raise ValueError("Markdown analysis body does not match link source")
    if not _may_contain_explicit_markdown_link(body):
        return ()
    candidates = _link_candidates(body, analysis=shared_analysis)
    return _materialize_candidates(
        body,
        candidates,
        analysis=shared_analysis,
    )


def rendered_wikilinks(
    body: str,
    *,
    analysis: RenderedMarkdownAnalysis | None = None,
) -> tuple[RenderedWikilink, ...]:
    """Return wikilinks produced from one decoded Markdown text token.

    The frontend tokenizes decoded mdast text. Backslash escapes and character
    references can therefore form delimiters, while emphasis, links, images,
    code, and ordinary links split or replace the text node and prevent a match.
    URL- or email-like targets and aliases are excluded by the relationship
    grammar even when a renderer would leave that spelling in a text node.
    Source markers preserve parser boundaries and recover the exact raw span
    used for snippets and math filtering.
    """

    shared_analysis = analysis or RenderedMarkdownAnalysis(body)
    if shared_analysis.markdown != body:
        raise ValueError("Markdown analysis body does not match wikilink source")
    source_candidates = _wikilink_candidates(body)
    if not source_candidates:
        return ()
    block_environment: dict[str, object] = {}
    block_tokens = tuple(RENDERED_BLOCKS.parse(body, block_environment))
    source_candidates = _exclude_non_text_block_candidates(
        body,
        source_candidates,
        block_tokens,
    )
    if not source_candidates:
        return ()
    decoded_body, decoded_offsets = _decoded_reader_text_source(body)
    highlight_sensitive = "==" in decoded_body
    if _plain_text_wikilink_fast_path(
        body,
        source_candidates,
        block_tokens,
        highlight_sensitive=highlight_sensitive,
        has_references=bool(
            block_environment.get("references") or block_environment.get("footnotes")
        ),
    ):
        visible_starts = (
            {
                decoded_offsets[position]
                for position in _visible_wikilink_positions(
                    decoded_body,
                    non_text_spans=tuple(
                        (
                            bisect_left(decoded_offsets, start),
                            bisect_left(decoded_offsets, end),
                        )
                        for start, end in _raw_reader_literal_spans(body)
                    ),
                )
            }
            if highlight_sensitive
            else None
        )
        return tuple(
            RenderedWikilink(
                target=candidate.target,
                source_start=candidate.source_start,
                source_end=candidate.source_end,
            )
            for candidate in source_candidates
            if visible_starts is None or candidate.source_start in visible_starts
        )

    marker_start, marker_end = _marker_delimiters(body)
    if highlight_sensitive:
        # The original next source character is punctuation (a bracket/bang
        # or its escaping slash). A bare PUA marker is not punctuation and
        # changes adjacent emphasis/strike flanking. Backslash + non-ASCII
        # PUA is literal text, not a Markdown escape, and is removed with the
        # marker after parsing; its punctuation preserves that boundary.
        marker_start = "\\" + marker_start
    marker_width = max(1, len(f"{len(source_candidates) - 1:x}"))
    markers = tuple(
        f"{marker_start}{ordinal:0{marker_width}x}{marker_end}"
        for ordinal in range(len(source_candidates))
    )
    pieces: list[str] = []
    cursor = 0
    for candidate, marker_text in zip(source_candidates, markers, strict=True):
        pieces.extend((body[cursor : candidate.marker_position], marker_text))
        cursor = candidate.marker_position
    pieces.append(body[cursor:])
    marked_body = "".join(pieces)
    marker = re.compile(
        rf"{re.escape(marker_start)}"
        r"(?P<ordinal>[0-9a-f]+)"
        rf"{re.escape(marker_end)}"
    )

    rendered_candidates: list[RenderedWikilink] = []
    reader_parser = (
        _wikilink_boundary_parser() if highlight_sensitive else _MARKDOWN_LINK_PARSER
    )
    reader_environment = {"_dtx_wikilink_source_marker": marker}
    for token in reader_parser.parse(marked_body, reader_environment):
        if token.type != "inline":
            continue
        link_depth = 0
        for child in token.children or ():
            if child.type == "link_open":
                link_depth += 1
                continue
            if child.type == "link_close":
                link_depth = max(0, link_depth - 1)
                continue
            if child.type != "text" or link_depth:
                continue
            rendered_candidates.extend(
                _wikilinks_from_text_token(
                    child.content,
                    marker=marker,
                    candidates=source_candidates,
                )
            )

    if not rendered_candidates:
        return ()
    outside_math = shared_analysis.source_spans_outside_math(
        tuple((item.source_start, item.source_end) for item in rendered_candidates)
    )
    return tuple(
        candidate
        for ordinal, candidate in enumerate(rendered_candidates)
        if ordinal in outside_math
    )


def _may_contain_explicit_markdown_link(body: str) -> bool:
    """Cheaply reject bodies with no possible explicit link construct."""

    return "<" in body or "](" in body or "]:" in body


def _plain_text_wikilink_fast_path(
    body: str,
    candidates: tuple[_WikilinkCandidate, ...],
    block_tokens: tuple[Token, ...],
    *,
    highlight_sensitive: bool = False,
    has_references: bool = False,
) -> bool:
    """Recognize the scale-safe subset whose candidates are plain text.

    A block without code, math, raw HTML, or ordinary Markdown link syntax
    cannot split or hide a candidate that itself carries no inline delimiter,
    so its rendered text token is the source text. This avoids feeding a legal
    index page through CommonMark's nested-link backtracking, which is
    quadratic in one paragraph (32k custom tokens measured 27 s before this
    path, 0.4 s after). A body whose blocks are not all plain still takes the
    marker round trip below.
    """

    plain_blocks = _plain_text_inline_blocks(body, block_tokens)
    if not plain_blocks:
        return False
    line_starts = _line_start_positions(body)
    block_starts = tuple(start for start, _ in plain_blocks)
    if not all(
        _plain_wikilink_candidate(candidate)
        and _line_in_ranges(
            bisect_right(line_starts, candidate.source_start) - 1,
            plain_blocks,
            block_starts,
        )
        for candidate in candidates
    ):
        return False
    if highlight_sensitive:
        # Outside a wikilink, emphasis/strike syntax can split the text node
        # between two == delimiters. Those blocks need the marker round trip;
        # genuine plain highlight/index blocks stay on this linear path.
        candidate_blocks = {
            bisect_right(
                block_starts, bisect_right(line_starts, candidate.source_start) - 1
            )
            - 1
            for candidate in candidates
        }
        decoded_block_highlights: dict[int, bool] = {}
        candidate_index = 0
        split_markers = merge(
            _INLINE_TEXT_SPLIT_MARKER.finditer(body),
            _REFERENCE_TEXT_SPLIT.finditer(body) if has_references else (),
            key=lambda match: match.start(),
        )
        for match in split_markers:
            position = match.start()
            while (
                candidate_index < len(candidates)
                and candidates[candidate_index].source_end <= position
            ):
                candidate_index += 1
            if (
                candidate_index < len(candidates)
                and candidates[candidate_index].source_start <= position
            ):
                continue
            line = bisect_right(line_starts, position) - 1
            block_index = bisect_right(block_starts, line) - 1
            if block_index not in candidate_blocks or not _line_in_ranges(
                line, plain_blocks, block_starts
            ):
                continue
            if block_index not in decoded_block_highlights:
                start_line, end_line = plain_blocks[block_index]
                source_end = (
                    line_starts[end_line] if end_line < len(line_starts) else len(body)
                )
                decoded, _ = _decoded_reader_text_source(
                    body[line_starts[start_line] : source_end]
                )
                decoded_block_highlights[block_index] = "==" in decoded
            if decoded_block_highlights[block_index]:
                return False
    return True


def _plain_text_inline_blocks(
    body: str,
    block_tokens: tuple[Token, ...],
) -> tuple[tuple[int, int], ...]:
    """Return line ranges of inline blocks that render as literal text.

    A construct that could consume or split a candidate always leaves one of
    these characters in the block's own source, so their absence is decidable
    per block. Reference definitions and table cells produce no mapped inline
    token and therefore never qualify.
    """

    line_starts = _line_start_positions(body)
    ranges: list[tuple[int, int]] = []
    for token in block_tokens:
        if token.type != "inline" or token.map is None:
            continue
        start_line, end_line = token.map
        source = body[
            line_starts[start_line] : (
                line_starts[end_line] if end_line < len(line_starts) else len(body)
            )
        ]
        if any(character in source for character in ("`", "$", "<")):
            continue
        if "](" in source or "][" in source or "]:" in source:
            continue
        # A block whose inline content is not its own source text had markup
        # stripped by the block parser (list markers, blockquote prefixes,
        # table cells), so the source span no longer proves what the inline
        # parser sees.
        if token.content.strip() != source.strip():
            continue
        ranges.append((start_line, end_line))
    return tuple(ranges)


def _line_start_positions(body: str) -> tuple[int, ...]:
    return (0, *(match.end() for match in re.finditer(r"\r\n?|\n", body)))


def _exclude_non_text_block_candidates(
    body: str,
    candidates: tuple[_WikilinkCandidate, ...],
    block_tokens: tuple[Token, ...],
) -> tuple[_WikilinkCandidate, ...]:
    """Drop candidates hidden by original source block structure.

    Markers can perturb an HTML or code opener before the marked document is
    parsed. Classifying source lines first keeps those blocks authoritative.
    """

    blocked_ranges = tuple(
        (token.map[0], token.map[1])
        for token in block_tokens
        if token.type in {"code_block", "fence", "html_block"}
        and token.map is not None
    )
    if not blocked_ranges:
        return candidates

    line_starts = (0, *(match.end() for match in re.finditer(r"\r\n?|\n", body)))
    blocked_starts = tuple(start for start, _ in blocked_ranges)
    return tuple(
        candidate
        for candidate in candidates
        if not _line_in_ranges(
            bisect_right(line_starts, candidate.source_start) - 1,
            blocked_ranges,
            blocked_starts,
        )
    )


def _line_in_ranges(
    line: int,
    ranges: tuple[tuple[int, int], ...],
    starts: tuple[int, ...],
) -> bool:
    index = bisect_right(starts, line) - 1
    return index >= 0 and line < ranges[index][1]


def _plain_wikilink_candidate(candidate: _WikilinkCandidate) -> bool:
    """Reject a candidate whose own text could start another inline rule.

    Bracket, image, and footnote syntax inside the custom token can consume
    part of it, which would leave the reader a different text node than the
    source span suggests.
    """

    inner = candidate.decoded_source[2:-2]
    if any(character in inner for character in ("*", "~", "`", "<", ">", "[", "]")):
        return False
    return all(
        character != "_"
        or (
            0 < position < len(inner) - 1
            and inner[position - 1].isalnum()
            and inner[position + 1].isalnum()
        )
        for position, character in enumerate(inner)
    )


def _link_candidates(
    body: str,
    *,
    analysis: RenderedMarkdownAnalysis,
) -> tuple[_LinkCandidate, ...]:
    normalized, source_boundaries = _normalized_source_with_boundaries(body)
    line_starts = (0, *(match.end() for match in re.finditer("\n", normalized)))
    inline_ends = _inline_source_ends(analysis.tokens, line_starts, len(normalized))
    state = StateInline(
        normalized,
        _MARKDOWN_LINK_PARSER,
        analysis.environment,
        [],
    )

    candidates: list[_LinkCandidate] = []
    occupied_until = 0
    for position, character in enumerate(normalized):
        if position < occupied_until or character not in "[<":
            continue
        if character == "[" and _is_cdata_opener_terminal_bracket(
            normalized,
            position,
        ):
            continue
        line = bisect_right(line_starts, position) - 1
        inline_end = inline_ends.get(line)
        if inline_end is None:
            continue

        state.pos = position
        state.posMax = inline_end
        label_end = -1
        if character == "[":
            label_end = _MARKDOWN_LINK_PARSER.helpers.parseLinkLabel(
                state,
                position,
                True,
            )
            matched = parse_link(state, True)
        else:
            matched = parse_autolink(state, True)
        if not matched:
            continue

        syntax_end = state.pos
        snippet_end = label_end + 1 if label_end >= position else syntax_end
        marker_position = _protected_marker_position(normalized, position)
        candidates.append(
            _LinkCandidate(
                syntax_start=source_boundaries[position],
                syntax_end=source_boundaries[syntax_end],
                snippet_start=source_boundaries[position],
                snippet_end=source_boundaries[snippet_end],
                marker_position=source_boundaries[marker_position],
            )
        )
        occupied_until = syntax_end
    return tuple(candidates)


def _materialize_candidates(
    body: str,
    candidates: tuple[_LinkCandidate, ...],
    *,
    analysis: RenderedMarkdownAnalysis,
) -> tuple[RenderedMarkdownLink, ...]:
    if not candidates:
        return ()

    marker_start, marker_end = _marker_delimiters(body)
    marker_width = max(1, len(f"{len(candidates) - 1:x}"))
    markers = tuple(
        f"{marker_start}{ordinal:0{marker_width}x}{marker_end}"
        for ordinal in range(len(candidates))
    )
    pieces: list[str] = []
    cursor = 0
    for candidate, marker in zip(candidates, markers, strict=True):
        pieces.extend((body[cursor : candidate.marker_position], marker))
        cursor = candidate.marker_position
    pieces.append(body[cursor:])
    marked_body = "".join(pieces)
    marker_at_end = re.compile(
        rf"{re.escape(marker_start)}"
        r"(?P<ordinal>[0-9a-f]+)"
        rf"{re.escape(marker_end)}$"
    )
    outside_math = analysis.source_spans_outside_math(
        tuple(
            (candidate.syntax_start, candidate.syntax_start + 1)
            for candidate in candidates
        ),
    )

    links: list[RenderedMarkdownLink] = []
    for token in _MARKDOWN_LINK_PARSER.parse(marked_body):
        if token.type != "inline":
            continue
        pending_ordinal: int | None = None
        for child in token.children or ():
            if child.type == "text":
                match = marker_at_end.search(child.content)
                pending_ordinal = (
                    int(match.group("ordinal"), 16)
                    if match is not None
                    else None
                )
                continue
            if child.type == "link_open" and pending_ordinal is not None:
                destination = child.attrGet("href")
                if (
                    destination
                    and pending_ordinal in outside_math
                ):
                    candidate = candidates[pending_ordinal]
                    links.append(
                        RenderedMarkdownLink(
                            destination=destination,
                            source_start=candidate.snippet_start,
                            source_end=candidate.snippet_end,
                        )
                    )
            pending_ordinal = None
    return tuple(links)


def _inline_source_ends(
    tokens: tuple[Token, ...],
    line_starts: tuple[int, ...],
    source_length: int,
) -> dict[int, int]:
    ends: dict[int, int] = {}
    for token in tokens:
        if token.type != "inline" or token.map is None:
            continue
        start_line, end_line = token.map
        source_end = (
            line_starts[end_line] if end_line < len(line_starts) else source_length
        )
        for line in range(start_line, end_line):
            ends[line] = source_end
    return ends


def _bracket_units(body: str) -> tuple[_BracketUnit, ...]:
    entity_units: dict[int, _BracketUnit] = {}
    for match in _HTML_ENTITY.finditer(body):
        if _preceding_backslash_count(body, match.start()) % 2:
            continue
        decoded = html.unescape(match.group(0))
        if decoded in {"[", "]"}:
            entity_units[match.start()] = _BracketUnit(
                character=decoded,
                source_start=match.start(),
                source_end=match.end(),
                marker_position=match.start(),
            )

    units: list[_BracketUnit] = []
    position = 0
    while position < len(body):
        entity = entity_units.get(position)
        if entity is not None:
            units.append(entity)
            position = entity.source_end
            continue
        character = body[position]
        if character not in "[]":
            position += 1
            continue
        if character == "[" and _is_cdata_opener_terminal_bracket(body, position):
            position += 1
            continue

        slash_count = _preceding_backslash_count(body, position)
        if slash_count % 2:
            units.append(
                _BracketUnit(
                    character=character,
                    source_start=position - 1,
                    source_end=position + 1,
                    marker_position=position - 1,
                )
            )
            position += 1
            continue

        marker_position = position
        rendered_prefix = ""
        if character == "[" and position and body[position - 1] == "!":
            bang_position = position - 1
            if _preceding_backslash_count(body, bang_position) % 2 == 0:
                marker_position = bang_position
                rendered_prefix = "!"
        units.append(
            _BracketUnit(
                character=character,
                source_start=position,
                source_end=position + 1,
                marker_position=marker_position,
                rendered_prefix=rendered_prefix,
            )
        )
        position += 1
    return tuple(units)


def _is_cdata_opener_terminal_bracket(body: str, position: int) -> bool:
    opener = "<![CDATA["
    opener_start = position - len(opener) + 1
    return (
        opener_start >= 0
        and body[opener_start : position + 1] == opener
        and _preceding_backslash_count(body, opener_start) % 2 == 0
        and body.find("]]>", position + 1) >= 0
    )


def _wikilink_candidates(body: str) -> tuple[_WikilinkCandidate, ...]:
    units = _bracket_units(body)
    line_starts = (0, *(match.end() for match in re.finditer(r"\r\n?|\n", body)))
    url_literals = _UrlLiteralIndex(body)
    candidates: list[_WikilinkCandidate] = []
    opening_index: int | None = None
    index = 0
    while index < len(units):
        unit = units[index]
        following = units[index + 1] if index + 1 < len(units) else None
        is_adjacent_pair = (
            following is not None and unit.source_end == following.source_start
        )
        if opening_index is None:
            if (
                unit.character == "["
                and is_adjacent_pair
                and following.character == "["
            ):
                opening_index = index
                index += 2
                continue
            index += 1
            continue

        first_open = units[opening_index]
        if bisect_right(line_starts, first_open.source_start) != bisect_right(
            line_starts,
            unit.source_start,
        ):
            opening_index = None
            continue

        if unit.character != "]":
            index += 1
            continue

        if is_adjacent_pair and following.character == "]":
            source = body[first_open.source_start : following.source_end]
            decoded = html.unescape(_ASCII_PUNCTUATION_ESCAPE.sub(r"\1", source))
            match = _WIKILINK.fullmatch(decoded)
            if (
                match is not None
                and _relationship_wikilink_text(match.group(1), match.group(2))
                and not url_literals.precedes(first_open.source_start)
            ):
                candidates.append(
                    _WikilinkCandidate(
                        target=match.group(1),
                        alias=match.group(2),
                        decoded_source=decoded,
                        source_start=first_open.source_start,
                        source_end=following.source_end,
                        marker_position=first_open.marker_position,
                        rendered_prefix=first_open.rendered_prefix,
                    )
                )
            index += 2
        else:
            index += 1
        opening_index = None
    return tuple(candidates)


def _relationship_wikilink_text(target: str, alias: str | None) -> bool:
    """Keep URL-looking custom tokens from becoming Graph relationships."""

    return all(
        not _url_or_email_like_wikilink_part(part)
        for part in (target, alias)
        if part is not None
    )


class _UrlLiteralIndex:
    """Answer "is this offset inside a bare URL-like token" in log time.

    The token that precedes a candidate is unbounded: a body without
    whitespace makes every candidate re-scan the whole prefix, which is
    quadratic (16k custom tokens on one line measured 176 s before this
    index). Token starts and URL-literal markers are therefore located once
    per body, and each candidate resolves to two binary searches.
    """

    __slots__ = ("_marker_spans", "_marker_starts", "_token_starts")

    def __init__(self, body: str) -> None:
        self._token_starts = _token_start_positions(body)
        self._marker_spans = _url_literal_markers(body)
        self._marker_starts = tuple(start for start, _ in self._marker_spans)

    def precedes(self, source_start: int) -> bool:
        token_start = self._token_starts[
            bisect_right(self._token_starts, source_start) - 1
        ]
        index = bisect_right(self._marker_starts, source_start) - 1
        while index >= 0 and self._marker_starts[index] >= token_start:
            if self._marker_spans[index][1] <= source_start:
                return True
            index -= 1
        return False


def _token_start_positions(body: str) -> tuple[int, ...]:
    positions = {0}
    positions.update(match.end() for match in _TOKEN_BREAK.finditer(body))
    positions.update(match.end() for match in _BRACKET_CALL_BREAK.finditer(body))
    return tuple(sorted(positions))


def _url_literal_markers(body: str) -> tuple[tuple[int, int], ...]:
    """Locate every ``://`` and ``www.`` literal, including decoded spellings."""

    spans = {
        (match.start(), match.end())
        for pattern in (_URL_SCHEME_SEPARATOR, _WIKILINK_WWW_LITERAL)
        for match in pattern.finditer(body)
    }
    if "&" in body or "\\" in body:
        decoded, offsets = _decoded_source_offsets(body)
        spans.update(
            (offsets[match.start()], offsets[match.end()])
            for pattern in (_URL_SCHEME_SEPARATOR, _WIKILINK_WWW_LITERAL)
            for match in pattern.finditer(decoded)
        )
    return tuple(sorted(spans))


def _decoded_source_offsets(body: str) -> tuple[str, tuple[int, ...]]:
    """Decode escapes and character references, keeping source offsets.

    The decoded text is what a reader sees, so an entity-spelled scheme
    separator still marks a bare URL. Each decoded index maps to the source
    offset of the unit that produced it, and the final entry maps past the end.
    """

    text, offsets = body, list(range(len(body) + 1))
    for pattern, decode in (
        (_ASCII_PUNCTUATION_ESCAPE, lambda match: match.group(1)),
        (_HTML_ENTITY, lambda match: html.unescape(match.group(0))),
    ):
        pieces: list[str] = []
        mapped: list[int] = []
        cursor = 0
        for match in pattern.finditer(text):
            pieces.append(text[cursor : match.start()])
            mapped.extend(offsets[cursor : match.start()])
            replacement = decode(match)
            pieces.append(replacement)
            mapped.extend([offsets[match.start()]] * len(replacement))
            cursor = match.end()
        if not pieces:
            continue
        pieces.append(text[cursor:])
        mapped.extend(offsets[cursor:])
        text = "".join(pieces)
        offsets = mapped
    return text, tuple(offsets)


def _url_or_email_like_wikilink_part(value: str) -> bool:
    return (
        "://" in value
        or _WIKILINK_WWW_LITERAL.search(value) is not None
        or _WIKILINK_EMAIL_LITERAL.search(value) is not None
    )


def _decoded_reader_text_source(body: str) -> tuple[str, Sequence[int]]:
    """Decode one text pass, preserving escaped entities as literal text.

    Its token visibility is authoritative only after the fast-path proof
    excludes syntax that splits candidate blocks. Escapes and entities are
    alternatives, not sequential passes: ``\\&#61;`` stays ``&#61;``, not ``=``.
    """

    if "&" not in body and "\\" not in body:
        return body, range(len(body) + 1)
    pieces: list[str] = []
    offsets: list[int] = []
    cursor = 0
    for match in _TEXT_DECODE_UNIT.finditer(body):
        pieces.append(body[cursor : match.start()])
        offsets.extend(range(cursor, match.start()))
        raw = match.group(0)
        decoded = raw[1:] if raw.startswith("\\") else html.unescape(raw)
        pieces.append(decoded)
        offsets.extend([match.start()] * len(decoded))
        cursor = match.end()
    pieces.append(body[cursor:])
    offsets.extend(range(cursor, len(body) + 1))
    return "".join(pieces), tuple(offsets)


def _reader_punctuation(character: str) -> bool:
    return unicodedata.category(character)[0] in {"P", "S"}


def _reader_boundary_whitespace(character: str) -> bool:
    # BOM is whitespace in the frontend's ECMAScript boundary predicates.
    return character.isspace() or character == "\ufeff"


def _trim_reader_url(content: str, start: int, end: int, *, raw: bool) -> int:
    if raw:
        resource = _BRACKET_CALL_BREAK.search(content, start, end)
        if resource is not None:
            # micromark's raw trailBracketAfter stops before ] followed by
            # a resource/reference opener. It does not stop at every ].
            end = resource.start()
    trail = (_RAW_READER_URL_TRAIL if raw else _READER_URL_TRAIL).search(
        content, start, end
    )
    if trail is None:
        return end
    base_end = trail.start()
    unmatched = content.count("(", start, base_end) - content.count(
        ")", start, base_end
    )
    for position in range(base_end, end):
        if content[position] == ")" and unmatched > 0:
            base_end = position + 1
            unmatched -= 1
    return base_end


def _reader_url_candidates(
    content: str, *, raw: bool
) -> Iterator[tuple[int, str, int, int, bool]]:
    """Retry rejected openers without repeatedly scanning their greedy tails."""

    breaks = iter((_RAW_READER_URL_END if raw else _READER_URL_END).finditer(content))
    boundary = next(breaks, None)
    domain_cursor = 0
    domain_details: dict[int, tuple[int, int, int, int, int, int]] = {}
    for opener in _READER_URL_OPENER.finditer(content):
        start = opener.start()
        protocol = opener.group()
        domain_start = start if protocol.lower().startswith("www") else opener.end()
        while boundary is not None and boundary.start() < domain_start:
            boundary = next(breaks, None)
        end = boundary.start() if boundary is not None else len(content)
        if domain_start >= domain_cursor:
            domain_cursor = domain_start
            while domain_cursor < end:
                character = content[domain_cursor]
                if raw:
                    allowed = character in ".-_" or not (
                        character.isspace()
                        # Astral UTF-16 units are not BMP punctuation in the
                        # raw frontend tokenizer (e.g. an emoji domain).
                        or (
                            ord(character) <= 0xFFFF
                            and _reader_punctuation(character)
                        )
                        or ord(character) < 32
                    )
                else:
                    allowed = character in ".-_" or (
                        character.isascii() and character.isalnum()
                    )
                if not allowed:
                    break
                domain_cursor += 1
        if domain_cursor not in domain_details:
            domain_end = domain_cursor
            if raw:
                while domain_end > domain_start and content[domain_end - 1] == ".":
                    domain_end -= 1
            last_dot = content.rfind(".", domain_start, domain_end)
            previous_dot = (
                content.rfind(".", domain_start, last_dot)
                if last_dot >= domain_start
                else -1
            )
            tail_start = max(domain_start, previous_dot + 1)
            underscore = content.rfind("_", tail_start, domain_end)
            first_good = last_good = meaningful = -1
            for position in range(domain_start, domain_end):
                character = content[position]
                if character not in ".-_":
                    meaningful = position
                if (
                    position >= tail_start
                    and character.isascii()
                    and character.isalnum()
                ):
                    if position < last_dot:
                        first_good = position
                    else:
                        last_good = position
            domain_details[domain_cursor] = (
                domain_end, last_dot, previous_dot, underscore, first_good, last_good
            )
            # Raw domains require a non-punctuation character anywhere, while
            # decoded GFM validates only the final two dot-separated parts.
            if raw:
                domain_details[domain_cursor] = (
                    domain_end,
                    last_dot,
                    previous_dot,
                    underscore,
                    meaningful,
                    last_good,
                )
        domain_end, last_dot, previous_dot, underscore, first_good, last_good = (
            domain_details[domain_cursor]
        )
        tail_start = max(domain_start, previous_dot + 1)
        domain_allowed = underscore < tail_start
        if raw:
            domain_allowed = domain_allowed and first_good >= domain_start
        else:
            domain_allowed = domain_allowed and last_dot >= domain_start and (
                (last_dot == tail_start or first_good >= tail_start)
                and (domain_end == last_dot + 1 or last_good > last_dot)
            )
        yield start, protocol, domain_start, end, domain_allowed


def _raw_reader_url_spans(content: str) -> tuple[tuple[int, int], ...]:
    """Raw GFM HTTP/www boundaries, before escapes/entities are decoded.

    In particular raw ``https://localhost`` is a link, but its entity-formed
    spelling is not: the later decoded-text GFM transform requires a dotted
    domain. These boundaries do not add bare URL relationships.
    """

    spans: list[tuple[int, int]] = []
    bracket_cursor = bracket_depth = consumed = 0
    for start, protocol, domain_start, end, domain_allowed in _reader_url_candidates(
        content, raw=True
    ):
        if start < consumed:
            continue
        while bracket_cursor < start:
            character = content[bracket_cursor]
            if character in "[]" and not (
                _preceding_backslash_count(content, bracket_cursor) % 2
            ):
                bracket_depth = max(0, bracket_depth + (1 if character == "[" else -1))
            bracket_cursor += 1
        if bracket_depth:
            continue
        previous = content[start - 1] if start else ""
        if protocol.lower().startswith("http"):
            if previous and previous.isascii() and previous.isalpha():
                continue
        else:
            if previous and not (
                previous in "(*_[]~" or _reader_boundary_whitespace(previous)
            ):
                continue
        if not domain_allowed:
            continue
        end = _trim_reader_url(content, start, end, raw=True)
        if end > domain_start:
            spans.append((start, end))
            consumed = end
    return tuple(spans)


def _decoded_reader_url_spans(content: str) -> tuple[tuple[int, int], ...]:
    """The reader's GFM text-node URL/email replacement boundaries."""

    urls: list[tuple[int, int]] = []
    consumed = 0
    for start, _protocol, _domain_start, end, domain_allowed in _reader_url_candidates(
        content, raw=False
    ):
        if start < consumed:
            continue
        previous = content[start - 1] if start else ""
        if previous and not (
            _reader_boundary_whitespace(previous) or _reader_punctuation(previous)
        ):
            continue
        if not domain_allowed:
            continue
        urls.append((start, _trim_reader_url(content, start, end, raw=False)))
        # GFM find-and-replace consumes even the punctuation it returns as a
        # following text node; only a rejected match retries from its opener.
        consumed = end
    emails: list[tuple[int, int]] = []
    url_index = 0
    for match in _READER_EMAIL_LITERAL.finditer(content):
        start = match.start()
        previous = content[start - 1] if start else ""
        if previous and (
            previous == "/"
            or not (
                _reader_boundary_whitespace(previous) or _reader_punctuation(previous)
            )
        ):
            continue
        if match.group(2)[-1] in "-0123456789_":
            continue
        while url_index < len(urls) and urls[url_index][1] <= start:
            url_index += 1
        if url_index < len(urls) and urls[url_index][0] <= start < urls[url_index][1]:
            continue
        emails.append(match.span())
    return tuple(merge(urls, emails))


def _raw_reader_literal_spans(content: str) -> tuple[tuple[int, int], ...]:
    emails: list[tuple[int, int]] = []
    cursor = depth = 0
    for match in _READER_EMAIL_LITERAL.finditer(content):
        start = match.start()
        while cursor < start:
            if content[cursor] in "[]" and not (
                _preceding_backslash_count(content, cursor) % 2
            ):
                depth = max(0, depth + (1 if content[cursor] == "[" else -1))
            cursor += 1
        # Raw micromark email literals allow a preceding Unicode letter;
        # its later decoded-text replacement does not. Keep both phases.
        if depth or (start and content[start - 1] == "/"):
            continue
        last = match.group(2)[-1]
        if last.isascii() and last.isalpha():
            emails.append(match.span())
    return tuple(merge(_raw_reader_url_spans(content), emails))


def _reader_literal_spans_before_source_markers(
    state: StateInline,
) -> tuple[tuple[int, int], ...]:
    marker = state.env.get("_dtx_wikilink_source_marker")
    if marker is None:
        return _raw_reader_literal_spans(state.src)
    pieces: list[str] = []
    positions: list[int] = []
    cursor = 0
    for match in marker.finditer(state.src):
        pieces.append(state.src[cursor:match.start()])
        positions.extend(range(cursor, match.start()))
        cursor = match.end()
    pieces.append(state.src[cursor:])
    positions.extend(range(cursor, len(state.src)))
    # Source markers must not split a raw GFM ]([ resource-trail boundary.
    # Map the resulting literal's first/last characters back to marked source
    # without accidentally consuming a marker exactly at its end boundary.
    return tuple(
        (positions[start], positions[end - 1] + 1)
        for start, end in _raw_reader_literal_spans("".join(pieces))
    )


def _reader_boundary_text(state: StateInline, silent: bool) -> bool:
    """Expose raw math/GFM boundaries without changing authored-link grammar."""

    if state.linkLevel:
        return parse_text(state, silent)
    if not hasattr(state, "_dtx_reader_boundaries"):
        runs = tuple(_DOLLAR_RUN.finditer(state.src))
        next_run: dict[int, int] = {}
        math: list[tuple[int, int]] = []
        for run in reversed(runs):
            width = run.end() - run.start()
            escaped = _preceding_backslash_count(state.src, run.start()) % 2
            opener_width = width - escaped
            if opener_width and opener_width in next_run:
                # An escape consumes only the first dollar. Its remaining run
                # may open math; closing runs inside math retain their raw size.
                math.append((run.start() + escaped, next_run[opener_width]))
            next_run[width] = run.end()
        spans = tuple(
            merge(_reader_literal_spans_before_source_markers(state), reversed(math))
        )
        state._dtx_reader_boundaries = spans
        state._dtx_reader_boundary_starts = tuple(start for start, _ in spans)
    spans = state._dtx_reader_boundaries
    index = bisect_left(state._dtx_reader_boundary_starts, state.pos)
    if (
        index < len(spans)
        and spans[index][0] == state.pos + 1
        and state.src[state.pos] == "\\"
        and _ASCII_PUNCTUATION_ESCAPE.match(state.src, state.pos) is None
    ):
        # CommonMark preserves an unknown escape's slash, but MarkdownIt's
        # escape rule consumes its next character too. Leave a raw URL opener
        # available for the following boundary rule, as micromark does.
        if not silent:
            state.pending += "\\"
        state.pos += 1
        return True
    if index < len(spans) and spans[index][0] == state.pos:
        if not silent:
            state.push("dtx_reader_non_text", "", 0)
        state.pos = spans[index][1]
        return True
    terminator = state.md.inline.terminator_re.search(state.src, state.pos)
    end = terminator.start() if terminator else state.posMax
    if index < len(spans):
        end = min(end, spans[index][0])
    if end == state.pos:
        return False
    if not silent:
        state.pending += state.src[state.pos:end]
    state.pos = end
    return True


class _ReaderBoundaryStateInline(StateInline):
    def scanDelims(self, start: int, canSplitWord: bool) -> Scanned:
        scanned = super().scanDelims(start, canSplitWord)
        end = start + scanned.length
        previous = self.src[start - 1] if start else " "
        following = self.src[end] if end < self.posMax else " "
        extra_whitespace = "\u2028\u2029\ufeff"
        if previous not in extra_whitespace and following not in extra_whitespace:
            return scanned
        # Match the same scanDelims formula, changing only the three frontend
        # whitespace characters absent from MarkdownIt's isWhiteSpace set.
        previous_punctuation = isMdAsciiPunct(ord(previous)) or isPunctChar(previous)
        following_punctuation = isMdAsciiPunct(ord(following)) or isPunctChar(following)
        previous_space = isWhiteSpace(ord(previous)) or previous in extra_whitespace
        following_space = isWhiteSpace(ord(following)) or following in extra_whitespace
        left = not (
            following_space
            or (following_punctuation and not (previous_space or previous_punctuation))
        )
        right = not (
            previous_space
            or (previous_punctuation and not (following_space or following_punctuation))
        )
        return Scanned(
            left and (canSplitWord or not right or previous_punctuation),
            right and (canSplitWord or not left or following_punctuation),
            scanned.length,
        )


class _ReaderBoundaryInlineParser(ParserInline):
    def __init__(self, source: ParserInline) -> None:
        super().__init__()
        self.ruler = deepcopy(source.ruler)
        self.ruler2 = deepcopy(source.ruler2)
        self._extra_terminator_chars = set(source._extra_terminator_chars)
        self.terminator_re = source.terminator_re

    def parse(
        self, src: str, md: MarkdownIt, env: EnvType, tokens: list[Token]
    ) -> list[Token]:
        # Only this private profile uses the reader-compatible state. Never
        # replace global StateInline or mutate the shared canonical parser.
        state = _ReaderBoundaryStateInline(src, md, env, tokens)
        self.tokenize(state)
        for rule in self.ruler2.getRules(""):
            rule(state)
        return state.tokens


@lru_cache(maxsize=1)
def _wikilink_boundary_parser() -> MarkdownIt:
    # Never mutate the canonical shared parser: explicit authored-link parsing
    # and media analysis retain their existing profile and relationship policy.
    parser = copy(_MARKDOWN_LINK_PARSER)
    parser.inline = _ReaderBoundaryInlineParser(_MARKDOWN_LINK_PARSER.inline)
    parser.inline.ruler.at("text", _reader_boundary_text)
    return parser


def _visible_wikilink_positions(
    content: str,
    *,
    non_text_spans: tuple[tuple[int, int], ...] = (),
) -> set[int]:
    # Raw links are already non-text nodes. The decoded GFM replacement runs
    # only on the remaining text, never across one of those consumed links.
    visible: set[int] = set()
    cursor = 0
    for start, end in non_text_spans:
        if start > cursor:
            visible.update(
                cursor + position
                for position in _visible_decoded_wikilink_positions(
                    content[cursor:start]
                )
            )
        cursor = max(cursor, end)
    visible.update(
        cursor + position
        for position in _visible_decoded_wikilink_positions(content[cursor:])
    )
    return visible


def _visible_decoded_wikilink_positions(content: str) -> set[int]:
    spans = _decoded_reader_url_spans(content)
    visible: set[int] = set()
    cursor = 0
    for start, end in spans:
        if start > cursor:
            visible.update(
                cursor + position
                for position in _custom_wikilink_positions(content[cursor:start])
            )
        cursor = max(cursor, end)
    visible.update(
        cursor + position
        for position in _custom_wikilink_positions(content[cursor:])
    )
    return visible


def _custom_wikilink_positions(content: str) -> set[int]:
    """Mirror the reader's competing wikilink/highlight text-token matches.

    Consume complete wikilink syntax even when its target is relationship-
    rejected: the frontend still consumes that spelling before any inner ==.
    An earlier ==highlight== instead consumes all inner wikilinks as text.
    Fixed-width overlapping openers and monotonic terminal cursors keep even
    malformed/nested bracket runs linear; no unbounded regex backtracking.
    """

    length = len(content)
    target_ends = [
        *(match.start() for match in _WIKILINK_TARGET_END.finditer(content)),
        length,
    ]
    alias_ends = [
        *(match.start() for match in _WIKILINK_ALIAS_END.finditer(content)),
        length,
    ]
    highlight_ends = [
        *(match.start() for match in _HIGHLIGHT_END.finditer(content)),
        length,
    ]
    target_index = alias_index = highlight_index = 0
    occupied_until = 0
    visible: set[int] = set()
    for match in _CUSTOM_TOKEN_OPENER.finditer(content):
        position = match.start()
        if position < occupied_until:
            continue
        inner_start = position + 2
        if match.group(1) == "==":
            while highlight_ends[highlight_index] < inner_start:
                highlight_index += 1
            end = highlight_ends[highlight_index]
            if end > inner_start and content[end : end + 2] == "==":
                occupied_until = end + 2
            continue
        while target_ends[target_index] < inner_start:
            target_index += 1
        end = target_ends[target_index]
        if end == inner_start or end == length or content[end] == "\n":
            continue
        if content[end] == "|":
            alias_start = end + 1
            while alias_ends[alias_index] < alias_start:
                alias_index += 1
            end = alias_ends[alias_index]
            if end == alias_start:
                continue
        if content[end : end + 2] == "]]":
            visible.add(position)
            occupied_until = end + 2
    return visible


def _wikilinks_from_text_token(
    content: str,
    *,
    marker: re.Pattern[str],
    candidates: tuple[_WikilinkCandidate, ...],
) -> tuple[RenderedWikilink, ...]:
    pieces: list[str] = []
    candidate_positions: list[tuple[int, int]] = []
    cursor = 0
    clean_length = 0
    for marker_match in marker.finditer(content):
        fragment = content[cursor : marker_match.start()]
        pieces.append(fragment)
        clean_length += len(fragment)
        ordinal = int(marker_match.group("ordinal"), 16)
        candidate_positions.append((ordinal, clean_length))
        cursor = marker_match.end()
    pieces.append(content[cursor:])
    clean = "".join(pieces)

    visible_starts = _visible_wikilink_positions(clean) if "==" in clean else None
    links: list[RenderedWikilink] = []
    for ordinal, marker_position in candidate_positions:
        candidate = candidates[ordinal]
        source_position = marker_position + len(candidate.rendered_prefix)
        if visible_starts is not None and source_position not in visible_starts:
            continue
        if not clean.startswith(candidate.decoded_source, source_position):
            continue
        links.append(
            RenderedWikilink(
                target=candidate.target,
                source_start=candidate.source_start,
                source_end=candidate.source_end,
            )
        )
    return tuple(links)


def _preceding_backslash_count(source: str, position: int) -> int:
    start = position
    while start and source[start - 1] == "\\":
        start -= 1
    return position - start


def _marker_delimiters(body: str) -> tuple[str, str]:
    """Choose short private-use delimiters absent from raw and decoded input."""

    decoded = html.unescape(body)
    available: list[str] = []
    for codepoint in range(0xE000, 0xF900):
        candidate = chr(codepoint)
        if candidate not in body and candidate not in decoded:
            available.append(candidate)
            if len(available) == 2:
                return available[0], available[1]
    raise ValueError("Markdown exhausts the private-use marker alphabet")


def _protected_marker_position(source: str, syntax_start: int) -> int:
    has_image_marker = (
        source[syntax_start] == "["
        and syntax_start > 0
        and source[syntax_start - 1] == "!"
    )
    protected_start = syntax_start - 1 if has_image_marker else syntax_start
    slash_start = protected_start
    while slash_start and source[slash_start - 1] == "\\":
        slash_start -= 1
    slash_count = protected_start - slash_start

    if has_image_marker:
        # An unescaped bang owns the bracket as image syntax. An escaped bang
        # is reader-visible text followed by an ordinary Markdown link.
        return protected_start if slash_count % 2 == 0 else syntax_start
    # Keep an odd escaping slash adjacent to the opener. Even slash pairs have
    # already reduced to reader-visible text, so the marker belongs after them.
    return syntax_start if slash_count % 2 == 0 else slash_start


def _normalized_source_with_boundaries(body: str) -> tuple[str, tuple[int, ...]]:
    """Match markdown-it newline/NUL normalization and retain raw offsets."""

    normalized: list[str] = []
    boundaries = [0]
    raw_position = 0
    while raw_position < len(body):
        character = body[raw_position]
        if character == "\r":
            raw_position += 1
            if raw_position < len(body) and body[raw_position] == "\n":
                raw_position += 1
            normalized.append("\n")
        elif character == "\n":
            raw_position += 1
            normalized.append("\n")
        else:
            raw_position += 1
            normalized.append("\ufffd" if character == "\0" else character)
        boundaries.append(raw_position)
    return "".join(normalized), tuple(boundaries)
