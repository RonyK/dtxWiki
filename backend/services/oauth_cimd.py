from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

import httpcore
import httpx

from backend.domain.credential_input import OAUTH_IDENTIFIER_MAX_BYTES
from backend.domain.oauth_input import (
    SUPPORTED_GRANT_TYPES,
    SUPPORTED_RESPONSE_TYPES,
    supported_values,
    validate_client_name,
    validate_identifier,
    validate_redirect_list,
    validate_scope_input,
    validate_supported_metadata,
)
from backend.utils.http_security import PinnedNetworkBackend, address_is_forbidden

CIMD_CACHE_TTL_SECONDS = 300
CIMD_CACHE_MAX_ENTRIES = 256
CIMD_CONNECT_TIMEOUT_SECONDS = 2.0
CIMD_READ_TIMEOUT_SECONDS = 3.0
CIMD_TOTAL_TIMEOUT_SECONDS = 5.0
CIMD_MAX_BYTES = 5 * 1024
CIMD_MAX_REDIRECTS = 3
CIMD_MAX_URL_BYTES = OAUTH_IDENTIFIER_MAX_BYTES
# MCP-08: only successes were cached before this fix, so a repeated
# bad/slow client_id re-paid DNS + TLS + HTTP (up to CIMD_TOTAL_TIMEOUT_SECONDS
# each) on every /oauth/authorize hit. Negative-cache failures for a shorter
# TTL than successes.
CIMD_FAILURE_TTL_SECONDS = 60
# Global concurrency cap across ALL client_ids on top of the existing
# per-client_id serialization -- caps how many /oauth/authorize requests can
# simultaneously pay for a real fetch, so a handful of slow/malicious
# client_ids cannot each pin a request thread for the full 5s timeout.
CIMD_MAX_INFLIGHT = 4


class CIMDFetchError(RuntimeError):
    """A deliberately detail-free client metadata discovery failure."""

    def __init__(self) -> None:
        super().__init__("client metadata is unavailable")


@dataclass(frozen=True)
class CIMDClientMetadata:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    scope: str
    grant_types: tuple[str, ...]
    response_types: tuple[str, ...]
    application_type: str


@dataclass(frozen=True)
class _CacheEntry:
    # None == a cached FAILURE ("known-bad"): fail fast without a real
    # fetch until expires_at (MCP-08).
    metadata: CIMDClientMetadata | None
    expires_at: float


_CIMD_CACHE: OrderedDict[str, _CacheEntry] = OrderedDict()
_CIMD_CACHE_LOCK = threading.RLock()
_CIMD_DNS_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="dtxwiki-cimd-dns",
)
_CIMD_INFLIGHT = threading.Semaphore(CIMD_MAX_INFLIGHT)


@dataclass
class _FetchLockState:
    lock: threading.Lock
    users: int = 0


_CIMD_FETCH_LOCKS: dict[str, _FetchLockState] = {}


def clear_cimd_cache() -> None:
    with _CIMD_CACHE_LOCK:
        _CIMD_CACHE.clear()
        _CIMD_FETCH_LOCKS.clear()


def fetch_client_metadata_document(client_id: str) -> CIMDClientMetadata:
    _validate_client_identifier_url(client_id)
    started_at = time.monotonic()
    cached = _cached_metadata(client_id)
    if cached is not None:
        return cached
    with _CIMD_CACHE_LOCK:
        lock_state = _CIMD_FETCH_LOCKS.setdefault(
            client_id,
            _FetchLockState(lock=threading.Lock()),
        )
        lock_state.users += 1
    acquired = False
    try:
        remaining = CIMD_TOTAL_TIMEOUT_SECONDS - (time.monotonic() - started_at)
        if remaining <= 0 or not lock_state.lock.acquire(timeout=remaining):
            raise CIMDFetchError()
        acquired = True
        cached = _cached_metadata(client_id)
        if cached is not None:
            return cached
        if not _CIMD_INFLIGHT.acquire(blocking=False):
            # MCP-08: a slow/misbehaving client_id must not be free to spin
            # up unbounded concurrent DNS+TLS+HTTP fetches -- fail fast
            # instead of queuing behind CIMD_MAX_INFLIGHT already in flight.
            raise CIMDFetchError()
        try:
            metadata = _fetch_uncached(client_id, started_at=started_at)
        except Exception:
            # MCP-08: negative-cache the failure so a repeated bad/slow
            # client_id doesn't re-pay DNS+TLS+HTTP on every call within the
            # failure TTL -- only successes were cached before this fix.
            with _CIMD_CACHE_LOCK:
                _CIMD_CACHE[client_id] = _CacheEntry(
                    metadata=None,
                    expires_at=time.monotonic() + CIMD_FAILURE_TTL_SECONDS,
                )
                _CIMD_CACHE.move_to_end(client_id)
                while len(_CIMD_CACHE) > CIMD_CACHE_MAX_ENTRIES:
                    _CIMD_CACHE.popitem(last=False)
            raise
        finally:
            _CIMD_INFLIGHT.release()
        with _CIMD_CACHE_LOCK:
            _CIMD_CACHE[client_id] = _CacheEntry(
                metadata=metadata,
                expires_at=time.monotonic() + CIMD_CACHE_TTL_SECONDS,
            )
            _CIMD_CACHE.move_to_end(client_id)
            while len(_CIMD_CACHE) > CIMD_CACHE_MAX_ENTRIES:
                _CIMD_CACHE.popitem(last=False)
        return metadata
    finally:
        if acquired:
            lock_state.lock.release()
        with _CIMD_CACHE_LOCK:
            lock_state.users -= 1
            if lock_state.users == 0 and _CIMD_FETCH_LOCKS.get(client_id) is lock_state:
                del _CIMD_FETCH_LOCKS[client_id]


