from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

_DNS_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def normalize_mcp_endpoint_url(
    url: str,
    *,
    allow_http_loopback: bool = False,
) -> str:
    """Validate and normalize one absolute HTTP MCP endpoint URL.

    The endpoint may have a deployment prefix, but its final non-empty path
    segment must be exactly ``mcp``. Credentials and request-specific URL
    components are rejected so they cannot be persisted to client config or
    echoed by diagnostics.
    """

    if not isinstance(url, str):
        raise ValueError("MCP endpoint URL must be a string")
    normalized = url.strip()
    if not normalized:
        raise ValueError("MCP endpoint URL must not be empty")
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in normalized
    ):
        raise ValueError("MCP endpoint URL is malformed")
    if "\\" in normalized or _INVALID_PERCENT_ESCAPE_RE.search(normalized):
        raise ValueError("MCP endpoint URL is malformed")

    # Check delimiters explicitly because urlsplit() does not retain whether
    # an empty query or fragment marker was supplied.
    if "?" in normalized:
        raise ValueError("MCP endpoint URL must not contain a query string")
    if "#" in normalized:
        raise ValueError("MCP endpoint URL must not contain a fragment")

    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        _ = parsed.port  # Force validation of numeric syntax and the valid range.
    except (UnicodeError, ValueError):
        raise ValueError("MCP endpoint URL is malformed") from None

    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError("MCP endpoint URL must be an absolute http(s) URL with a host")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise ValueError("MCP endpoint URL must not contain embedded credentials")

    _validate_authority(parsed.netloc, hostname=hostname)

    endpoint_path = parsed.path.rstrip("/")
    if not endpoint_path.startswith("/") or endpoint_path.rsplit("/", 1)[-1] != "mcp":
        raise ValueError("MCP endpoint URL path must end with the exact segment /mcp")

    if parsed.scheme == "http" and not (
        allow_http_loopback and is_loopback_hostname(hostname)
    ):
        raise ValueError(
            "MCP endpoint URL must use https://; plaintext http:// is allowed "
            "only for an explicitly enabled loopback endpoint"
        )

    return urlunsplit((parsed.scheme.lower(), parsed.netloc, endpoint_path, "", ""))


def is_loopback_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip(".").casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_authority(netloc: str, *, hostname: str) -> None:
    if netloc.startswith("["):
        closing_bracket = netloc.find("]")
        suffix = netloc[closing_bracket + 1 :] if closing_bracket >= 0 else ""
        if closing_bracket < 0 or (suffix and not _valid_port_suffix(suffix)):
            raise ValueError("MCP endpoint URL is malformed")
        try:
            if ipaddress.ip_address(hostname).version != 6:
                raise ValueError("MCP endpoint URL is malformed")
        except ValueError:
            raise ValueError("MCP endpoint URL is malformed") from None
        return

    if netloc.count(":") > 1:
        raise ValueError("MCP endpoint URL is malformed")
    if ":" in netloc and not _valid_port_suffix(netloc[netloc.rfind(":") :]):
        raise ValueError("MCP endpoint URL is malformed")
    _validate_dns_or_ipv4_hostname(hostname)


def _valid_port_suffix(suffix: str) -> bool:
    return suffix.startswith(":") and suffix[1:].isdigit()


def _validate_dns_or_ipv4_hostname(hostname: str) -> None:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        if address.version != 4:
            raise ValueError("MCP endpoint URL is malformed")
        return

    candidate = hostname.rstrip(".")
    try:
        ascii_hostname = candidate.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("MCP endpoint URL is malformed") from None
    if (
        not ascii_hostname
        or len(ascii_hostname) > 253
        or any(
            _DNS_LABEL_RE.fullmatch(label) is None
            for label in ascii_hostname.split(".")
        )
    ):
        raise ValueError("MCP endpoint URL is malformed")
