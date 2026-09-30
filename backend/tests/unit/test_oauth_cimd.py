from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from backend.services import oauth_cimd
from backend.services.oauth_cimd import CIMDFetchError

CLIENT_ID = "https://client.example/oauth/client.json"
PUBLIC_IP = "93.184.216.34"


def _dns_record(address: str):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
    return (family, socket.SOCK_STREAM, 6, "", sockaddr)


def _valid_body(client_id: str = CLIENT_ID) -> bytes:
    return json.dumps(
        {
            "client_id": client_id,
            "client_name": "CIMD Test Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read mcp:write",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "application_type": "native",
            "token_endpoint_auth_method": "none",
        }
    ).encode()


def _response(status: int, *, content_type: str = "application/json", **headers):
    return httpx.Response(
        status,
        headers={"content-type": content_type, **headers},
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.1.1",
        "169.254.169.254",
        "::1",
        "fd00::1",
        "fd00:ec2::254",
        "224.0.0.1",
        "ff02::1",
        "fec0::1",
        "240.0.0.1",
        "2001:db8::1",
    ],
)
def test_rejects_non_public_address_classes(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_dns_record(address)],
    )

    with pytest.raises(CIMDFetchError):
        oauth_cimd._resolve_public_destination(CLIENT_ID)


def test_name_is_rejected_when_any_resolved_address_is_private(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            _dns_record(PUBLIC_IP),
            _dns_record("10.0.0.9"),
        ],
    )

    with pytest.raises(CIMDFetchError):
        oauth_cimd._resolve_public_destination(CLIENT_ID)


@pytest.mark.parametrize(
    "client_id",
    [
        "http://client.example/client.json",
        "ftp://client.example/client.json",
        "file:///etc/passwd",
        "javascript:alert(1)",
    ],
)
def test_rejects_non_https_schemes(client_id):
    with pytest.raises(CIMDFetchError):
        oauth_cimd._fetch_uncached(client_id)


def test_redirect_to_private_address_is_rejected(monkeypatch):
    requests: list[str] = []

    def resolve(host, *args, **kwargs):
        address = PUBLIC_IP if host == "client.example" else "169.254.169.254"
        return [_dns_record(address)]

    def request(url, **kwargs):
        requests.append(url)
        return _response(302, location="https://metadata.internal/client"), b""

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(oauth_cimd, "_request_once", request)

    with pytest.raises(CIMDFetchError):
        oauth_cimd._fetch_uncached(CLIENT_ID)
    assert requests == [CLIENT_ID]


def test_redirect_count_is_capped(monkeypatch):
    requests: list[str] = []
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_dns_record(PUBLIC_IP)],
    )

    def request(url, **kwargs):
        requests.append(url)
        return _response(302, location=f"/redirect/{len(requests)}"), b""

    monkeypatch.setattr(oauth_cimd, "_request_once", request)

    with pytest.raises(CIMDFetchError):
        oauth_cimd._fetch_uncached(CLIENT_ID)
    assert len(requests) == oauth_cimd.CIMD_MAX_REDIRECTS + 1


def test_wrong_content_type_is_rejected_without_sniffing(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_dns_record(PUBLIC_IP)],
    )
    monkeypatch.setattr(
        oauth_cimd,
        "_request_once",
        lambda *args, **kwargs: (
            _response(200, content_type="text/plain"),
            _valid_body(),
        ),
    )

    with pytest.raises(CIMDFetchError):
        oauth_cimd._fetch_uncached(CLIENT_ID)


class _StreamResponse:
    def __init__(self, chunks, *, headers=None):
        self.headers = headers or {"content-type": "application/json"}
        self.status_code = 200
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def iter_bytes(self):
        yield from self._chunks


class _FakeHTTPClient:
    response: _StreamResponse

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        return self.response


