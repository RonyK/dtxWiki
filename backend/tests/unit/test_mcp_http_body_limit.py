import json

import anyio
from starlette.requests import Request

from backend.api.mcp_http import (
    MCP_MAX_ENCODED_IMAGE_BYTES,
    MCP_MAX_REQUEST_BODY_BYTES,
    MCP_REQUEST_JSON_OVERHEAD_BYTES,
    _read_bounded_mcp_body,
)
from backend.context_media.domain.policy import MAX_CONTEXT_IMAGE_UPLOAD_BYTES


def _request(*, headers=(), messages=None):
    queue = list(messages or [])

    async def receive():
        if not queue:
            raise AssertionError("bounded reader requested another body chunk")
        return queue.pop(0)

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": list(headers),
        },
        receive,
    )


def test_mcp_body_limit_is_derived_from_the_canonical_decoded_image_limit():
    assert MCP_MAX_ENCODED_IMAGE_BYTES == 4 * (
        (MAX_CONTEXT_IMAGE_UPLOAD_BYTES + 2) // 3
    )
    assert MCP_MAX_REQUEST_BODY_BYTES == (
        MCP_MAX_ENCODED_IMAGE_BYTES + MCP_REQUEST_JSON_OVERHEAD_BYTES
    )


def test_mcp_body_limit_rejects_oversized_content_length_without_reading():
    request = _request(
        headers=[
            (b"content-length", str(MCP_MAX_REQUEST_BODY_BYTES + 1).encode())
        ]
    )

    response = anyio.run(_read_bounded_mcp_body, request)

    assert response.status_code == 413
    payload = json.loads(response.body)
    assert payload["error"]["data"] == {
        "error": "request_too_large",
        "limit_bytes": MCP_MAX_REQUEST_BODY_BYTES,
    }
    assert "content_base64" not in response.body.decode()


def test_mcp_body_limit_stops_chunked_body_at_the_bound():
    request = _request(
        messages=[
            {
                "type": "http.request",
                "body": bytes(MCP_MAX_REQUEST_BODY_BYTES),
                "more_body": True,
            },
            {"type": "http.request", "body": b"x", "more_body": False},
        ]
    )

    response = anyio.run(_read_bounded_mcp_body, request)

    assert response.status_code == 413
