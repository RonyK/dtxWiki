"""Deterministic query planning values for Markdown-native search lanes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from backend.services.entity_ref import normalize_entity_name
from backend.utils.uuid import parse_uuid_or_none

SearchIntent = Literal[
    "direct_path",
    "entity_lookup",
    "concept_lookup",
    "overview",
    "fallback",
]

_CONTEXT_PATH = re.compile(
    r"^/?context/(?P<slug>[^/?#]+?)(?:\.md)?$",
    re.IGNORECASE,
)
_SLUG_TOKEN = re.compile(r"^[^\s/\\]+$")
_CODE_TOKEN = re.compile(r"(?=.*[A-Z])(?=.*(?:\d|[-_.]))^[A-Z0-9_.-]+$")
_OVERVIEW_MARKERS = (
    "overview",
    "architecture",
    "summary",
    "compare",
    "전체",
    "요약",
    "구조",
    "비교",
)


@dataclass(frozen=True, slots=True)
class QueryPlan:
    raw_query: str
    query_norm: str
    tokens: tuple[str, ...]
    intent: SearchIntent

    @classmethod
    def build(cls, query: str) -> QueryPlan:
        stripped = query.strip()
        lookup_text = _strip_balanced_quotes(stripped)
        tokens = tuple(token for token in lookup_text.split() if token)
        query_norm = normalize_entity_name(lookup_text)
        return cls(
            raw_query=query,
            query_norm=query_norm,
            tokens=tokens,
            intent=_classify_intent(stripped, query_norm, tokens),
        )

    @property
    def lane_query(self) -> str:
        """Trimmed, case-preserving query passed to semantic/model lanes."""

        return self.raw_query.strip()

    @property
    def keyword_query(self) -> str:
        return self.lane_query.lower()

    @property
    def context_slug(self) -> str | None:
        explicit = self.explicit_context_slug
        if explicit is not None:
            return explicit
        if self.intent != "direct_path":
            return None
        candidate = _strip_balanced_quotes(self.lane_query)
        if len(self.tokens) == 1 and _SLUG_TOKEN.fullmatch(candidate):
            return candidate.removesuffix(".md")
        return None

    @property
    def explicit_context_slug(self) -> str | None:
        match = _CONTEXT_PATH.fullmatch(self.lane_query)
        return match.group("slug") if match is not None else None

    @property
    def direct_page_id(self) -> UUID | None:
        return parse_uuid_or_none(self.lane_query)


def _classify_intent(
    stripped: str,
    query_norm: str,
    tokens: tuple[str, ...],
) -> SearchIntent:
    if not query_norm:
        return "fallback"
    if parse_uuid_or_none(stripped) is not None or _CONTEXT_PATH.fullmatch(stripped):
        return "direct_path"
    normalized_tokens = tuple(normalize_entity_name(token) for token in tokens)
    if any(
        marker in query_norm
        for marker in _OVERVIEW_MARKERS
    ):
        return "overview"
    if (
        (len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'")
        or any(_CODE_TOKEN.fullmatch(token) for token in tokens)
    ):
        return "entity_lookup"
    if len(tokens) == 1 and _SLUG_TOKEN.fullmatch(stripped):
        return "direct_path"
    if any(normalized_tokens):
        return "concept_lookup"
    return "fallback"


def _strip_balanced_quotes(value: str) -> str:
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in "\"'"
    ):
        return value[1:-1].strip()
    return value
