from __future__ import annotations

import base64
from io import BytesIO
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from backend.app.bootstrap import bootstrap
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.context_media import ContextMediaAsset, ContextMediaUsage
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.tests.conftest import authenticate_test_client_in_workspace


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), color=color).save(output, format="PNG")
    return output.getvalue()


def _issue_pat(
    rest: TestClient,
    workspace_ids: tuple[UUID, ...],
    *,
    name: str,
) -> dict[str, str]:
    issued = rest.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [
                {"workspace_id": str(workspace_id)} for workspace_id in workspace_ids
            ],
            "name": name,
            "scopes": ["mcp:read", "mcp:write"],
        },
    )
    assert issued.status_code == 201, issued.text
    plain_token = issued.json()["data"]["plain_token"]
    assert plain_token.startswith("dtxw_")
    return {"Authorization": f"Bearer {plain_token}"}


def _mcp_call(
    rest: TestClient, headers: dict, request_id: int, name: str, arguments: dict
):
    return rest.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def _mcp_structured(
    rest: TestClient,
    headers: dict[str, str],
    request_id: int,
    name: str,
    arguments: dict,
) -> dict:
    response = _mcp_call(rest, headers, request_id, name, arguments)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "error" not in payload, payload
    return payload["result"]["structuredContent"]


def test_pat_http_create_update_submit_is_visible_through_rest(tmp_path):
    """The supported bearer-authenticated MCP transport shares REST state."""

    database_path = tmp_path / "p0_1_mcp_http.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        session_secret="test-session-secret",
        public_base_url="http://testserver",
        mcp_http_mode="json_stateless",
    )
    bootstrap(settings)
    app = create_app(settings=settings)
    rest = TestClient(app)
    factory = app.state.container.db.control_session_factory()
    with factory.begin() as session:
        seed_default_workspace(session)
    workspace_id = UUID(
        authenticate_test_client_in_workspace(
            rest,
            factory,
            workspace_slug="default",
        )
    )
    headers = _issue_pat(
        rest,
        (workspace_id,),
        name="P0.1 HTTP acceptance credential",
    )

    created = _mcp_structured(
        rest,
        headers,
        1,
        "create_context_page",
        {
            "workspace_id": str(workspace_id),
            "conversation_token": "new",
            "title": "P0-1 MCP HTTP Acceptance",
            "page_type": "overview",
            "body_md": "# Initial HTTP body",
        },
    )
    assert created["envelope"]["result_status"] == "succeeded"

    updated = _mcp_structured(
        rest,
        headers,
        2,
        "update_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": created["data"]["id"],
            "title": "P0-1 MCP HTTP Acceptance",
            "body_md": "# Updated through authenticated HTTP",
            "base_version_id": created["data"]["version_id"],
            "base_content_hash": created["data"]["content_hash"],
            "expected_editing_revision": created["data"]["editing_revision"],
            "idempotency_key": "p0-1-http-update",
            "conversation_token": created["envelope"]["conversation_token"],
        },
    )
    assert updated["envelope"]["result_status"] == "succeeded"
    assert updated["data"]["version_id"] == created["data"]["version_id"]

    submitted = _mcp_structured(
        rest,
        headers,
        3,
        "submit_context_edit_for_review",
        {
            "workspace_id": str(workspace_id),
            "page_id": created["data"]["id"],
            "version_id": updated["data"]["version_id"],
            "expected_editing_revision": updated["data"]["editing_revision"],
            "reason": "P0-1 authenticated HTTP round trip",
            "idempotency_key": "p0-1-http-submit",
            "conversation_token": updated["envelope"]["conversation_token"],
        },
    )
    assert submitted["envelope"]["result_status"] == "succeeded"
    assert submitted["data"]["version_status"] == "draft"

    response = rest.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{created['data']['id']}",
        params={"version_id": updated["data"]["version_id"]},
    )
    assert response.status_code == 200, response.text
    body_md = response.json()["data"]["body_md"]
    assert body_md == "# Updated through authenticated HTTP"