def test_oversized_document_is_rejected(monkeypatch):
    _FakeHTTPClient.response = _StreamResponse(
        [b"x" * (oauth_cimd.CIMD_MAX_BYTES + 1)]
    )
    monkeypatch.setattr(oauth_cimd, "_PinnedHTTPTransport", lambda **kwargs: object())
    monkeypatch.setattr(oauth_cimd.httpx, "Client", _FakeHTTPClient)

    with pytest.raises(CIMDFetchError):
        oauth_cimd._request_once(
            CLIENT_ID,
            expected_host="client.example",
            pinned_addresses=(PUBLIC_IP,),
            started_at=oauth_cimd.time.monotonic(),
        )


def test_slow_document_is_rejected(monkeypatch):
    _FakeHTTPClient.response = _StreamResponse([b"{}"])
    monkeypatch.setattr(oauth_cimd, "_PinnedHTTPTransport", lambda **kwargs: object())
    monkeypatch.setattr(oauth_cimd.httpx, "Client", _FakeHTTPClient)
    monkeypatch.setattr(
        oauth_cimd.time,
        "monotonic",
        lambda: oauth_cimd.CIMD_TOTAL_TIMEOUT_SECONDS + 1,
    )

    with pytest.raises(CIMDFetchError):
        oauth_cimd._request_once(
            CLIENT_ID,
            expected_host="client.example",
            pinned_addresses=(PUBLIC_IP,),
            started_at=0.0,
        )


def test_dns_resolution_is_pinned_against_rebinding(monkeypatch):
    resolutions = iter(
        [
            [_dns_record(PUBLIC_IP)],
            [_dns_record("10.0.0.7")],
        ]
    )
    resolve_calls = 0

    def resolve(*args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return next(resolutions)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    host, addresses = oauth_cimd._resolve_public_destination(CLIENT_ID)
    connected: list[str] = []
    backend = oauth_cimd._PinnedNetworkBackend(
        expected_host=host,
        addresses=addresses,
    )

    class Delegate:
        def connect_tcp(self, address, *args, **kwargs):
            connected.append(address)
            return object()

    backend._backend = Delegate()
    backend.connect_tcp(host, 443)

    assert resolve_calls == 1
    assert connected == [PUBLIC_IP]


def test_success_is_cached_with_bounded_ttl(monkeypatch):
    now = 100.0
    request_count = 0
    monkeypatch.setattr(oauth_cimd.time, "monotonic", lambda: now)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_dns_record(PUBLIC_IP)],
    )

    def request(*args, **kwargs):
        nonlocal request_count
        request_count += 1
        return _response(200), _valid_body()

    monkeypatch.setattr(oauth_cimd, "_request_once", request)

    first = oauth_cimd.fetch_client_metadata_document(CLIENT_ID)
    second = oauth_cimd.fetch_client_metadata_document(CLIENT_ID)
    assert first == second
    assert request_count == 1

    now += oauth_cimd.CIMD_CACHE_TTL_SECONDS + 1
    oauth_cimd.fetch_client_metadata_document(CLIENT_ID)
    assert request_count == 2