def _cached_metadata(client_id: str) -> CIMDClientMetadata | None:
    now = time.monotonic()
    with _CIMD_CACHE_LOCK:
        cached = _CIMD_CACHE.get(client_id)
        if cached is not None and cached.expires_at > now:
            _CIMD_CACHE.move_to_end(client_id)
            if cached.metadata is None:
                # MCP-08: known-bad within the failure TTL -- fail fast
                # without attempting another real fetch.
                raise CIMDFetchError()
            return cached.metadata
        if cached is not None:
            del _CIMD_CACHE[client_id]
    return None


def _fetch_uncached(
    client_id: str,
    *,
    started_at: float | None = None,
) -> CIMDClientMetadata:
    _validate_client_identifier_url(client_id)
    current_url = client_id
    started_at = time.monotonic() if started_at is None else started_at
    deadline = started_at + CIMD_TOTAL_TIMEOUT_SECONDS

    for redirect_count in range(CIMD_MAX_REDIRECTS + 1):
        if time.monotonic() - started_at > CIMD_TOTAL_TIMEOUT_SECONDS:
            raise CIMDFetchError()
        host, addresses = _resolve_public_destination(
            current_url,
            deadline=deadline,
        )
        response, body = _request_once(
            current_url,
            expected_host=host,
            pinned_addresses=addresses,
            started_at=started_at,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            if redirect_count >= CIMD_MAX_REDIRECTS:
                raise CIMDFetchError()
            location = response.headers.get("location")
            if not location:
                raise CIMDFetchError()
            current_url = urljoin(current_url, location)
            _validate_client_identifier_url(current_url)
            continue
        if response.status_code != 200:
            raise CIMDFetchError()
        if not _is_json_content_type(response.headers.get("content-type", "")):
            raise CIMDFetchError()
        return _parse_metadata(client_id, body)

    raise CIMDFetchError()


def _validate_client_identifier_url(url: str) -> None:
    try:
        validate_identifier(url)
    except ValueError:
        raise CIMDFetchError() from None
    if any(character.isspace() for character in url):
        raise CIMDFetchError()
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        raise CIMDFetchError() from None
    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
        raise CIMDFetchError()
    if parsed.username or parsed.password or parsed.fragment or "\\" in url:
        raise CIMDFetchError()
    if parsed.hostname.endswith(".") or "%" in parsed.netloc:
        raise CIMDFetchError()
    if port == 0:
        raise CIMDFetchError()
    if not parsed.path:
        raise CIMDFetchError()
    if any(unquote(segment) in {".", ".."} for segment in parsed.path.split("/")):
        raise CIMDFetchError()


def _resolve_public_destination(
    url: str,
    *,
    deadline: float | None = None,
) -> tuple[str, tuple[str, ...]]:
    parsed = urlparse(url)
    host = str(parsed.hostname or "")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise CIMDFetchError() from None

    effective_deadline = (
        time.monotonic() + CIMD_TOTAL_TIMEOUT_SECONDS if deadline is None else deadline
    )
    remaining = effective_deadline - time.monotonic()
    if remaining <= 0:
        raise CIMDFetchError()
    future = _CIMD_DNS_EXECUTOR.submit(
        socket.getaddrinfo,
        ascii_host,
        parsed.port or 443,
        type=socket.SOCK_STREAM,
    )
    try:
        records = future.result(timeout=remaining)
    except (FutureTimeoutError, OSError, ValueError):
        future.cancel()
        raise CIMDFetchError() from None

    addresses: list[str] = []
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0])
        except ValueError:
            raise CIMDFetchError() from None
        if address_is_forbidden(address):
            raise CIMDFetchError()
        rendered = str(address)
        if rendered not in addresses:
            addresses.append(rendered)
    if not addresses:
        raise CIMDFetchError()
    return ascii_host, tuple(addresses)


class _PinnedNetworkBackend(PinnedNetworkBackend):
    """Keep CIMD failures at the service boundary."""

    def __init__(self, **kwargs) -> None:
        super().__init__(error_factory=CIMDFetchError, **kwargs)