def test_pat_http_context_images_survive_page_round_trips_and_actor_boundaries(
    tmp_path,
):
    """Image composition, ownership, and durable usage work through /mcp."""

    database_path = tmp_path / "p0_3_context_images_http.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        session_secret="test-session-secret",
        public_base_url="http://testserver",
        mcp_http_mode="json_stateless",
        library_blob_root=str(tmp_path / "p0_3_blobs"),
    )
    bootstrap(settings)
    app = create_app(settings=settings)
    factory = app.state.container.db.control_session_factory()
    with factory.begin() as session:
        seed_default_workspace(session)

    rest = TestClient(app)
    workspace_id = UUID(
        authenticate_test_client_in_workspace(
            rest,
            factory,
            workspace_slug="default",
        )
    )
    headers = _issue_pat(
        rest,
        (workspace_id,),
        name="P0.3 image author",
    )

    foreign_rest = TestClient(app)
    authenticate_test_client_in_workspace(
        foreign_rest,
        factory,
        email="p0-3-foreign@example.com",
        workspace_slug="default",
        role="editor",
    )
    foreign_actor_headers = _issue_pat(
        foreign_rest,
        (workspace_id,),
        name="P0.3 foreign image actor",
    )

    with factory.begin() as session:
        foreign_workspace = Workspace(
            slug="p0-3-foreign",
            title="P0-3 Foreign",
            name="P0-3 Foreign",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_page = ContextPage(
            workspace_id=foreign_workspace.id,
            slug="foreign-image-target",
            title="Foreign Image Target",
            page_type="overview",
            lifecycle_status="draft",
        )
        session.add(foreign_page)
        session.flush()
        foreign_page_id = foreign_page.id

    first_png = _png_bytes((24, 96, 160))
    second_png = _png_bytes((160, 96, 24))
    third_png = _png_bytes((32, 160, 96))

    guide = _mcp_structured(
        rest,
        headers,
        10,
        "guide",
        {"workspace_id": str(workspace_id), "conversation_token": "new"},
    )
    assert guide["envelope"]["result_status"] == "succeeded"
    guide_text = " ".join(guide["data"]["required_workflow"])
    assert "editing_version_id" in guide_text
    assert "data.markdown" in guide_text
    token = guide["envelope"]["conversation_token"]

    created = _mcp_structured(
        rest,
        headers,
        11,
        "create_context_page",
        {
            "workspace_id": str(workspace_id),
            "title": "P0-3 MCP Image Acceptance",
            "page_type": "overview",
            "conversation_token": token,
        },
    )
    assert created["envelope"]["result_status"] == "succeeded"
    page_id = created["data"]["id"]
    v1_id = created["data"]["version_id"]
    token = created["envelope"]["conversation_token"]

    first = _mcp_structured(
        rest,
        headers,
        12,
        "upload_image",
        {
            "workspace_id": str(workspace_id),
            "filename": "first.png",
            "mime_type": "image/png",
            "content_base64": base64.b64encode(first_png).decode("ascii"),
            "marker_uuid": str(uuid4()),
            "page_id": page_id,
            "editing_version_id": v1_id,
            "alt_text": "first",
            "conversation_token": token,
        },
    )
    assert first["envelope"]["result_status"] == "succeeded"
    assert first["data"]["content_url"] == (
        f"/api/v1/workspaces/{workspace_id}/context/pages/{page_id}"
        f"/versions/{v1_id}/media/{first['data']['asset_id']}/content"
    )
    assert first["data"]["logical_ref"].startswith("dtx-media://asset/")
    assert "/api/v1/" not in first["data"]["markdown"]
    token = first["envelope"]["conversation_token"]

    second = _mcp_structured(
        rest,
        headers,
        13,
        "upload_image",
        {
            "workspace_id": str(workspace_id),
            "filename": "second.png",
            "mime_type": "image/png",
            "content_base64": base64.b64encode(second_png).decode("ascii"),
            "marker_uuid": str(uuid4()),
            "page_id": page_id,
            "editing_version_id": v1_id,
            "alt_text": "second",
            "conversation_token": token,
        },
    )
    assert second["envelope"]["result_status"] == "succeeded"
    token = second["envelope"]["conversation_token"]
    v1_body = (
        "# P0-3 MCP Image Acceptance\n\n"
        f"{first['data']['markdown']}\n\n{second['data']['markdown']}\n"
    )
    assert "/api/v1/" not in v1_body

    updated_v1 = _mcp_structured(
        rest,
        headers,
        14,
        "update_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": page_id,
            "title": "P0-3 MCP Image Acceptance",
            "body_md": v1_body,
            "base_version_id": v1_id,
            "base_content_hash": created["data"]["content_hash"],
            "expected_editing_revision": created["data"]["editing_revision"],
            "idempotency_key": "p0-3-http-update-v1",
            "conversation_token": token,
        },
    )
    assert updated_v1["envelope"]["result_status"] == "succeeded"
    assert updated_v1["data"]["version_id"] == v1_id
    token = updated_v1["envelope"]["conversation_token"]

    submitted = _mcp_structured(
        rest,
        headers,
        15,
        "submit_context_edit_for_review",
        {
            "workspace_id": str(workspace_id),
            "page_id": page_id,
            "version_id": v1_id,
            "expected_editing_revision": updated_v1["data"]["editing_revision"],
            "reason": "verify canonical HTTP MCP image composition",
            "idempotency_key": "p0-3-http-submit-v1",
            "conversation_token": token,
        },
    )
    assert submitted["envelope"]["result_status"] == "succeeded"
    assert submitted["data"]["version_status"] == "draft"
    token = submitted["envelope"]["conversation_token"]

    blocked = _mcp_structured(
        rest,
        headers,
        16,
        "upload_image",
        {
            "workspace_id": str(workspace_id),
            "filename": "blocked.png",
            "mime_type": "image/png",
            "content_base64": base64.b64encode(first_png).decode("ascii"),
            "marker_uuid": str(uuid4()),
            "page_id": page_id,
            "editing_version_id": v1_id,
            "conversation_token": token,
        },
    )
    assert blocked["envelope"]["error_code"] == "media_editing_not_open"
    assert blocked["envelope"]["retryable"] is False
    token = blocked["envelope"]["conversation_token"]

    foreign_workspace = _mcp_structured(
        rest,
        headers,
        17,
        "upload_image",
        {
            "workspace_id": str(workspace_id),
            "page_id": str(foreign_page_id),
            "filename": "foreign.png",
            "mime_type": "image/png",
            "content_base64": base64.b64encode(first_png).decode("ascii"),
            "marker_uuid": str(uuid4()),
            "editing_version_id": v1_id,
            "conversation_token": token,
        },
    )
    assert foreign_workspace["envelope"]["error_code"] == "inconsistent_identifier"
    token = foreign_workspace["envelope"]["conversation_token"]

    reopened = _mcp_structured(
        rest,
        headers,
        18,
        "begin_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": page_id,
            "idempotency_key": "p0-3-http-begin-v2",
            "conversation_token": token,
        },
    )
    assert reopened["envelope"]["result_status"] == "succeeded"
    assert reopened["data"]["version_id"] != v1_id
    assert reopened["data"]["body_md"] == v1_body
    v2_id = reopened["data"]["version_id"]

    foreign_actor = _mcp_structured(
        foreign_rest,
        foreign_actor_headers,
        19,
        "upload_image",
        {
            "workspace_id": str(workspace_id),
            "page_id": page_id,
            "editing_version_id": v2_id,
            "filename": "foreign-actor.png",
            "mime_type": "image/png",
            "content_base64": base64.b64encode(first_png).decode("ascii"),
            "marker_uuid": str(uuid4()),
            "conversation_token": "new",
        },
    )
    assert foreign_actor["envelope"]["error_code"] == "not_found"
    assert foreign_actor["envelope"]["result_status"] == "failed"
    assert foreign_actor["envelope"]["retryable"] is False

    third = _mcp_structured(
        rest,
        headers,
        20,
        "upload_image",
        {
            "workspace_id": str(workspace_id),
            "page_id": page_id,
            "editing_version_id": v2_id,
            "filename": "third.png",
            "mime_type": "image/png",
            "content_base64": base64.b64encode(third_png).decode("ascii"),
            "marker_uuid": str(uuid4()),
            "alt_text": "third",
            "conversation_token": "new",
        },
    )
    assert third["envelope"]["result_status"] == "succeeded"
    assert "/api/v1/" not in third["data"]["markdown"]
    v2_body = v1_body + "\n" + third["data"]["markdown"] + "\n"

    updated_v2 = _mcp_structured(
        rest,
        headers,
        21,
        "update_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": page_id,
            "body_md": v2_body,
            "base_version_id": v2_id,
            "base_content_hash": reopened["data"]["content_hash"],
            "expected_editing_revision": reopened["data"]["editing_revision"],
            "idempotency_key": "p0-3-http-update-v2",
            "conversation_token": third["envelope"]["conversation_token"],
        },
    )
    assert updated_v2["envelope"]["result_status"] == "succeeded"
    assert updated_v2["data"]["version_id"] == v2_id

    fresh = _mcp_structured(
        rest,
        headers,
        22,
        "begin_context_edit",
        {
            "workspace_id": str(workspace_id),
            "page_id": page_id,
            "idempotency_key": "p0-3-http-resume-v2",
            "conversation_token": "new",
        },
    )
    assert fresh["envelope"]["result_status"] == "succeeded"
    assert fresh["data"]["version_id"] == v2_id
    assert fresh["data"]["body_md"] == v2_body
    assert "/api/v1/" not in fresh["data"]["body_md"]

    asset_ids = [
        UUID(first["data"]["asset_id"]),
        UUID(second["data"]["asset_id"]),
        UUID(third["data"]["asset_id"]),
    ]
    with factory() as session:
        assets = {
            asset.id: asset
            for asset in session.scalars(
                select(ContextMediaAsset).where(
                    ContextMediaAsset.workspace_id == workspace_id,
                )
            ).all()
        }
        assert set(assets) == set(asset_ids)
        assert all(str(asset.page_id) == page_id for asset in assets.values())
        assert all(asset.association_state == "active" for asset in assets.values())
        assert all(
            str(assets[asset_id].created_for_version_id) == v1_id
            for asset_id in asset_ids[:2]
        )
        assert str(assets[asset_ids[2]].created_for_version_id) == v2_id

        v1_usage_count = session.scalar(
            select(func.count(ContextMediaUsage.asset_id)).where(
                ContextMediaUsage.workspace_id == workspace_id,
                ContextMediaUsage.page_version_id == UUID(v1_id),
            )
        )
        v2_usage_count = session.scalar(
            select(func.count(ContextMediaUsage.asset_id)).where(
                ContextMediaUsage.workspace_id == workspace_id,
                ContextMediaUsage.page_version_id == UUID(v2_id),
            )
        )
        assert v1_usage_count == 2
        assert v2_usage_count == 3
        persisted_bodies = session.scalars(
            select(ContextPageVersion.body_md).where(
                ContextPageVersion.page_id == UUID(page_id)
            )
        ).all()
        assert persisted_bodies
        assert all("/api/v1/" not in body for body in persisted_bodies)


