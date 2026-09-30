"""Sprint_5 Phase_3 Task 3.1 — ADR A2 loopback redirect URI hardening.

Negative tests pair every assertion with a same-setup positive control or a
pinned failure-mode keyword per ADR A6. The positive control test
`test_arbitrary_runtime_port_matches_registered` also confirms the existing
`redirect_uri_matches` runtime-port exception (RFC 8252 §7.3) is preserved.
"""

from __future__ import annotations

import pytest

from backend.services.oauth_service import (
    _LOOPBACK_CANONICAL_HOSTS,
    InvalidClientRegistrationError,
    redirect_uri_matches,
    validate_redirect_uri,
)

# ---------------------------------------------------------------------------
# Positive controls (ADR A2 §1: canonical loopback hosts accepted)
# ---------------------------------------------------------------------------


def test_loopback_localhost_accepted() -> None:
    assert validate_redirect_uri("http://localhost:54321/cb") == (
        "http://localhost:54321/cb"
    )


def test_loopback_127_accepted() -> None:
    assert validate_redirect_uri("http://127.0.0.1:60000/cb") == (
        "http://127.0.0.1:60000/cb"
    )


def test_loopback_ipv6_accepted() -> None:
    assert validate_redirect_uri("http://[::1]:60000/cb") == (
        "http://[::1]:60000/cb"
    )


def test_canonical_set_shared_with_is_loopback_host() -> None:
    """Both DCR-time validation and runtime matching must agree on the
    canonical loopback host whitelist (Sprint_4 H4 finding)."""

    assert _LOOPBACK_CANONICAL_HOSTS == frozenset(
        {"localhost", "127.0.0.1", "::1"}
    )


# ---------------------------------------------------------------------------
# Negative tests with pinned failure-mode keywords (ADR A6)
# ---------------------------------------------------------------------------


def test_non_loopback_http_rejected_with_keyword() -> None:
    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://example.com/cb")
    assert "loopback" in str(exc.value)


def test_port_zero_rejected() -> None:
    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://127.0.0.1:0/cb")
    assert "port" in str(exc.value)


def test_port_out_of_range_rejected() -> None:
    """urlparse raises ValueError on ports above 65535; the validator
    converts it to InvalidClientRegistrationError with a port-keyword
    message so the failure mode is unambiguous."""

    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://127.0.0.1:99999/cb")
    assert "port" in str(exc.value)


def test_trailing_dot_localhost_rejected() -> None:
    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://localhost.:5000/cb")
    assert "trailing dot" in str(exc.value)


def test_trailing_dot_ipv4_rejected() -> None:
    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://127.0.0.1.:5000/cb")
    assert "trailing dot" in str(exc.value)


def test_non_canonical_ipv4_octets_rejected() -> None:
    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://127.000.000.001:5000/cb")
    assert "canonical" in str(exc.value)


def test_non_canonical_ipv4_shorthand_rejected() -> None:
    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://127.1:5000/cb")
    assert "canonical" in str(exc.value)


def test_ipv6_zone_id_rejected() -> None:
    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://[::1%lo0]:5000/cb")
    assert "zone identifier" in str(exc.value)


def test_ipv6_zone_id_percent_encoded_rejected() -> None:
    """A native client may percent-encode the zone delimiter as `%25`.
    The validator must reject the URL-encoded form as well so the bypass
    is closed."""

    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://[::1%25lo0]:5000/cb")
    assert "zone identifier" in str(exc.value)


def test_fragment_redirect_uri_rejected_with_keyword() -> None:
    """ADR A2: registered redirect URIs must not carry fragments.

    OAuth redirects append authorization response parameters to the query
    component; allowing a registered fragment would create ambiguous client-side
    interpretation and must be rejected with a pinned keyword.
    """

    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://127.0.0.1:5000/cb#frag")
    assert "fragment" in str(exc.value)


# ---------------------------------------------------------------------------
# Sprint_5 Phase_3 Task 3.7 (SEC-FR3MID-1): http loopback URI requires an
# explicit port. RFC 8252 §7.3 says the native client picks a runtime port
# and registers it; accepting the missing-port form would let a later
# runtime match consult only host + path, which is not the RFC's intent.
# https paths still accept the implicit port (default 443).
# ---------------------------------------------------------------------------


