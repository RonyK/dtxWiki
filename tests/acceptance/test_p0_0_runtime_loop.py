"""P0-0 runtime loop: authenticated MCP and Browser REST stay one system.

The MCP side deliberately enters through the supported PAT-authenticated
``/mcp`` endpoint. The tests prove that its Context state is shared with REST
and that a stale browser write cannot overwrite a newer MCP edit.
"""

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.tests.conftest import authenticate_test_client_in_workspace


def _clients_and_pat(page_service):
    app = create_app(
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="test-session-secret",
            public_base_url="http://testserver",
            mcp_http_mode="json_stateless",
        ),
        page_service=page_service,
    )
    author_client = TestClient(app)
    workspace_id = authenticate_test_client_in_workspace(
        author_client,
        page_service.session_factory,
        workspace_slug="default",
    )
    issued = author_client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": workspace_id}],
            "name": "P0-0 runtime loop",
            "scopes": ["mcp:read", "mcp:write"],
        },
    )
    assert issued.status_code == 201, issued.text
    plain_token = issued.json()["data"]["plain_token"]
    assert plain_token.startswith("dtxw_")

    browser_client = TestClient(app)
    authenticate_test_client_in_workspace(
        browser_client,
        page_service.session_factory,
        email="browser-reader@example.com",
        workspace_slug="default",
        role="editor",
    )
    return (
        author_client,
        browser_client,
        {"Authorization": f"Bearer {plain_token}"},
        workspace_id,
    )


def _mcp_call(
    client: TestClient,
    headers: dict[str, str],
    request_id: int,
    name: str,
    arguments: dict,
) -> dict:
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "error" not in payload, payload
    return payload["result"]["structuredContent"]


def _create_page(
    client: TestClient,
    headers: dict[str, str],
    workspace_id: str,
    *,
    title: str,
    body_md: str,
) -> dict:
    created = _mcp_call(
        client,
        headers,
        1,
        "create_context_page",
        {
            "workspace_id": workspace_id,
            "title": title,
            "page_type": "overview",
            "body_md": body_md,
            "conversation_token": "new",
        },
    )
    assert created["envelope"]["result_status"] == "succeeded"
    return created


def test_mcp_write_rest_read_mcp_read_loop(page_service):
    mcp_client, browser_client, headers, workspace_id = _clients_and_pat(page_service)

    page = _create_page(
        mcp_client,
        headers,
        workspace_id,
        title="Test Page",
        body_md="# Hello PoC",
    )

    submitted = _mcp_call(
        mcp_client,
        headers,
        2,
        "submit_context_edit_for_review",
        {
            "workspace_id": workspace_id,
            "page_id": page["data"]["id"],
            "version_id": page["data"]["version_id"],
            "expected_editing_revision": page["data"]["editing_revision"],
            "reason": "runtime loop acceptance test",
            "idempotency_key": "p0-0-submit",
            "conversation_token": page["envelope"]["conversation_token"],
        },
    )
    assert submitted["envelope"]["result_status"] == "succeeded", submitted["envelope"]

    rest_read = browser_client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{page['data']['id']}",
        params={"version_id": page["data"]["version_id"]},
    )
    mcp_read = _mcp_call(
        mcp_client,
        headers,
        3,
        "read",
        {
            "workspace_id": workspace_id,
            "target_type": "context_page",
            "target_id": page["data"]["id"],
            "conversation_token": "new",
        },
    )

    assert rest_read.status_code == 200, rest_read.text
    rest_body = rest_read.json()["data"]["body_md"]
    mcp_body = mcp_read["data"]["body_markdown"]

    # "One system" is the whole point of this test, so the primary assertion
    # is that both transports hand back the SAME bytes for the same version.
    assert rest_body == mcp_body

    # That equality alone could hold while both sides were equally wrong, so
    # it is anchored to what was actually authored. Note the stored body
    # carries YAML frontmatter: create_context_page composes it, unlike the
    # deregistered write_draft this test used to call, which stored the raw
    # markdown -- which is why the old `== "# Hello PoC"` assertion is not
    # simply restored here.
    assert mcp_body.startswith("---\n"), mcp_body
    assert 'title: "Test Page"' in mcp_body, mcp_body
    assert mcp_body.endswith("# Hello PoC"), mcp_body


def test_rest_stale_patch_after_mcp_update_returns_conflict(page_service):
    mcp_client, browser_client, headers, workspace_id = _clients_and_pat(page_service)

    v1 = _create_page(
        mcp_client,
        headers,
        workspace_id,
        title="Test Page",
        body_md="# v1",
    )
    updated = _mcp_call(
        mcp_client,
        headers,
        2,
        "update_context_edit",
        {
            "workspace_id": workspace_id,
            "page_id": v1["data"]["id"],
            "base_version_id": v1["data"]["version_id"],
            "base_content_hash": v1["data"]["content_hash"],
            "body_md": "# v2",
            "expected_editing_revision": v1["data"]["editing_revision"],
            "idempotency_key": "p0-0-update-v2",
            "conversation_token": v1["envelope"]["conversation_token"],
        },
    )
    assert updated["envelope"]["result_status"] == "succeeded", updated["envelope"]
    # update_context_edit is an autosave: it rewrites the draft row IN PLACE,
    # so the version id is deliberately unchanged and the content hash is
    # what moves. That is precisely why the REST write below is stale -- it
    # still carries v1's hash -- and why naming a version id was never what
    # made this test's conflict fire.
    assert updated["data"]["version_id"] == v1["data"]["version_id"]
    assert updated["data"]["content_hash"] != v1["data"]["content_hash"]

    # Deliberately still pointing at v1: the page moved on through MCP, so
    # this REST write is stale and must lose rather than silently clobber.
    stale = browser_client.patch(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{v1['data']['id']}",
        json={
            "title": "Stale",
            "body_md": "# stale",
            "base_version_id": v1["data"]["version_id"],
            "base_content_hash": v1["data"]["content_hash"],
            "on_stale": "conflict",
        },
    )

    assert stale.status_code == 409, stale.text
    body = stale.json()
    assert body["error_code"] == "stale_base_version", body

    # The pre-P2.2 version of this test asserted the 409 NAMED the winning
    # version (`server_version_id == v2`). That assertion is now wrong by
    # design and is deliberately not restored: v2 is another author's private
    # "editing" row (one user wrote it through PAT-authenticated MCP; a
    # different user is the REST caller), and PageService's disclosure policy
    # returns
    # (None, None) in exactly that case, because naming the row would be the
    # disclosure P2.2/P2.6 exist to prevent. So the modern invariant is
    # stronger than the old one: the stale write loses AND learns nothing.
    assert "server_version_id" not in body, body
    assert "server_content_hash" not in body, body

    # Losing must mean losing: the MCP author's v2 is still what is stored.
    after = _mcp_call(
        mcp_client,
        headers,
        3,
        "read",
        {
            "workspace_id": workspace_id,
            "target_type": "context_page",
            "target_id": v1["data"]["id"],
            "conversation_token": "new",
        },
    )
    assert after["data"]["body_markdown"].endswith("# v2"), after["data"][
        "body_markdown"
    ]
