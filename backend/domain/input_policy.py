"""Approved limits for human-authored metadata and content.

Lengths are Unicode code points. These functions never shorten explicit input.
Historical read payloads deliberately do not use these write policies.
"""

import re

from backend.utils.graphemes import grapheme_clusters

SHORT_TEXT_SPACES = (
    " \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
)
_INVALID_TEXT = re.compile(r"[\x00\ud800-\udfff]")
CONTENT_TITLE_MAX = 256
BODY_MAX = 1_000_000
REASON_MAX = 2_000


class InputPolicyError(ValueError):
    """An explicit input does not satisfy its field's write contract."""


def validate_text(
    value: str, *, field: str, max_length: int, min_length: int = 0
) -> str:
    if not isinstance(value, str):
        raise InputPolicyError(f"{field} must be a string")
    if _INVALID_TEXT.search(value):
        raise InputPolicyError(f"{field} contains an invalid character")
    if not min_length <= len(value) <= max_length:
        raise InputPolicyError(
            f"{field} must contain {min_length}..{max_length} characters"
        )
    return value


def normalize_short_text(
    value: str | None, *, field: str, max_length: int, optional: bool = False
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise InputPolicyError(f"{field} must be a string")
    if any(
        ord(ch) < 32
        or 0x7F <= ord(ch) <= 0x9F
        or 0xD800 <= ord(ch) <= 0xDFFF
        or ch in "\u2028\u2029"
        for ch in value
    ):
        raise InputPolicyError(
            f"{field} must be a single line without control characters"
        )
    normalized = value.strip(SHORT_TEXT_SPACES)
    return validate_text(normalized, field=field, max_length=max_length, min_length=1)


def validate_title(
    value: str | None, *, max_length: int = CONTENT_TITLE_MAX, optional: bool = False
) -> str | None:
    return normalize_short_text(
        value, field="title", max_length=max_length, optional=optional
    )


def validate_body(value: str, *, max_length: int = BODY_MAX) -> str:
    return validate_text(value, field="body", max_length=max_length)


def fit_generated_title(
    value: str, *, max_length: int = CONTENT_TITLE_MAX, fallback: str = "Untitled"
) -> str:
    """Fit a generated display label; explicit authored titles must be rejected.

    Grapheme boundaries protect display sequences, while the code-point budget
    matches stored CHECKs. The source value itself is never changed.
    """
    if max_length < 1:
        raise ValueError("generated title budget must be positive")
    for candidate in (value, fallback, "Untitled"):
        safe = "".join(
            " " if ord(ch) < 32 or 127 <= ord(ch) <= 159 or ch.isspace() else ch
            for ch in candidate
            if not 0xD800 <= ord(ch) <= 0xDFFF
        )
        safe = " ".join(safe.split())
        parts = []
        remaining = max_length
        for cluster in grapheme_clusters(safe):
            if len(cluster) > remaining:
                break
            parts.append(cluster)
            remaining -= len(cluster)
        fitted = "".join(parts).strip(SHORT_TEXT_SPACES)
        if fitted:
            return validate_title(fitted, max_length=max_length)
    raise ValueError("generated title fallback must fit its budget")


def normalize_ask_user_message(value: str) -> str:
    if not isinstance(value, str) or _INVALID_TEXT.search(value):
        raise InputPolicyError("user_message contains invalid characters")
    # Empty messages retain the Ask service's invalid_user_message response;
    # this shared schema boundary only aligns normalization and the upper limit.
    return validate_text(value.strip(), field="user_message", max_length=8000)


def validate_list_count(value, *, field: str, max_items: int):
    if value is not None and len(value) > max_items:
        raise InputPolicyError(f"{field} must contain at most {max_items} items")
    return value


def validate_labels(values, *, field: str, max_length: int):
    if values is None:
        return None
    if not isinstance(values, (list, tuple)):
        raise InputPolicyError(f"{field} must be a list")
    validate_list_count(values, field=field, max_items=32)
    return [
        normalize_short_text(value, field=field, max_length=max_length)
        for value in values
    ]