def test_http_loopback_without_port_rejected() -> None:
    """Negative pin: ``http://127.0.0.1/cb`` (no port) must be rejected
    with the ``port is required`` keyword."""

    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://127.0.0.1/cb")
    assert "port is required" in str(exc.value)


def test_http_localhost_without_port_rejected() -> None:
    """Same rule for ``http://localhost/cb`` — RFC 8252 §7.3 requires
    a concrete runtime port."""

    with pytest.raises(InvalidClientRegistrationError) as exc:
        validate_redirect_uri("http://localhost/cb")
    assert "port is required" in str(exc.value)


def test_https_without_port_accepted() -> None:
    """Positive control paired with the http-port-required negatives:
    https default-port form is allowed because https implies 443."""

    assert (
        validate_redirect_uri("https://example.com/cb")
        == "https://example.com/cb"
    )


# ---------------------------------------------------------------------------
# Positive control: RFC 8252 §7.3 arbitrary runtime port match preserved.
# Sprint_4 hardening (line 832-857) must continue to accept any port at
# runtime for registered loopback URIs after Task 3.1.
# ---------------------------------------------------------------------------


def test_arbitrary_runtime_port_matches_registered() -> None:
    registered = ["http://127.0.0.1:5000/cb"]
    assert redirect_uri_matches("http://127.0.0.1:62345/cb", registered) is True


def test_runtime_port_match_accepts_same_query() -> None:
    """The RFC 8252 loopback exception permits only a port delta; a fixed
    registered query string remains part of the redirect URI contract."""

    registered = ["http://127.0.0.1:5000/cb?fixed=1"]
    assert (
        redirect_uri_matches("http://127.0.0.1:62345/cb?fixed=1", registered)
        is True
    )


def test_runtime_port_match_rejects_query_mismatch() -> None:
    """Negative control: runtime loopback matching must not treat a query
    mismatch as merely another runtime-port variation."""

    registered = ["http://127.0.0.1:5000/cb?fixed=1"]
    assert (
        redirect_uri_matches("http://127.0.0.1:62345/cb?evil=1", registered)
        is False
    )


def test_runtime_port_match_rejects_requested_fragment() -> None:
    """Negative control: authorization-request redirect URIs must not add a
    fragment while using the runtime-port exception."""

    registered = ["http://127.0.0.1:5000/cb"]
    assert redirect_uri_matches("http://127.0.0.1:62345/cb#frag", registered) is False


def test_runtime_port_match_rejects_registered_fragment() -> None:
    """Defense in depth: even a legacy/bad registered URI with a fragment must
    not participate in the runtime-port exception."""

    registered = ["http://127.0.0.1:5000/cb#frag"]
    assert redirect_uri_matches("http://127.0.0.1:62345/cb", registered) is False


def test_runtime_port_match_rejects_requested_missing_port() -> None:
    """Runtime loopback requests must still carry an explicit valid port."""

    registered = ["http://127.0.0.1:5000/cb"]
    assert redirect_uri_matches("http://127.0.0.1/cb", registered) is False


def test_runtime_port_match_rejects_requested_port_zero() -> None:
    registered = ["http://127.0.0.1:5000/cb"]
    assert redirect_uri_matches("http://127.0.0.1:0/cb", registered) is False


def test_runtime_port_match_rejects_requested_out_of_range_port() -> None:
    registered = ["http://127.0.0.1:5000/cb"]
    assert redirect_uri_matches("http://127.0.0.1:99999/cb", registered) is False


def test_runtime_port_match_rejects_registered_missing_port() -> None:
    registered = ["http://127.0.0.1/cb"]
    assert redirect_uri_matches("http://127.0.0.1:62345/cb", registered) is False


def test_runtime_port_match_rejects_non_loopback_host() -> None:
    """Negative control paired with the positive RFC 8252 §7.3 test above."""

    registered = ["http://example.com:5000/cb"]
    assert redirect_uri_matches("http://example.com:62345/cb", registered) is False
