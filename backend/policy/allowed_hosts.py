"""Host header allowlist shared by the HTTP middleware and the MCP transport.

A browser that resolves an attacker's host name to this server's address
(DNS rebinding) sends that attacker host name in ``Host``. Answering only the
names the deployment is actually reached under closes that path for every
mode, including the default local install. The allowlist is always the
loopback names, the host of ``DTXWIKI_PUBLIC_BASE_URL`` when one is set, and
any names listed in ``DTXWIKI_ALLOWED_HOSTS``. Ports never matter: one host
name may be served on several ports (the Vite dev server, a proxy) and the
port does not change who controls the name.

This module stays free of Settings and framework imports so ``settings.py``
can derive the effective set from it without an import cycle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
WILDCARD = "*"

# Host names, IPv4 literals and (bracket-less) IPv6 literals after
# normalisation. Anything else -- a path, a query, whitespace, userinfo --
# cannot be a host the deployment is reached under.
_HOST_PATTERN = re.compile(r"[a-z0-9._:-]+")


class AllowedHostsError(ValueError):
    """``DTXWIKI_ALLOWED_HOSTS`` contains an entry that is not a host name."""


def normalize_host(value: str | None) -> str | None:
    """Return the lowercase host of a Host header or allowlist entry.

    The port and IPv6 brackets are dropped, as is one trailing dot (``a.b.``
    and ``a.b`` name the same host). ``None`` means the value is not a host
    at all, which callers treat as "not allowed".
    """

    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return None
        host = text[1:end]
        remainder = text[end + 1 :]
        if remainder and not (remainder.startswith(":") and remainder[1:].isdigit()):
            return None
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
        if not port.isdigit():
            return None
    else:
        # A host name, an IPv4 literal, or a bare IPv6 literal (two or more
        # colons, which cannot carry a port without brackets).
        host = text
    if host.endswith("."):
        host = host[:-1]
    if not host or not _HOST_PATTERN.fullmatch(host):
        return None
    return host


def parse_allowed_hosts(raw: str | None) -> tuple[frozenset[str], bool]:
    """Parse ``DTXWIKI_ALLOWED_HOSTS`` into ``(hosts, wildcard)``.

    Entries are comma-separated host names; ports are ignored and a full URL
    is reduced to its host. ``*`` disables the check. An entry that does not
    name a host raises :class:`AllowedHostsError` instead of being skipped: a
    silently ignored typo would leave the operator wondering why their host
    is refused.
    """

    hosts: set[str] = set()
    wildcard = False
    invalid: list[str] = []
    for entry in (raw or "").split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        if candidate == WILDCARD:
            wildcard = True
            continue
        if "://" in candidate:
            candidate = urlsplit(candidate).netloc
        host = normalize_host(candidate)
        if host is None:
            invalid.append(entry.strip())
        else:
            hosts.add(host)
    if invalid:
        raise AllowedHostsError(
            "not a host name: " + ", ".join(repr(item) for item in invalid)
        )
    return frozenset(hosts), wildcard


def effective_allowed_hosts(
    *,
    configured: str | None,
    public_base_url: str | None,
) -> frozenset[str] | None:
    """Return the hosts this deployment answers to, or ``None`` for ``*``."""

    hosts, wildcard = parse_allowed_hosts(configured)
    if wildcard:
        return None
    allowed = set(LOOPBACK_HOSTS) | hosts
    public_host = normalize_host(urlsplit((public_base_url or "").strip()).netloc)
    if public_host is not None:
        allowed.add(public_host)
    return frozenset(allowed)


def host_is_allowed(host_header: str | None, allowed: frozenset[str]) -> bool:
    host = normalize_host(host_header)
    return host is not None and host in allowed


def mcp_transport_allowlists(
    allowed: frozenset[str],
    *,
    extra_origins: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """Translate the allowlist into the MCP SDK's exact-match lists.

    The SDK compares the raw ``Host`` and ``Origin`` header values, with
    ``name:*`` standing for "any port", so every allowed host is listed with
    and without a port, bracketed when it is an IPv6 literal. Configured CORS
    origins are browser clients the operator already trusts and are kept as
    allowed origins too.
    """

    allowed_hosts: list[str] = []
    allowed_origins: list[str] = []
    for host in sorted(allowed):
        literal = f"[{host}]" if ":" in host else host
        allowed_hosts.extend((literal, f"{literal}:*"))
        for scheme in ("http", "https"):
            allowed_origins.extend((f"{scheme}://{literal}", f"{scheme}://{literal}:*"))
    for origin in extra_origins:
        candidate = origin.strip().rstrip("/")
        if candidate and candidate not in allowed_origins:
            allowed_origins.append(candidate)
    return allowed_hosts, allowed_origins


__all__ = [
    "LOOPBACK_HOSTS",
    "WILDCARD",
    "AllowedHostsError",
    "effective_allowed_hosts",
    "host_is_allowed",
    "mcp_transport_allowlists",
    "normalize_host",
    "parse_allowed_hosts",
]
