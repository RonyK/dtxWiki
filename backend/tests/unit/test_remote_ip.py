"""resolve_remote_ip must not trust a client-injected X-Forwarded-For (Codex #3)."""

from backend.app.settings import Settings
from backend.policy.remote_ip import resolve_remote_ip


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, peer, headers):
        self.client = _FakeClient(peer) if peer else None
        self.headers = headers  # lowercase keys; resolve uses .get()


def _resolve(
    peer, *, xff=None, xri=None, allow="10.0.0.1", behind=True, trust_xri=False
):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    if xri is not None:
        headers["x-real-ip"] = xri
    request = _FakeRequest(peer, headers)
    settings = Settings(
        behind_proxy=behind,
        forwarded_allow_ips=allow,
        trust_x_real_ip=trust_xri,
    )
    return resolve_remote_ip(request, settings)


def test_prefers_x_real_ip_when_opted_in():
    assert _resolve(
        "10.0.0.1", xff="9.9.9.9, 10.0.0.1", xri="203.0.113.5", trust_xri=True
    ) == "203.0.113.5"


def test_x_real_ip_ignored_without_opt_in():
    # A proxy that forwards (rather than overwrites) X-Real-IP must not let a
    # client spoof the identity used for rate limits / metrics allowlist.
    assert _resolve(
        "10.0.0.1", xff="9.9.9.9, 10.0.0.1", xri="127.0.0.1"
    ) == "9.9.9.9"


def test_x_real_ip_ignored_without_opt_in_falls_back_to_peer():
    assert _resolve("10.0.0.1", xri="127.0.0.1") == "10.0.0.1"


def test_x_real_ip_opt_in_does_not_apply_to_untrusted_peer():
    assert _resolve(
        "8.8.8.8", xri="127.0.0.1", allow="10.0.0.1", trust_xri=True
    ) == "8.8.8.8"


def test_rightmost_untrusted_hop_through_trusted_chain():
    # client -> proxy 10.0.0.2 -> our peer 10.0.0.1 ; both proxies trusted.
    assert _resolve(
        "10.0.0.1", xff="203.0.113.42, 10.0.0.2", allow="10.0.0.1,10.0.0.2"
    ) == "203.0.113.42"


def test_spoofed_leftmost_is_not_trusted():
    # Only the immediate peer is trusted; the injected leftmost must not win.
    assert _resolve("10.0.0.1", xff="1.2.3.4, 203.0.113.42", allow="10.0.0.1") == (
        "203.0.113.42"
    )


def test_untrusted_peer_returns_peer():
    assert _resolve("8.8.8.8", xff="1.2.3.4", allow="10.0.0.1") == "8.8.8.8"


def test_not_behind_proxy_returns_peer():
    assert _resolve("10.0.0.1", xff="1.2.3.4", behind=False) == "10.0.0.1"