def test_failed_fetch_is_negative_cached_until_ttl_expires(monkeypatch):
    """MCP-08: before this fix a failure was never cached, so a session
    hitting a repeatedly bad/slow client_id re-paid a full DNS+TLS+HTTP
    attempt (up to CIMD_TOTAL_TIMEOUT_SECONDS) on every /oauth/authorize
    call. A failure must now be negative-cached for CIMD_FAILURE_TTL_SECONDS
    -- retried only once that (shorter than success's) TTL expires."""

    oauth_cimd.clear_cimd_cache()
    now = 100.0
    calls = 0
    monkeypatch.setattr(oauth_cimd.time, "monotonic", lambda: now)

    def fetch(client_id, *, started_at=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CIMDFetchError()
        return oauth_cimd._parse_metadata(client_id, _valid_body(client_id))

    monkeypatch.setattr(oauth_cimd, "_fetch_uncached", fetch)

    with pytest.raises(CIMDFetchError):
        oauth_cimd.fetch_client_metadata_document(CLIENT_ID)
    assert calls == 1

    # Still inside the failure TTL: must fail fast from the negative cache
    # rather than pay for another real fetch attempt.
    with pytest.raises(CIMDFetchError):
        oauth_cimd.fetch_client_metadata_document(CLIENT_ID)
    assert calls == 1

    now += oauth_cimd.CIMD_FAILURE_TTL_SECONDS + 1
    assert oauth_cimd.fetch_client_metadata_document(CLIENT_ID).client_id == CLIENT_ID
    assert calls == 2


def test_inflight_fetches_are_capped_across_client_ids(monkeypatch):
    """MCP-08: CIMD_MAX_INFLIGHT bounds concurrent real fetches across ALL
    client_ids (on top of the existing per-client_id single-flight lock), so
    a handful of slow/malicious client_ids cannot each pin a request thread
    for the full timeout. A distinct client_id past the cap must fail fast,
    not queue."""

    oauth_cimd.clear_cimd_cache()
    entered = threading.Barrier(oauth_cimd.CIMD_MAX_INFLIGHT + 1, timeout=2)
    release = threading.Event()

    def blocking_fetch(client_id, *, started_at=None):
        entered.wait(timeout=2)
        assert release.wait(timeout=2)
        return oauth_cimd._parse_metadata(client_id, _valid_body(client_id))

    monkeypatch.setattr(oauth_cimd, "_fetch_uncached", blocking_fetch)

    client_ids = [
        f"https://client-{i}.example/oauth/client.json"
        for i in range(oauth_cimd.CIMD_MAX_INFLIGHT)
    ]
    try:
        with ThreadPoolExecutor(max_workers=len(client_ids)) as executor:
            futures = [
                executor.submit(oauth_cimd.fetch_client_metadata_document, cid)
                for cid in client_ids
            ]
            # All CIMD_MAX_INFLIGHT fetches are now past the semaphore
            # acquire and blocked inside _fetch_uncached.
            entered.wait(timeout=2)

            started_at = time.monotonic()
            with pytest.raises(CIMDFetchError):
                oauth_cimd.fetch_client_metadata_document(
                    "https://client-overflow.example/oauth/client.json"
                )
            assert time.monotonic() - started_at < 0.5

            release.set()
            for client_id, future in zip(client_ids, futures, strict=True):
                assert future.result(timeout=2).client_id == client_id
    finally:
        release.set()


def test_dns_resolution_is_bounded_by_total_deadline(monkeypatch):
    release = threading.Event()

    def slow_resolve(*args, **kwargs):
        release.wait(timeout=0.2)
        return [_dns_record(PUBLIC_IP)]

    monkeypatch.setattr(socket, "getaddrinfo", slow_resolve)
    monkeypatch.setattr(oauth_cimd, "CIMD_TOTAL_TIMEOUT_SECONDS", 0.02)
    started_at = time.monotonic()
    try:
        with pytest.raises(CIMDFetchError):
            oauth_cimd._fetch_uncached(CLIENT_ID)
    finally:
        release.set()
    assert time.monotonic() - started_at < 0.15


def test_single_flight_lock_wait_is_bounded_by_total_deadline(monkeypatch):
    oauth_cimd.clear_cimd_cache()
    entered = threading.Event()
    release = threading.Event()

    def blocking_fetch(client_id, *, started_at=None):
        entered.set()
        release.wait(timeout=0.5)
        return oauth_cimd._parse_metadata(client_id, _valid_body(client_id))

    monkeypatch.setattr(oauth_cimd, "_fetch_uncached", blocking_fetch)
    monkeypatch.setattr(oauth_cimd, "CIMD_TOTAL_TIMEOUT_SECONDS", 0.02)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(oauth_cimd.fetch_client_metadata_document, CLIENT_ID)
        assert entered.wait(timeout=1)
        started_at = time.monotonic()
        with pytest.raises(CIMDFetchError):
            oauth_cimd.fetch_client_metadata_document(CLIENT_ID)
        assert time.monotonic() - started_at < 0.15
        release.set()
        assert first.result(timeout=1).client_id == CLIENT_ID


def test_address_predicate_allows_public_control():
    assert not oauth_cimd.address_is_forbidden(ipaddress.ip_address(PUBLIC_IP))
