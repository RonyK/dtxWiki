"""Framework-free HTTP content selection for authorized immutable bytes.

Domain resolvers must authorize a concrete Library version or Context usage
before calling these helpers.  The helpers only normalize conditional and
single-range headers; they never resolve a blob or grant access to one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_MAX_HTTP_RANGE_BYTES = 8 * 1024 * 1024
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$", re.IGNORECASE)
_MAX_RANGE_NUMBER_DIGITS = 20


class ByteRangeNotSatisfiable(ValueError):
    """A Range header cannot be served as one bounded byte range."""

    def __init__(self, *, complete_length: int) -> None:
        super().__init__("The requested content byte range is invalid.")
        self.complete_length = complete_length


@dataclass(frozen=True, slots=True)
class HttpByteRange:
    """One normalized inclusive HTTP byte range."""

    start: int
    end: int
    complete_length: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.complete_length}"


def parse_http_byte_range(
    header_value: str | None,
    *,
    complete_length: int,
    max_range_bytes: int = DEFAULT_MAX_HTTP_RANGE_BYTES,
    clamp_open_ended: bool = False,
) -> HttpByteRange | None:
    """Parse one RFC 9110-style bytes range and enforce a response-size cap.

    ``clamp_open_ended`` is reserved for seekable media transports. Browsers
    commonly request ``bytes=<offset>-`` and expect the server to choose a
    bounded response window before issuing the next seek request. Explicit
    oversized end and suffix ranges remain rejected.
    """

    if complete_length < 0:
        raise ValueError("complete_length must not be negative")
    if max_range_bytes <= 0:
        raise ValueError("max_range_bytes must be positive")
    if header_value is None:
        return None

    value = header_value.strip()
    if "," in value:
        raise ByteRangeNotSatisfiable(complete_length=complete_length)
    match = _RANGE_RE.fullmatch(value)
    if match is None:
        raise ByteRangeNotSatisfiable(complete_length=complete_length)
    first_text, last_text = match.groups()
    if not first_text and not last_text:
        raise ByteRangeNotSatisfiable(complete_length=complete_length)
    if complete_length == 0:
        raise ByteRangeNotSatisfiable(complete_length=complete_length)
    if any(
        len(component) > _MAX_RANGE_NUMBER_DIGITS
        for component in (first_text, last_text)
    ):
        raise ByteRangeNotSatisfiable(complete_length=complete_length)

    if not first_text:
        suffix_length = int(last_text)
        if suffix_length <= 0:
            raise ByteRangeNotSatisfiable(complete_length=complete_length)
        length = min(suffix_length, complete_length)
        start = complete_length - length
        end = complete_length - 1
    else:
        start = int(first_text)
        if start >= complete_length:
            raise ByteRangeNotSatisfiable(complete_length=complete_length)
        if last_text:
            requested_end = int(last_text)
            if requested_end < start:
                raise ByteRangeNotSatisfiable(complete_length=complete_length)
            end = min(requested_end, complete_length - 1)
        else:
            end = (
                min(start + max_range_bytes - 1, complete_length - 1)
                if clamp_open_ended
                else complete_length - 1
            )
        length = end - start + 1

    if length > max_range_bytes:
        raise ByteRangeNotSatisfiable(complete_length=complete_length)
    return HttpByteRange(start=start, end=end, complete_length=complete_length)


def if_none_match_matches(header_value: str | None, etag: str) -> bool:
    """Apply weak comparison to an ``If-None-Match`` field value."""

    if header_value is None:
        return False
    expected = _weak_etag(etag)
    return any(
        candidate == "*" or _weak_etag(candidate) == expected
        for candidate in header_value.split(",")
        if candidate.strip()
    )


def if_range_allows_range(header_value: str | None, etag: str) -> bool:
    """Return whether a Range may be applied for a strong opaque ETag."""

    if header_value is None:
        return True
    candidate = header_value.strip()
    if candidate.startswith("W/"):
        return False
    return candidate == etag


def _weak_etag(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    return normalized


__all__ = [
    "ByteRangeNotSatisfiable",
    "DEFAULT_MAX_HTTP_RANGE_BYTES",
    "HttpByteRange",
    "if_none_match_matches",
    "if_range_allows_range",
    "parse_http_byte_range",
]
