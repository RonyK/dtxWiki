"""The public-origin policy: what it decides, and what it must never do."""

from __future__ import annotations

import socket
import ssl

import pytest

from backend.app.settings import Settings
from backend.policy.public_origin import resolve_public_origin
from backend.services import deployment


def test_capability_reads_never_touch_the_network(monkeypatch):
    """V01. A capability read must not resolve DNS or open a TLS socket.

    This is the whole reason the check is a configuration check. The app starts
    before its reverse proxy in both the installer and the compose topology, so
    a probe here would test a listener that is not up yet -- and caching that
    answer would strand a server that came up correctly a moment later.
    """

    def explode(*args, **kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError("capability read performed network I/O")

    monkeypatch.setattr(socket, "getaddrinfo", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", explode)

    settings = Settings(public_base_url="https://wiki.example.com")
    assert deployment.oauth_available(settings) is True
    assert deployment.https_configured(settings) is True
    assert deployment.mcp_url(settings) == "https://wiki.example.com/mcp"
    assert deployment.oauth_unavailable_reason(settings) is None


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "http://wiki.example.com",
        "ftp://wiki.example.com",
        "https://",
        "https://user:pw@wiki.example.com",
        "https://wiki.example.com/prefix",
        "https://wiki.example.com?a=1",
        "https://wiki.example.com#frag",
        "https://wiki.example.com\nX-Injected: 1",
    ],
)
def test_unusable_origins_cannot_offer_oauth(url):
    """V02. Missing, plaintext, or malformed origins are refused with a reason."""

    resolved = resolve_public_origin(url)
    assert resolved.oauth_available is False
    assert resolved.https_configured is False
    assert resolved.reason is not None
    assert resolved.message


@pytest.mark.parametrize(
    "url",
    [
        "https://wiki.example.com",
        "https://wiki.example.com/",
        "https://wiki.internal:8443",
        "https://10.0.0.5:8443",
        "https://[2001:db8::1]:8443",
        "https://wiki.test",
    ],
)
def test_private_names_ports_and_ipv6_are_supported(url):
    """V03. A private name, a non-standard port or an IPv6 literal is fine.

    `--tls internal` issues from a local CA for an internal name, so refusing
    anything that is not a public DNS name with a public CA would reject a
    deployment the installer itself offers. Who signed the certificate is a
    question for the client that has to trust it, not for this policy.
    """

    resolved = resolve_public_origin(url)
    assert resolved.oauth_available is True
    assert resolved.reason is None
    assert resolved.mcp_url == f"{url.rstrip('/')}/mcp"


def test_reserved_names_are_configurable_but_not_reachable_by_that_fact():
    """V04. An https `.invalid` origin satisfies CONFIGURATION and nothing more.

    `.invalid` can never resolve, so a server configured this way cannot really
    serve OAuth -- and the E2E harness configures exactly that. The policy still
    reports it configured, because proving reachability is the diagnostic's job
    and blanket-blocking reserved TLDs would also break `.test` and `.localhost`
    deployments that are legitimate on a private network. The distinction is
    reported, not hidden.
    """

    resolved = resolve_public_origin("https://e2e.dtxwiki.invalid")
    assert resolved.https_configured is True
    assert resolved.oauth_available is True
    assert resolved.mcp_url == "https://e2e.dtxwiki.invalid/mcp"


def test_the_issuer_string_is_never_rewritten():
    """The origin is the OAuth issuer, so normalising it would break tokens.

    Only surrounding whitespace and one trailing slash come off. Case is left
    exactly as configured: lowercasing the host here would change the `iss`
    every already-signed token was minted with.
    """

    assert resolve_public_origin("  https://Wiki.Example.COM/  ").origin == (
        "https://Wiki.Example.COM"
    )


def test_dcr_is_independent_of_oauth_availability():
    """V07. Self-registration off does not take the OAuth surface with it.

    A client using an https URL client identifier authorises with DCR off, so
    folding DCR into availability would hide a flow that works.
    """

    settings = Settings(
        public_base_url="https://wiki.example.com",
        oauth_allow_dcr=False,
    )
    assert deployment.oauth_available(settings) is True
    assert deployment.oauth_dcr_enabled(settings) is False
