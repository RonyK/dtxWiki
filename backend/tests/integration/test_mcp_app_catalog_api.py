from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.tests.conftest import authenticate_test_client_in_workspace


def make_client(page_service, **settings_overrides):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="mcp-app-catalog-test-secret",
                **settings_overrides,
            ),
            page_service=page_service,
        )
    )
    workspace_id = authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        email="mcp-app-catalog@example.com",
        workspace_slug="default",
    )
    return client, workspace_id


def test_catalog_api_projects_options_and_secret_free_setup_plan(page_service):
    client, workspace_id = make_client(page_service)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-apps?locale=ko"
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["catalog_revision"] == "2026-09-13.1"
    assert [app["id"] for app in payload["apps"]] == [
        "claude-code",
        "codex",
        "gemini-cli",
        "antigravity",
    ]
    assert [app["featured_rank"] for app in payload["apps"]] == [1, 2, 3, 4]
    assert payload["apps"][0]["icon_url"].endswith(
        "?v=6d53db4be375e899c937c26cf16684a80d6e869b1928d72b37748bef2560e219"
    )
    assert payload["apps"][2]["icon_url"].endswith(
        "?v=d6c30349364931f944e2d0f3a92acc2d7e372356995f9be02b88ecc1305ea7f0"
    )
    assert payload["apps"][3]["icon_url"].endswith(
        "?v=98eb7e852bf45b60941957bd9b7fbdf00af5be05c2351727585cdc135b466c82"
    )
    assert payload["generic_profiles"][0]["icon_url"].endswith(
        "?generic=true&v=c0a2e2f99faf21ef8ae317f2d27fd071724a8ad7920939c4b9f9dad5e2c4a63c"
    )
    assert len([app for app in payload["apps"] if app["featured_rank"]]) <= 4
    assert payload["generic_profiles"][0]["label"] == "기타 MCP 클라이언트"
    oauth = next(
        option
        for option in payload["apps"][0]["options"]
        if option["auth"] == "oauth"
    )
    assert oauth == {
        **oauth,
        "available": False,
        "reason_codes": ["server_oauth_unavailable"],
    }

    plan_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-apps/codex/setup-plan",
        json={
            "variant_id": "codex-cli-access-key",
            "platform": "windows",
            "shell": "powershell",
            "locale": "ko",
        },
    )
    assert plan_response.status_code == 200, plan_response.text
    plan = plan_response.json()["data"]
    assert plan["owner_id"] == "codex"
    assert plan["auth"] == "access-key"
    assert plan["mcp_url"].endswith("/mcp")
    serialized = json_text = plan_response.text
    assert "plain_token" not in serialized
    assert "dtx_pat_" not in json_text
    command = next(
        artifact for artifact in plan["artifacts"] if artifact["kind"] == "command"
    )
    assert command["executableId"] == "codex"
    assert command["commandName"] == "codex"

    for app in payload["apps"]:
        icon = client.get(app["icon_url"])
        assert icon.status_code == 200
        assert icon.headers["content-type"].startswith("image/svg+xml")
        assert icon.headers["x-content-type-options"] == "nosniff"


def test_google_client_setup_plans_expose_canonical_secret_free_shapes(page_service):
    client, workspace_id = make_client(page_service)

    gemini_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-apps/gemini-cli/setup-plan",
        json={
            "variant_id": "gemini-cli-access-key",
            "platform": "windows",
            "shell": "powershell",
            "locale": "en",
        },
    )
    assert gemini_response.status_code == 200, gemini_response.text
    gemini = gemini_response.json()["data"]
    gemini_command = next(
        artifact for artifact in gemini["artifacts"] if artifact["kind"] == "command"
    )
    assert gemini_command["commandName"] == "gemini"
    assert gemini_command["env"] == {"DTXWIKI_MCP_PAT": [{"slot": "access-token-env"}]}
    assert gemini_command["argv"] == [
        [{"literal": "mcp"}],
        [{"literal": "add"}],
        [{"literal": "--scope"}],
        [{"literal": "user"}],
        [{"literal": "--transport"}],
        [{"literal": "http"}],
        [{"literal": "--header"}],
        [{"literal": "Authorization: Bearer $DTXWIKI_MCP_PAT"}],
        [{"slot": "server-name"}],
        [{"slot": "mcp-url"}],
    ]
    assert "dtx_pat_" not in gemini_response.text

    antigravity_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-apps/antigravity/setup-plan",
        json={
            "variant_id": "antigravity-cli-access-key",
            "platform": "windows",
            "shell": "powershell",
            "locale": "en",
        },
    )
    assert antigravity_response.status_code == 200, antigravity_response.text
    antigravity = antigravity_response.json()["data"]
    config = next(
        artifact
        for artifact in antigravity["artifacts"]
        if artifact["kind"] == "json-config"
    )
    assert config["value"] == {
        "serverUrl": {"slot": "mcp-url"},
        "headers": {
            "Authorization": {"slot": "authorization-bearer"},
        },
    }
    assert "dtx_pat_" not in antigravity_response.text


def test_antigravity_oauth_plan_projects_native_agy_command(page_service):
    client, workspace_id = make_client(
        page_service,
        public_base_url="https://wiki.example.com",
        # TestClient uses an HTTP origin; keep its authenticated cookie usable
        # while separately exercising HTTPS-derived OAuth availability.
        session_cookie_secure=False,
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-apps/antigravity/setup-plan",
        json={
            "variant_id": "antigravity-cli-oauth",
            "platform": "windows",
            "shell": "powershell",
            "locale": "en",
        },
    )

    assert response.status_code == 200, response.text
    plan = response.json()["data"]
    command = next(
        artifact for artifact in plan["artifacts"] if artifact["kind"] == "command"
    )
    assert command["executableId"] == "agy"
    assert command["commandName"] == "agy"
    assert command["argv"] == [
        [{"literal": "mcp"}],
        [{"literal": "add"}],
        [{"literal": "--type"}],
        [{"literal": "http"}],
        [{"slot": "server-name"}],
        [{"slot": "mcp-url"}],
    ]
    assert "access-token" not in response.text
    # Sign-in is its own artifact now, not a sentence under the recipe.
    sign_in = [
        artifact
        for artifact in plan["artifacts"]
        if artifact.get("step") == "authenticate"
    ]
    assert [artifact["kind"] for artifact in sign_in] == [
        "app-command",
        "instruction",
    ]
    assert sign_in[0]["value"] == [{"literal": "/mcp"}]


def test_setup_plan_rejects_unavailable_oauth(page_service):
    client, workspace_id = make_client(page_service)
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-apps/codex/setup-plan",
        json={"variant_id": "codex-cli-oauth"},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "mcp_variant_unavailable"
