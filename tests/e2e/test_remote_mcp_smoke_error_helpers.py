from __future__ import annotations

import anyio
import httpx

from tests.e2e import remote_mcp_smoke as smoke
from tests.e2e.remote_mcp_smoke import _exception_details


def test_exception_details_extracts_http_status_from_sdk_exception_group():
    request = httpx.Request("POST", "https://dtxwiki.test:8443/mcp")
    response = httpx.Response(
        401,
        text='{"error_code":"unauthorized"}',
        request=request,
    )
    error = httpx.HTTPStatusError(
        "Client error '401 Unauthorized'",
        request=request,
        response=response,
    )
    grouped = ExceptionGroup("unhandled errors in a TaskGroup", [error])

    details = _exception_details(grouped)

    assert "401" in details
    assert "unauthorized" in details


def test_exception_details_extracts_response_body_from_sdk_exception_group():
    request = httpx.Request("POST", "https://dtxwiki.test:8443/mcp")
    response = httpx.Response(
        403,
        text='{"error_code":"restricted_library_denied"}',
        request=request,
    )
    error = httpx.HTTPStatusError(
        "Client error '403 Forbidden'",
        request=request,
        response=response,
    )
    grouped = ExceptionGroup("unhandled errors in a TaskGroup", [error])

    details = _exception_details(grouped)

    assert "403" in details
    assert "restricted_library_denied" in details


def test_exception_details_handles_unread_streaming_response():
    request = httpx.Request("POST", "https://dtxwiki.test:8443/mcp")
    response = httpx.Response(
        401,
        stream=httpx.ByteStream(b'{"error_code":"unauthorized"}'),
        request=request,
    )
    error = httpx.HTTPStatusError(
        "Client error '401 Unauthorized'",
        request=request,
        response=response,
    )
    grouped = ExceptionGroup("unhandled errors in a TaskGroup", [error])

    details = _exception_details(grouped)

    assert "401" in details
    assert "Unauthorized" in details
    assert "ResponseNotRead" in details


def test_error_response_hook_reads_body_for_exception_details():
    async def run() -> str:
        request = httpx.Request("POST", "https://dtxwiki.test:8443/mcp")
        response = httpx.Response(
            403,
            stream=httpx.ByteStream(b'{"error_code":"restricted_library_denied"}'),
            request=request,
        )
        await smoke._read_error_response_body(response)
        error = httpx.HTTPStatusError(
            "Client error '403 Forbidden'",
            request=request,
            response=response,
        )
        return _exception_details(error)

    details = anyio.run(run)

    assert "403" in details
    assert "restricted_library_denied" in details
