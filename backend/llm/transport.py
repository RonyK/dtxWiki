"""Default HTTP transport for LLM adapters."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping

import httpx

from backend.spi.llm import LLMAdapterError, TransportResponse

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0

# Failures that prove no request byte reached the provider. Anything else
# (including proxy and protocol errors) may have been received.
_NOT_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
)


class HttpxLLMTransport:
    """One POST per call: no retries and no redirects.

    httpx keeps reading the proxy and CA environment (``HTTPS_PROXY``,
    ``SSL_CERT_FILE``), as LLM calls always have.
    """

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._connect_timeout_seconds = connect_timeout_seconds
        self._transport = transport

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> TransportResponse:
        timeout = httpx.Timeout(
            timeout_seconds,
            connect=min(self._connect_timeout_seconds, timeout_seconds),
        )
        client = httpx.AsyncClient(
            timeout=timeout,
            transport=self._transport,
            follow_redirects=False,
        )
        try:
            request = client.build_request(
                "POST", url, headers=dict(headers), json=dict(payload)
            )
            response = await client.send(request, stream=True)
            try:
                body = await response.aread()
            except httpx.DecodingError:
                # The status and headers still describe the reply when its
                # body is unreadable.
                body = b""
            finally:
                with contextlib.suppress(httpx.HTTPError):
                    await response.aclose()
        except httpx.DecodingError:
            # An injected transport can fail before handing over a response.
            raise LLMAdapterError("invalid_response", delivery="sent") from None
        except httpx.RequestError as exc:
            raise LLMAdapterError(
                "timeout"
                if isinstance(exc, httpx.TimeoutException)
                else "network_error",
                delivery="not_sent" if isinstance(exc, _NOT_SENT) else "unknown",
            ) from None
        finally:
            # The reply is already read. A teardown failure must not turn it
            # into an unknown outcome that a caller would send again.
            with contextlib.suppress(httpx.HTTPError):
                await client.aclose()
        return TransportResponse(
            status_code=response.status_code,
            headers=response.headers,
            body=body,
        )
