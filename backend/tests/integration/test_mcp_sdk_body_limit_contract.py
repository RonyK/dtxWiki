"""The MCP SDK must not reject a request the app's own /mcp limit admits.

backend/api/mcp_http.py bounds an MCP request at MCP_MAX_REQUEST_BODY_BYTES,
sized so `upload_image` can carry a base64-encoded image up to the 10 MiB
Context media limit. It then hands the buffered body to the SDK's
StreamableHTTPSessionManager. From mcp 1.29.0 that manager enforces its own
request-body limit, 4 MiB by default, and answers anything larger with a plain
`413 Request body too large` before the app sees it -- so with the default an
upload of a roughly 3 MiB image or more stops working while every unit test of
the app's own limit stays green.

This module sends one request between the two limits through both transport
modes. It fails if the locked SDK is moved to a release with the default limit
without the app passing ``max_request_body_size`` to its session managers.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.api.mcp_http import MCP_MAX_REQUEST_BODY_BYTES
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.tests.integration._cross_transport_helpers import (
    issue_personal_api_token,
    make_client,
)

pytestmark = pytest.mark.mcp_http

# The SDK's default limit since mcp 1.29.0 (DEFAULT_MAX_REQUEST_BODY_SIZE).
SDK_DEFAULT_REQUEST_BODY_LIMIT = 4 * 1024 * 1024
PADDING = 5 * 1024 * 1024


def _initialize_request() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "body-limit-contract", "version": "1"},
            "_meta": {"padding": "x" * PADDING},
        },
    }


def _post(client: TestClient, token: str):
    body = json.dumps(_initialize_request()).encode()
    # Anti-vacuity: the request must be one an SDK left on its default limit
    # rejects, and one the app's own limit admits.
    assert SDK_DEFAULT_REQUEST_BODY_LIMIT < len(body) < MCP_MAX_REQUEST_BODY_BYTES
    return client.post(
        "/mcp",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )


def test_json_stateless_transport_admits_a_request_inside_the_app_limit(
    page_service,
):
    client = make_client(page_service)
    token = issue_personal_api_token(client)

    response = _post(client, token)

    assert response.status_code == 200, response.text[:200]
    assert response.json()["result"]["serverInfo"]


def test_streamable_transport_admits_a_request_inside_the_app_limit(page_service):
    token = issue_personal_api_token(make_client(page_service))
    app = create_app(
        settings=Settings(
            mcp_http_mode="streamable",
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="test-session-secret",
            public_base_url="http://testserver",
        ),
        page_service=page_service,
    )

    with TestClient(app) as client:
        response = _post(client, token)

    assert response.status_code == 200, response.text[:200]
    assert '"serverInfo"' in response.text
