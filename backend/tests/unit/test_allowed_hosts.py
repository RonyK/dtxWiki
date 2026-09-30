"""Host header normalisation and DTXWIKI_ALLOWED_HOSTS parsing."""

from __future__ import annotations

import pytest

from backend.policy.allowed_hosts import (
    LOOPBACK_HOSTS,
    AllowedHostsError,
    effective_allowed_hosts,
    host_is_allowed,
    mcp_transport_allowlists,
    normalize_host,
    parse_allowed_hosts,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Wiki.Example.com", "wiki.example.com"),
        ("wiki.example.com:8443", "wiki.example.com"),
        ("wiki.example.com.", "wiki.example.com"),
        ("127.0.0.1:21080", "127.0.0.1"),
        ("[::1]:21080", "::1"),
        ("[::1]", "::1"),
        ("::1", "::1"),
        ("dtxwiki_app:21080", "dtxwiki_app"),
    ],
)
def test_normalize_host(value, expected):
    assert normalize_host(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "x/search?",
        "wiki.example.com:http",
        "wiki.example.com:",
        "user@wiki.example.com",
        "[::1",
        "[::1]x",
        "wiki example.com",
    ],
)
def test_values_that_are_not_hosts_normalize_to_none(value):
    assert normalize_host(value) is None


def test_default_allowlist_is_loopback_plus_the_public_host():
    assert effective_allowed_hosts(configured=None, public_base_url=None) == (
        LOOPBACK_HOSTS
    )
    assert effective_allowed_hosts(
        configured=None, public_base_url="https://Wiki.Example.com:8443/base"
    ) == LOOPBACK_HOSTS | {"wiki.example.com"}


def test_configured_entries_drop_ports_and_schemes():
    hosts, wildcard = parse_allowed_hosts(
        " lan-box.local:21080 , https://Other.Example/path,[2001:db8::1]:443,,"
    )

    assert hosts == {"lan-box.local", "other.example", "2001:db8::1"}
    assert wildcard is False


def test_wildcard_disables_the_allowlist():
    assert effective_allowed_hosts(configured="*", public_base_url=None) is None


def test_invalid_entry_is_reported_not_skipped():
    with pytest.raises(AllowedHostsError, match="'wiki example.com'"):
        parse_allowed_hosts("ok.example, wiki example.com")


def test_host_is_allowed_compares_normalised_hosts():
    allowed = effective_allowed_hosts(configured="lan-box.local", public_base_url=None)

    assert host_is_allowed("LAN-BOX.local:21080", allowed)
    assert host_is_allowed("[::1]:21080", allowed)
    assert not host_is_allowed("lan-box.local.attacker.example", allowed)
    assert not host_is_allowed("x/search?", allowed)
    assert not host_is_allowed(None, allowed)


def test_mcp_allowlists_cover_every_port_and_both_schemes():
    hosts, origins = mcp_transport_allowlists(
        frozenset({"::1", "wiki.example.com"}),
        extra_origins=["https://client.example/"],
    )

    assert hosts == ["[::1]", "[::1]:*", "wiki.example.com", "wiki.example.com:*"]
    assert "http://[::1]:*" in origins
    assert "https://wiki.example.com" in origins
    assert "https://wiki.example.com:*" in origins
    assert "https://client.example" in origins
