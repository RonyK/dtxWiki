"""Effective policy for document bibliography, separate from claim evidence."""

from collections.abc import Mapping
from typing import Any

DOCUMENT_CITATION_KINDS = ("context_page", "library_item", "agent_page")


class CitationPolicyError(ValueError):
    pass


def coerce_document_kinds(value: object) -> list[str]:
    """Validate a policy edit and return the canonical declaration order."""
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list) or any(not isinstance(kind, str) for kind in value):
        raise CitationPolicyError(
            "accepted_document_kinds must be a list of document kinds"
        )
    if set(value) - set(DOCUMENT_CITATION_KINDS):
        raise CitationPolicyError(
            "accepted_document_kinds accepts context_page, library_item and agent_page"
        )
    return [kind for kind in DOCUMENT_CITATION_KINDS if kind in value]


def normalized_citation_policy(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    """Missing is the declared default; an explicit empty list allows none.

    Invalid persisted values raise rather than inventing an enabled default.
    Legacy accepted_citation_types and strictness keep their existing meaning.
    """
    result = dict(policy or {})
    result["accepted_document_kinds"] = coerce_document_kinds(
        result.get("accepted_document_kinds", list(DOCUMENT_CITATION_KINDS))
    )
    return result
