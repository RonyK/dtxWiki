"""The one place that decides what this server's public origin is.

Every consumer that needs the canonical origin, the MCP endpoint, or whether
OAuth can be offered at all reads it from here, so they cannot disagree. They
used to: ``/auth/me`` answered from a scheme check on the configured string
while the Settings screen ANDed that with a check on the URL the *browser*
happened to reach the app on, and the same page could say "OAuth is available"
above a card reading "OAuth apps — Unavailable".

**No network I/O.** Nothing here resolves DNS, opens a socket, or validates a
certificate, and capability reads must never start doing so: the app boots
before the reverse proxy in both the installer and the compose topology, so a
probe at startup would test a proxy that is not listening yet, and caching that
failure would strand a server that came up correctly a second later. The
server's own view of its public address is also not the view that matters --
an MCP client on another network, with another trust store, is the one that has
to reach it. Proving the origin really answers over TLS is the job of the
post-setup diagnostic (``tests/packaging/production_smoke.sh``), which runs once
the proxy is up and reports as of when it ran.

So the contract here is deliberately narrower than it sounds: "the
configuration permits offering OAuth at this origin", never "this origin was
reached and its certificate verified".
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = [
    "PublicOrigin",
    "resolve_public_origin",
]

# Reasons are stable machine codes; the message beside each is what an operator
# reads. Keep them in sync with the frontend's unavailable-reason rendering.
_REASON_NOT_CONFIGURED = "not_configured"
_REASON_NOT_HTTPS = "not_https"
_REASON_MALFORMED = "malformed"


@dataclass(frozen=True)
class PublicOrigin:
    """The resolved public origin, or why it could not be resolved."""

    origin: str | None
    """Canonical origin with no trailing slash, exactly as configured.

    Byte-identical to the configured value minus surrounding whitespace and a
    trailing slash. It is NOT case-folded or otherwise normalised: this string
    is the OAuth issuer, and rewriting it here would silently invalidate every
    token already signed against the configured spelling.
    """

    https_configured: bool
    """The origin is present, well-formed, and uses https."""

    reason: str | None
    """Machine code for why OAuth cannot be offered, or None when it can."""

    message: str | None
    """Operator-facing explanation matching ``reason``."""

    @property
    def mcp_url(self) -> str | None:
        """The MCP endpoint under this origin, or None when unresolved.

        Root-mounted, matching where the app actually serves ``/mcp`` and
        ``/.well-known``. A non-empty path prefix is rejected rather than
        supported, because supporting it would need the routers, the discovery
        documents and the recipes to agree on it too.
        """

        if self.origin is None:
            return None
        return f"{self.origin}/mcp"

    @property
    def oauth_available(self) -> bool:
        """The configuration permits offering OAuth at this origin.

        Not "the certificate was verified" and not "a client reached it" --
        see the module docstring. DCR is a separate capability and never gates
        this: a client using an https URL client identifier authorises fine
        with self-registration switched off.
        """

        return self.https_configured


def _has_control_characters(value: str) -> bool:
    return any(character < " " or character == "\x7f" for character in value)


def resolve_public_origin(public_base_url: str | None) -> PublicOrigin:
    """Validate the configured public origin without touching the network."""

    value = (public_base_url or "").strip()
    if not value:
        return PublicOrigin(
            origin=None,
            https_configured=False,
            reason=_REASON_NOT_CONFIGURED,
            message=(
                "No public URL is configured. OAuth needs one: it is the token "
                "issuer, and the address MCP clients are sent to."
            ),
        )

    if _has_control_characters(value):
        return PublicOrigin(
            origin=None,
            https_configured=False,
            reason=_REASON_MALFORMED,
            message="The configured public URL contains control characters.",
        )

    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()

    if scheme not in {"http", "https"}:
        return PublicOrigin(
            origin=None,
            https_configured=False,
            reason=_REASON_MALFORMED,
            message=(
                "The configured public URL must be an http or https URL; "
                f"got scheme {parsed.scheme!r}."
            ),
        )

    # `hostname` is None for an empty authority and lowercases the host for the
    # check only -- the returned origin keeps the configured spelling.
    if not parsed.hostname:
        return PublicOrigin(
            origin=None,
            https_configured=False,
            reason=_REASON_MALFORMED,
            message="The configured public URL has no host.",
        )

    # Credentials in the origin would end up in the issuer, in discovery
    # documents, and in the setup commands shown on screen.
    if parsed.username is not None or parsed.password is not None:
        return PublicOrigin(
            origin=None,
            https_configured=False,
            reason=_REASON_MALFORMED,
            message="The configured public URL must not carry a username or password.",
        )

    if parsed.query or parsed.fragment:
        return PublicOrigin(
            origin=None,
            https_configured=False,
            reason=_REASON_MALFORMED,
            message="The configured public URL must not carry a query or fragment.",
        )

    # A path prefix is rejected rather than silently dropped: dropping it would
    # hand out an MCP URL and an issuer the operator did not configure.
    if parsed.path not in {"", "/"}:
        return PublicOrigin(
            origin=None,
            https_configured=False,
            reason=_REASON_MALFORMED,
            message=(
                "The configured public URL must be an origin with no path; "
                f"got path {parsed.path!r}. dtxWiki serves /mcp and "
                "/.well-known at the root."
            ),
        )

    # Raises for a malformed port (urlsplit defers the check to this property).
    try:
        _ = parsed.port
    except ValueError:
        return PublicOrigin(
            origin=None,
            https_configured=False,
            reason=_REASON_MALFORMED,
            message="The configured public URL has an invalid port.",
        )

    origin = value.rstrip("/")

    if scheme != "https":
        return PublicOrigin(
            origin=origin,
            https_configured=False,
            reason=_REASON_NOT_HTTPS,
            message=(
                "OAuth needs an https public URL (it is the token issuer, and "
                "MCP clients refuse a non-https authorization server). This "
                "server's public URL is http — use an Access Key instead."
            ),
        )

    # A privately-issued certificate is a supported deployment (`--tls
    # internal`), so nothing here inspects who signed it or what the host name
    # ends in. Whether a given client trusts that CA is that client's question,
    # and the post-setup diagnostic is where it gets asked.
    return PublicOrigin(
        origin=origin,
        https_configured=True,
        reason=None,
        message=None,
    )
