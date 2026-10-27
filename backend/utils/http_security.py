"""Shared destination validation and DNS-pinned HTTP connection primitives."""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable

import httpcore


class PinnedNetworkError(RuntimeError):
    """The requested connection violates its pinned host or deadline."""


CLOUD_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


def address_is_forbidden(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Reject every non-global or explicitly dangerous destination address."""

    mapped = getattr(address, "ipv4_mapped", None)
    return (
        address in CLOUD_METADATA_ADDRESSES
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or bool(getattr(address, "is_site_local", False))
        or not address.is_global
        or (mapped is not None and address_is_forbidden(mapped))
    )


class PinnedNetworkBackend(httpcore.SyncBackend):
    def __init__(
        self,
        *,
        expected_host: str,
        addresses: tuple[str, ...],
        deadline: float | None = None,
        error_factory: Callable[[], Exception] = PinnedNetworkError,
    ) -> None:
        self._error_factory = error_factory
        self._expected_host = expected_host
        self._addresses = addresses
        self._deadline = deadline
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        if host.lower() != self._expected_host:
            raise self._error_factory()
        last_error: Exception | None = None
        for address in self._addresses:
            connect_timeout = timeout
            if self._deadline is not None:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise self._error_factory()
                connect_timeout = min(timeout, remaining) if timeout else remaining
            try:
                return self._backend.connect_tcp(
                    address,
                    port,
                    timeout=connect_timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # httpcore maps the final error for httpx
                last_error = exc
        if last_error is not None:
            raise last_error
        raise self._error_factory()

    def connect_unix_socket(self, *args, **kwargs):
        raise self._error_factory()