class _PinnedHTTPTransport(httpx.HTTPTransport):
    def __init__(
        self,
        *,
        expected_host: str,
        addresses: tuple[str, ...],
        deadline: float,
    ) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            max_connections=1,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_PinnedNetworkBackend(
                expected_host=expected_host,
                addresses=addresses,
                deadline=deadline,
            ),
        )


def _request_once(
    url: str,
    *,
    expected_host: str,
    pinned_addresses: tuple[str, ...],
    started_at: float,
) -> tuple[httpx.Response, bytes]:
    if time.monotonic() - started_at > CIMD_TOTAL_TIMEOUT_SECONDS:
        raise CIMDFetchError()
    timeout = httpx.Timeout(
        CIMD_READ_TIMEOUT_SECONDS,
        connect=CIMD_CONNECT_TIMEOUT_SECONDS,
        read=CIMD_READ_TIMEOUT_SECONDS,
        write=CIMD_CONNECT_TIMEOUT_SECONDS,
        pool=CIMD_CONNECT_TIMEOUT_SECONDS,
    )
    transport = _PinnedHTTPTransport(
        expected_host=expected_host,
        addresses=pinned_addresses,
        deadline=started_at + CIMD_TOTAL_TIMEOUT_SECONDS,
    )
    try:
        with httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                "GET",
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "dtxWiki-CIMD/1",
                },
            ) as response:
                if response.status_code == 200 and not _is_json_content_type(
                    response.headers.get("content-type", "")
                ):
                    raise CIMDFetchError()
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        if int(content_length) > CIMD_MAX_BYTES:
                            raise CIMDFetchError()
                    except ValueError:
                        raise CIMDFetchError() from None
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() - started_at > CIMD_TOTAL_TIMEOUT_SECONDS:
                        raise CIMDFetchError()
                    body.extend(chunk)
                    if len(body) > CIMD_MAX_BYTES:
                        raise CIMDFetchError()
                return response, bytes(body)
    except CIMDFetchError:
        raise
    except Exception:
        raise CIMDFetchError() from None


def _is_json_content_type(value: str) -> bool:
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


def _parse_metadata(client_id: str, body: bytes) -> CIMDClientMetadata:
    if not isinstance(body, bytes) or len(body) > CIMD_MAX_BYTES:
        raise CIMDFetchError()
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CIMDFetchError() from None
    if not isinstance(payload, dict) or payload.get("client_id") != client_id:
        raise CIMDFetchError()
    client_name = payload.get("client_name", "Client ID Metadata client")
    redirect_uris = payload.get("redirect_uris")
    # An omitted scope names no restriction; registration decides what the
    # client may request (``registration_scope``).
    scope = payload.get("scope", "")
    try:
        validate_identifier(client_id)
        validate_supported_metadata(payload)
        validate_redirect_list(redirect_uris)
        validate_client_name(client_name)
        validate_scope_input(scope)
    except ValueError:
        raise CIMDFetchError() from None
    # This server authenticates no client at the token endpoint, so the client
    # must offer ``none``.
    if "none" not in _offered_token_endpoint_auth_methods(payload):
        raise CIMDFetchError()
    if "client_secret" in payload or "client_secret_expires_at" in payload:
        raise CIMDFetchError()

    return CIMDClientMetadata(
        client_id=client_id,
        client_name=client_name,
        redirect_uris=tuple(redirect_uris),
        scope=scope,
        grant_types=supported_values(
            payload.get("grant_types", list(SUPPORTED_GRANT_TYPES)),
            SUPPORTED_GRANT_TYPES,
        ),
        response_types=supported_values(
            payload.get("response_types", list(SUPPORTED_RESPONSE_TYPES)),
            SUPPORTED_RESPONSE_TYPES,
        ),
        # Like DCR, register only a type this server knows.
        application_type=(
            "web" if payload.get("application_type") == "web" else "native"
        ),
    )


def _offered_token_endpoint_auth_methods(payload: dict) -> tuple[str, ...]:
    """The token endpoint authentication methods a document says it can use.

    ``token_endpoint_auth_methods_supported`` lists every method the client
    can use, and a singular ``token_endpoint_auth_method`` must be one of
    them, naming the client's preference. With neither, a client metadata
    document defaults to ``none``. A published document cannot keep a shared
    secret, so no ``client_secret_*`` method may appear (Client ID Metadata
    Document §4.1, §5).
    """

    preferred = payload.get("token_endpoint_auth_method", "none")
    offered = payload.get("token_endpoint_auth_methods_supported", [preferred])
    if (
        not isinstance(offered, list)
        or not offered
        or not all(isinstance(method, str) for method in offered)
        or preferred not in offered
        or any(method.startswith("client_secret_") for method in offered)
    ):
        raise CIMDFetchError()
    return tuple(offered)
