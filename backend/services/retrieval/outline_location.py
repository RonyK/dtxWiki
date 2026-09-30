"""Shared Markdown-outline location resolution for retrieval results and reads."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache

from backend.utils.markdown_outline import Outline, Section, parse_outline

MARKDOWN_OUTLINE_PARSER_VERSION = "markdown-outline/v1"
_QUERY_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)


def raw_body_sha256(body_md: str) -> str:
    return hashlib.sha256(body_md.encode("utf-8")).hexdigest()


def outline_for_body(body_md: str) -> Outline:
    """Parse raw Markdown with a cache key that is safe for raw offsets.

    Normalized content hashes can collide for raw bodies whose line endings or
    trailing whitespace differ. Offset-bearing outlines therefore use the
    parser-version-prefixed digest of the exact raw body as their cache key.
    """

    cache_key = f"{MARKDOWN_OUTLINE_PARSER_VERSION}:{raw_body_sha256(body_md)}"
    return _outline_for_raw_body(cache_key, body_md)


@lru_cache(maxsize=256)
def _outline_for_raw_body(cache_key: str, body_md: str) -> Outline:
    del cache_key
    return parse_outline(body_md)


def heading_location_for_offset(body_md: str, char_offset: int) -> Section | None:
    """Return the deepest heading whose raw subtree contains ``char_offset``."""

    candidates = [
        section
        for section in outline_for_body(body_md).sections
        if section.kind == "heading"
        and section.char_start <= char_offset < section.subtree_char_end
    ]
    if not candidates:
        return _first_heading(body_md)
    return max(candidates, key=lambda section: (section.level, section.char_start))


def heading_location_for_query(body_md: str, query: str) -> Section | None:
    """Resolve a lexical query to a heading using the shared raw outline."""

    # Match against the RAW body. `casefold()` is not length-preserving --
    # German "ß" folds to "ss" -- so an offset taken in a folded copy does not
    # index the raw outline this result is then attributed against, and the hit
    # can be credited to the wrong heading. `re.IGNORECASE` gives
    # case-insensitive matching at exact raw offsets, which is the property
    # that matters here.
    #
    # _query_tokens() is sorted longest-first. Use the earliest-occurring
    # token in THAT order (the most distinctive one that actually appears),
    # not the minimum position across all tokens -- a common word can occur
    # earlier in the body than the distinctive token that made the query
    # actually match, and taking min() would then attribute the hit to a
    # section the query wasn't really about.
    for token in _query_tokens(query):
        match = re.search(re.escape(token), body_md, re.IGNORECASE)
        if match is not None:
            return heading_location_for_offset(body_md, match.start())
    return _first_heading(body_md)


def public_heading_path(section: Section | None) -> list[str] | None:
    if section is None:
        return None
    return list(section.heading_path)


def _query_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    for token in _QUERY_TOKEN_RE.findall(query):
        if token not in tokens:
            tokens.append(token)
    return sorted(tokens, key=len, reverse=True)


def _first_heading(body_md: str) -> Section | None:
    return next(
        (
            section
            for section in outline_for_body(body_md).sections
            if section.kind == "heading"
        ),
        None,
    )
