"""Validate locations before they reach browser Location headers."""

import posixpath
import re
from urllib.parse import unquote, urlsplit


def safe_return_path(value: str | None, default: str = "/dashboard") -> str:
    if not isinstance(value, str) or len(value.encode()) > 2048:
        return default
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or re.search(r"[\x00-\x20\x7f]", value)
    ):
        return default
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return default
    decoded = parsed.path
    # Reject encoded separators/control characters too: proxies and browsers
    # may decode at different stages. Ordinary OAuth query parameters survive.
    for _ in range(8):
        if (
            not decoded.startswith("/")
            or decoded.startswith("//")
            or "\\" in decoded
            or re.search(r"[\x00-\x20\x7f]", decoded)
            or posixpath.normpath(decoded).lower().startswith("/api/")
            or posixpath.normpath(decoded).lower() == "/api"
        ):
            return default
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    else:
        return default
    return value