def test_one_pat_round_trips_two_workspaces_and_is_denied_a_third(tmp_path):
    """One authenticated HTTP MCP credential is bounded to its grants."""

    from backend.models.user import User
    from backend.models.workspace import WorkspaceMembership

    database_path = tmp_path / "p10_3_multi_workspace.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    settings = Settings(
        database_url=database_url,
        session_secret="test-session-secret",
        public_base_url="http://testserver",
        # A bare TestClient does not enter the stateful session manager's
        # lifespan. Stateless mode changes framing only, not authorization.
        mcp_http_mode="json_stateless",
    )
    bootstrap(settings)
    app = create_app(settings=settings)
    rest = TestClient(app)
    factory = app.state.container.db.control_session_factory()
    with factory.begin() as session:
        seed_default_workspace(session)

    workspace_a = UUID(
        authenticate_test_client_in_workspace(rest, factory, workspace_slug="default")
    )

    def _create_workspace(title: str, slug: str) -> UUID:
        created = rest.post(
            "/api/v1/workspaces",
            json={"title": title, "slug": slug},
        )
        assert created.status_code == 201, created.text
        return UUID(created.json()["data"]["id"])

    workspace_b = _create_workspace("P10.3 Allowed", "p10-3-allowed")
    # The user owns this workspace, but the MCP credential is not granted it.
    workspace_c = _create_workspace("P10.3 Outside", "p10-3-outside")
    with factory.begin() as session:
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        assert user is not None
        user_id = user.id

    issued = rest.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [
                {"workspace_id": str(workspace_a)},
                {"workspace_id": str(workspace_b)},
            ],
            "name": "P10.3 acceptance credential",
            "scopes": ["mcp:read", "mcp:write"],
        },
    )
    assert issued.status_code == 201, issued.text
    plain_token = issued.json()["data"]["plain_token"]
    headers = {"Authorization": f"Bearer {plain_token}"}

    listed = _mcp_call(rest, headers, 1, "list_my_workspaces", {})
    assert listed.status_code == 200, listed.text
    listed_data = listed.json()["result"]["structuredContent"]["data"]
    assert {entry["id"] for entry in listed_data["workspaces"]} == {
        str(workspace_a),
        str(workspace_b),
    }
    with factory.begin() as session:
        membership_total = session.scalar(
            select(func.count(WorkspaceMembership.workspace_id)).where(
                WorkspaceMembership.user_id == user_id
            )
        )
    assert membership_total == 4
    assert listed_data["other_membership_count"] == membership_total - 2
    rendered = listed.text
    assert str(workspace_c) not in rendered
    assert "P10.3 Outside" not in rendered

    page_ids = {}
    for index, workspace_id in enumerate((workspace_a, workspace_b)):
        created = _mcp_call(
            rest,
            headers,
            10 + index,
            "create_context_page",
            {
                "workspace_id": str(workspace_id),
                "conversation_token": "new",
                "title": f"P10.3 acceptance {index}",
                "page_type": "overview",
                "body_md": "# One credential, two workspaces",
            },
        )
        assert created.status_code == 200, created.text
        payload = created.json()["result"]["structuredContent"]
        assert payload["envelope"]["result_status"] == "succeeded", payload["envelope"]
        page_ids[workspace_id] = payload["data"]["id"]

    denied = _mcp_call(
        rest,
        headers,
        20,
        "create_context_page",
        {
            "workspace_id": str(workspace_c),
            "conversation_token": "new",
            "title": "P10.3 must not exist",
            "page_type": "overview",
            "body_md": "# denied",
        },
    )
    denied_body = denied.json()
    assert "result" not in denied_body, denied_body
    assert denied_body["error"]["data"]["error"] == "workspace_ambiguous", denied_body

    # REST read-back proves each HTTP MCP write landed in its target workspace.
    for workspace_id, page_id in page_ids.items():
        response = rest.get(f"/api/v1/workspaces/{workspace_id}/context/pages")
        assert response.status_code == 200, response.text
        assert page_id in {page["id"] for page in response.json()["data"]}
    with factory.begin() as session:
        assert (
            session.scalar(
                select(func.count(ContextPage.id)).where(
                    ContextPage.workspace_id == workspace_c
                )
            )
            == 0
        )
