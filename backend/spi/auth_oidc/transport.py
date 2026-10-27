"""Bounded HTTPS JSON transport for administrator-configured identity providers."""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from urllib.parse import urlsplit

import httpcore
import httpx

from backend.spi.auth import AuthMethodError
from backend.utils.http_security import (
    CLOUD_METADATA_ADDRESSES,
    PinnedNetworkBackend,
    PinnedNetworkError,
    address_is_forbidden,
)

_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dtxwiki-oidc-dns")
_DNS_SLOTS = threading.BoundedSemaphore(8)


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, stream, deadline):
        self.stream = stream
        self.deadline = deadline

    def _remaining(self, timeout):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AuthMethodError("identity_provider_timeout")
        return min(timeout, remaining) if timeout is not None else remaining

    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout=self._remaining(timeout))

    def write(self, buffer, timeout=None):
        return self.stream.write(buffer, timeout=self._remaining(timeout))

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _DeadlineStream(
            self.stream.start_tls(
                ssl_context,
                server_hostname=server_hostname,
                timeout=self._remaining(timeout),
            ),
            self.deadline,
        )

    def close(self):
        self.stream.close()

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)


class _DeadlineBackend(PinnedNetworkBackend):
    def connect_tcp(self, *args, **kwargs):
        return _DeadlineStream(super().connect_tcp(*args, **kwargs), self._deadline)


class _PinnedHTTPTransport(httpx.HTTPTransport):
    def __init__(self, *, expected_host, addresses, deadline):
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
            network_backend=_DeadlineBackend(
                expected_host=expected_host, addresses=addresses, deadline=deadline
            ),
        )


def https_url(value: object, *, issuer: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise AuthMethodError("invalid_identity_provider_url")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (issuer and parsed.query)
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or "\\" in value
            or "%" in parsed.netloc
            or parsed.port == 0
        ):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise AuthMethodError("invalid_identity_provider_url") from None
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


class JsonTransport:
    """No redirects, proxy environment, unbounded bodies, or provider error text.

    Private HTTPS hosts require explicit ``allow_private_network`` configuration.
    DNS answers are checked and pinned to prevent a second, unvalidated lookup.
    ``transport`` is an injection point for a trusted adapter or test fixture.
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        max_bytes: int = 1024 * 1024,
        timeout_seconds: float = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_bytes < 1 or timeout_seconds <= 0:
            raise ValueError("positive transport limits required")
        self.transport = transport
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.clock = clock

    def _pinned_transport(
        self, url: str, *, allow_private_network: bool, deadline: float
    ):
        parsed = urlsplit(url)
        host = str(parsed.hostname).encode("idna").decode("ascii").lower()
        if not _DNS_SLOTS.acquire(blocking=False):
            raise AuthMethodError("identity_provider_busy")
        future = _DNS_EXECUTOR.submit(
            socket.getaddrinfo, host, parsed.port or 443, type=socket.SOCK_STREAM
        )
        future.add_done_callback(lambda _: _DNS_SLOTS.release())
        try:
            records = future.result(timeout=max(0, deadline - time.monotonic()))
        except (FutureTimeoutError, OSError, ValueError):
            future.cancel()
            raise AuthMethodError("identity_provider_resolution_failed") from None
        addresses: list[str] = []
        for record in records:
            address = ipaddress.ip_address(record[4][0])
            mapped = getattr(address, "ipv4_mapped", None)
            checked = mapped if mapped is not None else address
            if address_is_forbidden(address) and not (
                allow_private_network
                and checked.is_private
                and checked not in CLOUD_METADATA_ADDRESSES
                and not (
                    checked.is_link_local
                    or checked.is_multicast
                    or checked.is_unspecified
                    or checked.is_reserved
                )
            ):
                raise AuthMethodError("identity_provider_address_forbidden")
            if str(address) not in addresses:
                addresses.append(str(address))
        if not addresses:
            raise AuthMethodError("identity_provider_resolution_failed")
        return _PinnedHTTPTransport(
            expected_host=host, addresses=tuple(addresses), deadline=deadline
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        allow_private_network: bool = False,
    ) -> dict[str, object]:
        https_url(url)
        started = self.clock()
        request_headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            **(headers or {}),
        }
        try:
            with httpx.Client(
                transport=self.transport
                or self._pinned_transport(
                    url,
                    allow_private_network=allow_private_network,
                    deadline=time.monotonic() + self.timeout_seconds,
                ),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(min(self.timeout_seconds, 3.0)),
            ) as client:
                with client.stream(
                    method, url, data=data, headers=request_headers
                ) as response:
                    if response.status_code != 200:
                        raise AuthMethodError("identity_provider_request_failed")
                    media_type = response.headers.get("content-type", "").split(";")[0]
                    media_type = media_type.lower().strip()
                    if media_type != "application/json" and not (
                        media_type.startswith("application/")
                        and media_type.endswith("+json")
                    ):
                        raise AuthMethodError("invalid_identity_provider_response")
                    if (
                        response.headers.get("content-encoding", "identity").lower()
                        != "identity"
                    ):
                        raise AuthMethodError("compressed_identity_provider_response")
                    declared = response.headers.get("content-length")
                    if (
                        declared is not None
                        and not 0 <= int(declared) <= self.max_bytes
                    ):
                        raise AuthMethodError("identity_provider_response_too_large")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            raise AuthMethodError(
                                "identity_provider_response_too_large"
                            )
                        if self.clock() - started > self.timeout_seconds:
                            raise AuthMethodError("identity_provider_timeout")
                    if self.clock() - started > self.timeout_seconds:
                        raise AuthMethodError("identity_provider_timeout")
                    result = json.loads(body, object_pairs_hook=_unique_object)
                    if not isinstance(result, dict):
                        raise ValueError("JSON object required")
                    return result
        except AuthMethodError:
            raise
        except (
            httpx.HTTPError,
            PinnedNetworkError,
            ValueError,
            TypeError,
            RecursionError,
            OverflowError,
        ):
            raise AuthMethodError("invalid_identity_provider_response") from None
