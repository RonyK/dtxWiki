import asyncio
import random
import time
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from backend.llm.base import LLMProvider, LLMProviderError, LLMRequest, LLMResponse

RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504, 529})
DEFAULT_TIMEOUT = 120.0
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BASE_DELAY = 0.5
DEFAULT_RETRY_MAX_DELAY = 8.0


class HTTPLLMProvider(LLMProvider):
    provider_name = ""
    api_key_label = ""
    default_model = ""
    default_base_url = ""

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
        retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(f"{self.api_key_label} API key is required")
        self.api_key = api_key
        self.model = model or self.default_model
        self.base_url = base_url or self.default_base_url
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.transport = transport

    def chat(self, request: LLMRequest) -> LLMResponse:
        payload = self._payload(request)
        with httpx.Client(
            timeout=self._request_timeout(request),
            transport=self.transport,
        ) as client:
            response = self._send_with_retries(
                lambda: client.post(
                    self.base_url,
                    headers=self._headers(),
                    json=payload,
                )
            )
            return self._parse_response(response.json())

    async def async_chat(self, request: LLMRequest) -> LLMResponse:
        payload = self._payload(request)
        async with httpx.AsyncClient(
            timeout=self._request_timeout(request),
            transport=self.transport,
        ) as client:
            response = await self._async_send_with_retries(
                lambda: client.post(
                    self.base_url,
                    headers=self._headers(),
                    json=payload,
                )
            )
            return self._parse_response(response.json())

    @abstractmethod
    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def _parse_response(self, payload: dict[str, Any]) -> LLMResponse:
        raise NotImplementedError

    def _request_timeout(self, request: LLMRequest) -> httpx.Timeout:
        timeout = request.timeout if request.timeout is not None else self.timeout
        return httpx.Timeout(timeout, connect=min(self.connect_timeout, timeout))

    def _send_with_retries(
        self, send: Callable[[], httpx.Response]
    ) -> httpx.Response:
        attempt = 0
        while True:
            retry_after: str | None = None
            try:
                response = send()
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise self._transport_error(exc) from exc
            else:
                if not response.is_error:
                    return response
                if (
                    response.status_code not in RETRYABLE_STATUS_CODES
                    or attempt >= self.max_retries
                ):
                    raise self._status_error(response)
                retry_after = response.headers.get("retry-after")
            time.sleep(self._retry_delay(attempt, retry_after))
            attempt += 1

    async def _async_send_with_retries(
        self, send: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        attempt = 0
        while True:
            retry_after: str | None = None
            try:
                response = await send()
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise self._transport_error(exc) from exc
            else:
                if not response.is_error:
                    return response
                if (
                    response.status_code not in RETRYABLE_STATUS_CODES
                    or attempt >= self.max_retries
                ):
                    raise self._status_error(response)
                retry_after = response.headers.get("retry-after")
            await asyncio.sleep(self._retry_delay(attempt, retry_after))
            attempt += 1

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        delay = min(self.retry_max_delay, self.retry_base_delay * (2**attempt))
        delay *= 0.5 + random.random() / 2
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), self.retry_max_delay))
            except ValueError:
                pass
        return delay

    def _transport_error(self, exc: httpx.TransportError) -> LLMProviderError:
        return LLMProviderError(
            f"{self.provider_name} request failed: {exc}",
            provider=self.provider_name,
            retryable=True,
        )

    def _status_error(self, response: httpx.Response) -> LLMProviderError:
        error_type, error_message = self._parse_error_body(response)
        message = f"{self.provider_name} API returned {response.status_code}"
        if error_type:
            message += f" ({error_type})"
        detail = error_message or response.text[:200]
        if detail:
            message += f": {detail}"
        return LLMProviderError(
            message,
            provider=self.provider_name,
            status_code=response.status_code,
            error_type=error_type,
            retryable=response.status_code in RETRYABLE_STATUS_CODES,
        )

    @staticmethod
    def _parse_error_body(response: httpx.Response) -> tuple[str | None, str | None]:
        try:
            body = response.json()
        except ValueError:
            return None, None
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            return None, None
        error_type = error.get("type")
        error_message = error.get("message")
        return (
            str(error_type) if error_type else None,
            str(error_message) if error_message else None,
        )
