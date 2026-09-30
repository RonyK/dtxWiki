import pytest

from backend.tests.integration._cross_transport_helpers import (
    assert_restricted_library_items_excluded,
    prepare_cross_transport_scenario,
)


@pytest.mark.parametrize(
    "transport", ["session", "oauth_jwt", "pat", "mcp_http", "mcp_http_pat"]
)
def test_owner_default_viewer_target_restricted_search_matches_across_transports(
    page_service,
    transport,
):
    """Fix-6 Task 7.1 (M-F5-CODEX-1): adds `mcp_http_pat` so PAT
    + `/mcp tools/call` is covered by the same scenario as OAuth-bearing
    `/mcp`. Pre-fix, the matrix only exercised PAT via REST and `/mcp`
    via OAuth — a regression isolated to PAT-on-MCP could pass.
    """
    client, scenario = prepare_cross_transport_scenario(page_service)
    workspace_id = scenario.target_workspace_id

    if transport == "session":
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/search",
            json={"query": "needle cross transport", "scope": "all"},
        )
        payload = response.json()["data"]
    elif transport == "oauth_jwt":
        client.cookies.clear()
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/search",
            headers={"Authorization": f"Bearer {scenario.oauth_access_token}"},
            json={"query": "needle cross transport", "scope": "all"},
        )
        payload = response.json()["data"]
    elif transport == "pat":
        client.cookies.clear()
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/search",
            headers={"Authorization": f"Bearer {scenario.personal_api_token}"},
            json={"query": "needle cross transport", "scope": "all"},
        )
        payload = response.json()["data"]
    elif transport == "mcp_http":
        client.cookies.clear()
        response = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {scenario.oauth_access_token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search",
                    "arguments": {
                        "workspace_id": str(workspace_id),
                        "query": "needle cross transport",
                        "scope": "all",
                    },
                },
            },
        )
        payload = response.json()["result"]["structuredContent"]["data"]
    elif transport == "mcp_http_pat":
        client.cookies.clear()
        response = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {scenario.personal_api_token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search",
                    "arguments": {
                        "workspace_id": str(workspace_id),
                        "query": "needle cross transport",
                        "scope": "all",
                    },
                },
            },
        )
        payload = response.json()["result"]["structuredContent"]["data"]
    else:  # pragma: no cover - param list is exhaustive.
        raise AssertionError(f"unknown transport: {transport}")

    assert response.status_code == 200, response.text
    assert_restricted_library_items_excluded(payload)
