import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.container import DatabaseProvider
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.audit import AuditEvent
from backend.models.base import Base, utc_now
from backend.models.bundle import AgentInputBundle
from backend.models.mcp_chat import McpChatSession
from backend.models.mcp_trace import MCPToolCall
from backend.models.oauth import OAuthClient, OAuthConsent, OAuthRefreshToken
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.policy.auth import ActorContext
from backend.services.mcp_trace_capture import touch_mcp_client_last_used
from backend.services.oauth_service import (
    OAuthService,
    derive_oauth_connection_state,
    encode_connection_cursor,
)
from backend.services.settings_service import SettingsService
from backend.tests.conftest import authenticate_test_client_in_workspace
from backend.tests.utils.postgres import drop_reflected_postgres_metadata


def _client(page_service) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                public_base_url="http://testserver",
                mcp_http_mode="json_stateless",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(client, page_service.session_factory)
    return client


def _seed_expired_connection(page_service):
    now = utc_now()
    grant_id = uuid4()
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User))
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        oauth_client = OAuthClient(
            id="dyn_c9_expired",
            client_name="Claude Code",
            redirect_uris=["http://127.0.0.1:54321/callback"],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            application_type="native",
            scope="mcp:read",
            created_via="dcr",
            registration_identity="c9-expired-registration",
            last_used_at=now - timedelta(hours=1),
        )
        session.add(oauth_client)
        session.add(
            OAuthConsent(
                user_id=user.id,
                client_id=oauth_client.id,
                workspace_id=workspace.id,
                scope="mcp:read",
            )
        )
        session.add(
            OAuthRefreshToken(
                grant_id=grant_id,
                token_lookup_hash="c9-expired-token",
                user_id=user.id,
                client_id=oauth_client.id,
                scope="mcp:read",
                expires_at=now - timedelta(minutes=1),
            )
        )
        mcp_client = MCPClient(
            workspace_id=workspace.id,
            name=f"OAuth client {oauth_client.id} (user {str(user.id)[:8]})",
            client_type="oauth",
            scopes=["mcp:read"],
            status="active",
            issued_to_user_id=user.id,
            runtime_binding_key=f"oauth_grant:{grant_id}",
            token_metadata={"oauth_client_id": oauth_client.id},
            last_used_at=now - timedelta(hours=1),
        )
        session.add(mcp_client)
        session.flush()
        return user.id, workspace.id, oauth_client.id, mcp_client.id


def test_expired_refresh_family_reads_expired_and_revoke_refetches_revoked(
    page_service,
):
    client = _client(page_service)
    _user_id, workspace_id, oauth_client_id, mcp_client_id = _seed_expired_connection(
        page_service
    )

    consents = client.get("/oauth/consents")
    assert consents.status_code == 200
    assert consents.json()["data"][0]["connection_state"] == "expired"
    assert consents.json()["data"][0]["mcp_client_ids"] == [str(mcp_client_id)]
    policies = client.get(f"/api/v1/workspaces/{workspace_id}/settings/policies")
    connection = next(
        row
        for row in policies.json()["data"]["mcp_clients"]
        if row["id"] == str(mcp_client_id)
    )
    assert connection["connection_state"] == "expired"
    assert connection["oauth_client_id"] == oauth_client_id

    revoked = client.delete(
        "/oauth/consents",
        params={"client_id": oauth_client_id},
    )
    assert revoked.status_code == 200
    assert client.get("/oauth/consents").json()["data"] == []
    refreshed = client.get(f"/api/v1/workspaces/{workspace_id}/settings/policies")
    connection = next(
        row
        for row in refreshed.json()["data"]["mcp_clients"]
        if row["id"] == str(mcp_client_id)
    )
    assert connection["connection_state"] == "revoked"


def test_cleanup_is_idempotent_and_preserves_trace_references(page_service):
    client = _client(page_service)
    _user_id, workspace_id, _oauth_client_id, mcp_client_id = _seed_expired_connection(
        page_service
    )
    with page_service.session_factory.begin() as session:
        session.add(
            MCPToolCall(
                client_id=mcp_client_id,
                workspace_id=workspace_id,
                request_id=uuid4(),
                tool_name="guide",
                input_summary={},
                result_status="succeeded",
            )
        )

    preview = client.get("/oauth/consents/cleanup")
    assert preview.status_code == 200
    assert preview.json()["data"]["eligible_consent_count"] == 1
    first = client.post("/oauth/consents/cleanup")
    second = client.post("/oauth/consents/cleanup")
    assert first.json()["data"]["cleaned_consent_count"] == 1
    assert second.json()["data"]["cleaned_consent_count"] == 0

    with page_service.session_factory() as session:
        assert session.get(MCPClient, mcp_client_id) is not None
        assert session.scalar(select(func.count()).select_from(MCPToolCall)) == 1


def test_cleanup_revokes_only_the_eligible_consent_workspace(page_service):
    """P10.3 T3 (section 2.3): the cleanup UNIT is now the whole (user,
    client) GROUP, not one workspace's consent row -- OAuthRefreshToken lost
    its workspace_id (T1), so "family alive" is a single GLOBAL (user,
    client) fact, identical for every row of that client's group. A dead
    group (the expired connection's OAuthClient "dyn_c9_expired", one
    workspace, no live refresh token) is fully eligible and gets cleaned; an
    entirely SEPARATE client with its own live refresh token in a different
    workspace is a different (user, client) group and stays untouched --
    this is what "only the eligible ... group" now means, since a family can
    no longer be alive in one workspace and dead in another within the SAME
    client (the old same-client two-workspace scenario this test used to
    seed is no longer a distinguishable case).
    """
    client = _client(page_service)
    user_id, _expired_workspace_id, oauth_client_id, _mcp_client_id = (
        _seed_expired_connection(page_service)
    )
    now = utc_now()
    with page_service.session_factory.begin() as session:
        retained_workspace = Workspace(
            slug="cleanup-retained",
            title="Cleanup retained",
            name="Cleanup retained",
        )
        session.add(retained_workspace)
        retained_client = OAuthClient(
            id="dyn_c9_retained",
            client_name="Retained Client",
            redirect_uris=["http://127.0.0.1:54321/callback"],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            application_type="native",
            scope="mcp:read",
            created_via="dcr",
            registration_identity="c9-retained-registration",
        )
        session.add(retained_client)
        session.flush()
        session.add(
            OAuthConsent(
                user_id=user_id,
                client_id=retained_client.id,
                workspace_id=retained_workspace.id,
                scope="mcp:read",
            )
        )
        retained_refresh = OAuthRefreshToken(
            token_lookup_hash="c9-retained-live-token",
            user_id=user_id,
            client_id=retained_client.id,
            scope="mcp:read",
            expires_at=now + timedelta(days=1),
        )
        session.add(retained_refresh)
        session.flush()
        retained_refresh_id = retained_refresh.id
        retained_workspace_id = retained_workspace.id
        retained_client_id = retained_client.id

    cleaned = client.post("/oauth/consents/cleanup")
    assert cleaned.status_code == 200, cleaned.text
    assert cleaned.json()["data"]["cleaned_consent_count"] == 1
    with page_service.session_factory() as session:
        # The dead group's own consent row is gone.
        assert (
            session.get(
                OAuthConsent,
                (user_id, oauth_client_id, _expired_workspace_id),
            )
            is None
        )
        # The unrelated, still-alive client's group is fully untouched.
        retained = session.get(OAuthRefreshToken, retained_refresh_id)
        assert retained is not None
        assert retained.revoked_at is None
        assert (
            session.get(
                OAuthConsent,
                (user_id, retained_client_id, retained_workspace_id),
            )
            is not None
        )


def _seed_connection_page(page_service, *, count: int = 3):
    now = utc_now()
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User))
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert user is not None and workspace is not None
        rows = []
        for index in range(count):
            client_id = f"dyn_c9_page_{index}"
            last_used_at = now - timedelta(minutes=index)
            session.add(
                OAuthClient(
                    id=client_id,
                    client_name=f"Paged client {index}",
                    redirect_uris=["http://127.0.0.1:54321/callback"],
                    token_endpoint_auth_method="none",
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    application_type="native",
                    scope="mcp:read",
                    created_via="dcr",
                    registration_identity=f"c9-page-{index}",
                    last_used_at=last_used_at,
                )
            )
            session.add(
                OAuthConsent(
                    user_id=user.id,
                    client_id=client_id,
                    workspace_id=workspace.id,
                    scope="mcp:read",
                )
            )
            session.add(
                OAuthRefreshToken(
                    token_lookup_hash=f"c9-page-refresh-{index}",
                    user_id=user.id,
                    client_id=client_id,
                    scope="mcp:read",
                    expires_at=now + timedelta(days=1),
                )
            )
            mcp_client = MCPClient(
                workspace_id=workspace.id,
                name=f"Paged MCP client {index}",
                client_type="oauth",
                scopes=["mcp:read"],
                status="active",
                issued_to_user_id=user.id,
                token_metadata={"oauth_client_id": client_id},
                last_used_at=last_used_at,
            )
            session.add(mcp_client)
            session.flush()
            rows.append((client_id, mcp_client.id))
        return user.id, workspace.id, rows


def test_connection_pages_use_recency_keysets_not_offsets(page_service):
    """The MCPClient (Settings > Access) listing still pages on
    last_used_at (a recency keyset) and still has the drop this test name
    describes -- that side was NOT touched by MCP-11's fix (see the module
    docstring's cross-reference below). The OAuth consents listing WAS
    fixed: MCP-11 replaced its keyset with the immutable (client_id,
    workspace_id) remainder of the consent PK specifically because a
    last_used_at-keyed cursor let a token refresh mid-page silently drop a
    connection from the union of both pages. Before that fix this test
    asserted ``consent_second["data"] == []`` -- i.e. it pinned the bug as
    expected behavior. It now asserts the row survives.
    """

    client = _client(page_service)
    _user_id, workspace_id, rows = _seed_connection_page(page_service)

    consent_first = client.get("/oauth/consents", params={"limit": 2}).json()
    policy_first = client.get(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        params={"limit": 2},
    ).json()["data"]
    assert [item["client_id"] for item in consent_first["data"]] == [
        rows[0][0],
        rows[1][0],
    ]
    assert [item["id"] for item in policy_first["mcp_clients"]] == [
        str(rows[0][1]),
        str(rows[1][1]),
    ]

    with page_service.session_factory.begin() as session:
        session.get(OAuthClient, rows[2][0]).last_used_at = utc_now() + timedelta(
            minutes=1
        )
        session.get(MCPClient, rows[2][1]).last_used_at = utc_now() + timedelta(
            minutes=1
        )

    consent_second = client.get(
        "/oauth/consents",
        params={"limit": 2, "cursor": consent_first["next_cursor"]},
    ).json()
    policy_second = client.get(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        params={
            "limit": 2,
            "cursor": policy_first["mcp_clients_next_cursor"],
        },
    ).json()["data"]
    # MCP-11: the touched client's consent survives on the immutable
    # (client_id, workspace_id) keyset -- the union of both pages still
    # covers every seeded connection.
    assert [item["client_id"] for item in consent_second["data"]] == [rows[2][0]]
    # The MCPClient/policies side is untouched by this fix and keeps its
    # known, documented recency-keyset drop.
    second_mcp_ids = {item["id"] for item in policy_second["mcp_clients"]}
    assert second_mcp_ids.isdisjoint({str(rows[0][1]), str(rows[1][1])})


def test_legacy_oauth_consent_cursor_is_rejected_and_restarts_from_page_one(
    page_service,
):
    """P10.3 T3 (section 2.3, Codex review #9/R2-3): the OAuth-consents
    keyset cursor is now client_id alone (one row per (user, client) GROUP,
    not per workspace) and its version marker was bumped v1 -> v2, so a
    cursor encoded before this change fails ``decode_oauth_consent_cursor``'s
    shape/version check outright. That is the safe outcome for a cursor
    whose identity columns no longer match the current listing's grouping --
    silently reinterpreting a (client_id, workspace_id) legacy cursor as a
    client_id-only one could skip or duplicate a client. The endpoint
    degrades to "cursor not recognised -> restart from page one" rather than
    honouring stale legacy cursor shapes indefinitely.
    """

    client = _client(page_service)
    _user_id, _workspace_id, rows = _seed_connection_page(page_service, count=4)
    now = utc_now()
    with page_service.session_factory.begin() as session:
        session.get(OAuthClient, rows[3][0]).last_used_at = now
        session.get(OAuthClient, rows[0][0]).last_used_at = now - timedelta(minutes=1)
        session.get(OAuthClient, rows[1][0]).last_used_at = now - timedelta(minutes=2)
        session.get(OAuthClient, rows[2][0]).last_used_at = now - timedelta(minutes=3)

    legacy_cursor = encode_connection_cursor(
        now - timedelta(minutes=1),
        rows[0][0],
        rows[0][0],
    )
    response = client.get(
        "/oauth/consents",
        params={"limit": 1, "cursor": legacy_cursor},
    )

    assert response.status_code == 200, response.text
    # Unrecognised cursor -> first page in the current (client_id ascending)
    # identity order, not the legacy recency order the stale cursor implied.
    assert [item["client_id"] for item in response.json()["data"]] == [rows[0][0]]
    continuation = client.get(
        "/oauth/consents",
        params={"limit": 1, "cursor": response.json()["next_cursor"]},
    )
    assert continuation.status_code == 200, continuation.text
    assert [item["client_id"] for item in continuation.json()["data"]] == [
        rows[1][0]
    ]


def test_connection_listing_query_count_is_constant_per_page(page_service):
    _client(page_service)
    user_id, workspace_id, _rows = _seed_connection_page(page_service, count=5)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
        public_base_url="http://testserver",
    )
    oauth = OAuthService(
        session_factory=page_service.session_factory,
        settings=settings,
        public_base_url="http://testserver",
        database_provider=DatabaseProvider(
            None, session_factory=page_service.session_factory
        ),
    )
    with page_service.session_factory() as session:
        engine = session.get_bind()

    def count_selects(call):
        statements = 0

        def capture(_conn, _cursor, statement, _params, _context, _many):
            nonlocal statements
            if statement.lstrip().upper().startswith("SELECT"):
                statements += 1

        event.listen(engine, "before_cursor_execute", capture)
        try:
            call()
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        return statements

    oauth_one = count_selects(
        lambda: oauth.list_user_consents(user_id=user_id, limit=1)
    )
    oauth_five = count_selects(
        lambda: oauth.list_user_consents(user_id=user_id, limit=5)
    )
    with page_service.session_factory() as session:
        settings_one = count_selects(
            lambda: SettingsService().get_policies(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                mcp_client_limit=1,
            )
        )
    with page_service.session_factory() as session:
        settings_five = count_selects(
            lambda: SettingsService().get_policies(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                mcp_client_limit=5,
            )
        )
    assert oauth_five == oauth_one
    assert settings_five == settings_one


def test_mcp_last_used_touch_coalesces_within_five_minutes(page_service):
    # The rule only reads and writes MCPClient.last_used_at, so the persisted
    # fixture client is enough: no app, user or OAuth connection is involved.
    with page_service.session_factory.begin() as session:
        client = session.scalar(
            select(MCPClient).where(MCPClient.name == "fixture-system-client")
        )
        assert client is not None
        first_used_at = utc_now()
        client.last_used_at = None
        assert touch_mcp_client_last_used(client, used_at=first_used_at) is True
        session.flush()
        assert (
            touch_mcp_client_last_used(
                client,
                used_at=first_used_at + timedelta(minutes=5),
            )
            is False
        )
        assert session.is_modified(client, include_collections=False) is False
        assert client.last_used_at == first_used_at


def _pat_binding(session, workspace_id):
    user = session.scalar(select(User))
    token = ApiToken(
        user_id=user.id,
        name="Listing credential",
        token_lookup_hash=str(uuid4()),
        hashed_token="unused",
        scopes=["mcp:read"],
    )
    session.add(token)
    session.flush()
    session.add(
        ApiTokenWorkspace(
            token_id=token.id, workspace_id=workspace_id, desired_scopes=["mcp:read"]
        )
    )
    return user.id, f"api_token:{token.id}"


def _seed_manageable_client_facets(page_service):
    now = utc_now()
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        rows = []
        definitions = (
            ("Active alpha", "active"),
            ("Active beta", "active"),
            ("Disabled needle one", "disabled"),
            ("Disabled needle two", "disabled"),
        )
        for index, (name, status) in enumerate(definitions):
            user_id, binding = _pat_binding(session, workspace.id)
            row = MCPClient(
                workspace_id=workspace.id,
                name=name,
                client_type="generic",
                scopes=["mcp:read"],
                status=status,
                created_at=now - timedelta(minutes=index),
                runtime_binding_key=binding,
                issued_to_user_id=user_id,
            )
            session.add(row)
            rows.append(row)
        session.add(
            MCPClient(
                workspace_id=workspace.id,
                name="Deleted client",
                client_type="generic",
                scopes=["mcp:read"],
                status="disabled",
                runtime_binding_key="test-facet:deleted",
                deleted_at=now,
            )
        )
        session.flush()
        return workspace.id, [row.id for row in rows]


def test_mcp_client_list_filters_counts_and_binds_cursor_to_query(page_service):
    client = _client(page_service)
    workspace_id, row_ids = _seed_manageable_client_facets(page_service)
    url = f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients"

    first = client.get(url, params={"limit": 1})
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert [row["id"] for row in first_payload["data"]] == [str(row_ids[0])]
    assert first_payload["total_count"] == 4
    assert first_payload["filtered_count"] == 2
    assert first_payload["status_counts"] == {"active": 2, "disabled": 2}
    assert first_payload["next_cursor"]

    second = client.get(
        url,
        params={"limit": 1, "cursor": first_payload["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert [row["id"] for row in second.json()["data"]] == [str(row_ids[1])]

    disabled = client.get(url, params={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text
    assert {row["id"] for row in disabled.json()["data"]} == {
        str(row_ids[2]),
        str(row_ids[3]),
    }
    assert disabled.json()["filtered_count"] == 2

    searched = client.get(
        url,
        params={"status": "all", "q": "  DISABLED   needle "},
    )
    assert searched.status_code == 200, searched.text
    assert searched.json()["total_count"] == 4
    assert searched.json()["filtered_count"] == 2
    assert searched.json()["status_counts"] == {"active": 0, "disabled": 2}

    mismatched = client.get(
        url,
        params={
            "status": "disabled",
            "limit": 1,
            "cursor": first_payload["next_cursor"],
        },
    )
    assert mismatched.status_code == 422
    assert mismatched.json()["error_code"] == "mcp_client_cursor_invalid"

    original_cursor = first_payload["next_cursor"]
    replacement = "A" if original_cursor[-1] != "A" else "B"
    tampered_cursor = f"{original_cursor[:-1]}{replacement}"
    tampered = client.get(url, params={"limit": 1, "cursor": tampered_cursor})
    assert tampered.status_code == 422
    assert tampered.json()["error_code"] == "mcp_client_cursor_invalid"


def test_mcp_client_cursor_traverses_more_than_one_default_page_exactly_once(
    page_service,
):
    client = _client(page_service)
    now = utc_now()
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        assert workspace is not None
        rows = []
        for index in range(23):
            user_id, binding = _pat_binding(session, workspace.id)
            row = MCPClient(
                workspace_id=workspace.id,
                name=f"Cursor traversal client {index:02d}",
                client_type="generic",
                scopes=["mcp:read"],
                status="active",
                runtime_binding_key=binding,
                issued_to_user_id=user_id,
                created_at=now - timedelta(seconds=index),
            )
            session.add(row)
            rows.append(row)
        session.flush()
        workspace_id = workspace.id
        expected_ids = [str(row.id) for row in rows]

    url = f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients"
    seen_ids: list[str] = []
    page_sizes: list[int] = []
    cursor = None
    page_count = 0
    while True:
        params = {"cursor": cursor} if cursor is not None else {}
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        payload = response.json()
        page_ids = [row["id"] for row in payload["data"]]
        assert not set(page_ids).intersection(seen_ids)
        assert payload["total_count"] == 23
        assert payload["filtered_count"] == 23
        assert payload["status_counts"] == {"active": 23, "disabled": 0}
        seen_ids.extend(page_ids)
        page_sizes.append(len(page_ids))
        page_count += 1
        cursor = payload["next_cursor"]
        if cursor is None:
            break

    assert page_count == 2
    assert page_sizes == [20, 3]
    assert seen_ids == expected_ids
    assert len(seen_ids) == len(set(seen_ids)) == 23


def test_disabled_client_delete_tombstones_and_preserves_history(page_service):
    client = _client(page_service)
    now = utc_now()
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User))
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        disabled = MCPClient(
            workspace_id=workspace.id,
            name="Delete history client",
            client_type="generic",
            scopes=["mcp:read"],
            status="disabled",
            runtime_binding_key="delete-history-binding",
        )
        active = MCPClient(
            workspace_id=workspace.id,
            name="Active cannot delete",
            client_type="generic",
            scopes=["mcp:read"],
            status="active",
            runtime_binding_key="active-delete-binding",
        )
        session.add_all([disabled, active])
        session.flush()
        tool_call = MCPToolCall(
            client_id=disabled.id,
            workspace_id=workspace.id,
            request_id=uuid4(),
            tool_name="guide",
            input_summary={},
            result_status="succeeded",
        )
        chat = McpChatSession(
            workspace_id=workspace.id,
            client_id=disabled.id,
            owner_user_id=user.id,
            mcp_session_id="delete-history-chat",
        )
        bundle = AgentInputBundle(
            workspace_id=workspace.id,
            client_id=disabled.id,
            task_intent="preserve deletion history",
            token_budget=100,
            ttl_seconds=3600,
            valid_until=now + timedelta(hours=1),
            invalidation_keys={},
        )
        session.add_all([tool_call, chat, bundle])
        session.flush()
        workspace_id = workspace.id
        actor_id = str(user.id)
        disabled_id = disabled.id
        active_id = active.id
        tool_call_id = tool_call.id
        chat_id = chat.id
        bundle_id = bundle.id

    active_delete = client.delete(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{active_id}"
    )
    assert active_delete.status_code == 409
    assert active_delete.json()["error_code"] == "mcp_client_must_be_disabled"

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{disabled_id}"
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["deleted_client_id"] == str(disabled_id)
    assert deleted.json()["data"]["deleted_at"]

    deleted_again = client.delete(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{disabled_id}"
    )
    assert deleted_again.status_code == 409
    assert deleted_again.json()["error_code"] == "mcp_client_already_deleted"

    listed = client.get(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients",
        params={"status": "all"},
    )
    assert str(disabled_id) not in {row["id"] for row in listed.json()["data"]}

    reactivated = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{disabled_id}",
        json={"status": "active"},
    )
    assert reactivated.status_code == 422

    with page_service.session_factory() as session:
        tombstone = session.get(MCPClient, disabled_id)
        assert tombstone is not None
        assert tombstone.deleted_at is not None
        assert tombstone.status == "disabled"
        assert tombstone.runtime_binding_key == "delete-history-binding"
        assert session.get(MCPToolCall, tool_call_id) is not None
        assert session.get(McpChatSession, chat_id) is not None
        assert session.get(AgentInputBundle, bundle_id) is not None
        assert session.get(MCPClient, active_id).deleted_at is None
        deletion_events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "settings.mcp_client.deleted",
                    AuditEvent.target_id == disabled_id,
                )
            ).all()
        )
        assert len(deletion_events) == 1
        deletion_event = deletion_events[0]
        assert deletion_event.workspace_id == workspace_id
        assert deletion_event.actor_kind == "human"
        assert deletion_event.actor_id == actor_id
        assert deletion_event.target_kind == "mcp_client"
        assert deletion_event.payload == {
            "client_type": "generic",
            "previous_status": "disabled",
        }
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "settings.mcp_client.deleted",
                    AuditEvent.target_id == active_id,
                )
            )
            == 0
        )


def test_mcp_client_delete_rejects_system_foreign_and_non_owner(page_service):
    owner = _client(page_service)
    with page_service.session_factory.begin() as session:
        default_workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        assert default_workspace is not None
        system_client = MCPClient(
            workspace_id=default_workspace.id,
            name="System managed client",
            client_type="system",
            scopes=["mcp:read"],
            status="disabled",
            runtime_binding_key="delete-denial:system",
        )
        viewer_target = MCPClient(
            workspace_id=default_workspace.id,
            name="Viewer cannot delete",
            client_type="generic",
            scopes=["mcp:read"],
            status="disabled",
            runtime_binding_key="delete-denial:viewer",
        )
        foreign_workspace = Workspace(
            slug="mcp-delete-foreign",
            title="MCP delete foreign",
            name="MCP delete foreign",
        )
        session.add_all([system_client, viewer_target, foreign_workspace])
        session.flush()
        foreign_client = MCPClient(
            workspace_id=foreign_workspace.id,
            name="Foreign disabled client",
            client_type="generic",
            scopes=["mcp:read"],
            status="disabled",
            runtime_binding_key="delete-denial:foreign",
        )
        session.add(foreign_client)
        session.flush()
        workspace_id = default_workspace.id
        system_id = system_client.id
        viewer_target_id = viewer_target.id
        foreign_workspace_id = foreign_workspace.id
        foreign_client_id = foreign_client.id

    system_denied = owner.delete(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{system_id}"
    )
    assert system_denied.status_code == 409
    assert system_denied.json()["error_code"] == "mcp_client_system_managed"

    cross_workspace_client_denied = owner.delete(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{foreign_client_id}"
    )
    assert cross_workspace_client_denied.status_code == 404
    assert cross_workspace_client_denied.json()["error_code"] == "not_found"

    foreign_workspace_denied = owner.delete(
        f"/api/v1/workspaces/{foreign_workspace_id}/settings/mcp-clients/"
        f"{foreign_client_id}"
    )
    assert foreign_workspace_denied.status_code == 404
    assert foreign_workspace_denied.json()["error_code"] == "not_found"

    viewer = TestClient(owner.app)
    authenticate_test_client_in_workspace(
        viewer,
        page_service.session_factory,
        email="mcp-delete-viewer@example.com",
        role="viewer",
    )
    non_owner_denied = viewer.delete(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{viewer_target_id}"
    )
    assert non_owner_denied.status_code == 403
    assert non_owner_denied.json()["error_code"] == "workspace_role_insufficient"

    with page_service.session_factory() as session:
        for client_id in (system_id, viewer_target_id, foreign_client_id):
            assert session.get(MCPClient, client_id).deleted_at is None
        denied_target_ids = {system_id, viewer_target_id, foreign_client_id}
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "settings.mcp_client.deleted",
                    AuditEvent.target_id.in_(denied_target_ids),
                )
            )
            == 0
        )


def test_mcp_client_delete_rejects_bearer_pat_even_for_owner(page_service):
    owner = _client(page_service)
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        assert user is not None and workspace is not None
        target = MCPClient(
            workspace_id=workspace.id,
            name="PAT cannot delete",
            client_type="generic",
            scopes=["mcp:read"],
            status="disabled",
            runtime_binding_key="delete-denial:pat",
        )
        session.add(target)
        session.flush()
        workspace_id = workspace.id
        target_id = target.id

    issued = owner.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": str(workspace_id)}],
            "name": "Delete denial PAT",
            "scopes": ["mcp:read"],
        },
    )
    assert issued.status_code == 201, issued.text
    plain_token = issued.json()["data"]["plain_token"]

    bearer = TestClient(owner.app)
    denied = bearer.delete(
        f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{target_id}",
        headers={"Authorization": f"Bearer {plain_token}"},
    )
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "scope_denied"

    with page_service.session_factory() as session:
        assert session.get(MCPClient, target_id).deleted_at is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "settings.mcp_client.deleted",
                    AuditEvent.target_id == target_id,
                )
            )
            == 0
        )


def _seed_connection_state_axis_fixture(
    page_service, *, group_size: int, now: datetime | None = None
):
    """Seed `group_size` clients in each of the four connection states.

    Every client except the disabled group carries ``status="active"``, so a
    facet that still keyed off the physical column would put all of
    active+expired+revoked on one side. The revoked group deliberately keeps a
    live refresh token: that proves ``OAuthClient.revoked_at`` is checked in its
    own right rather than inferred from token state.

    ``now`` is the instant the token expiries are measured from; a test that
    judges them at a different clock must pass the clock it will use.
    """

    if now is None:
        now = utc_now()
    expected: dict[str, set[str]] = {
        "active": set(),
        "expired": set(),
        "revoked": set(),
        "disabled": set(),
    }
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User))
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        for state in ("active", "expired", "revoked", "disabled"):
            for index in range(group_size):
                grant_id = uuid4()
                oauth_client_id = f"dyn_axis_{state}_{index}"
                session.add(
                    OAuthClient(
                        id=oauth_client_id,
                        client_name=f"Axis {state} {index}",
                        redirect_uris=["http://127.0.0.1:54321/callback"],
                        token_endpoint_auth_method="none",
                        grant_types=["authorization_code", "refresh_token"],
                        response_types=["code"],
                        application_type="native",
                        scope="mcp:read",
                        created_via="dcr",
                        registration_identity=f"axis-{state}-{index}",
                        # Only a revoked grant carries a revocation timestamp.
                        revoked_at=(
                            now - timedelta(hours=1) if state == "revoked" else None
                        ),
                    )
                )
                session.add(
                    OAuthConsent(
                        user_id=user.id,
                        client_id=oauth_client_id,
                        workspace_id=workspace.id,
                        scope="mcp:read",
                    )
                )
                session.add(
                    OAuthRefreshToken(
                        grant_id=grant_id,
                        token_lookup_hash=f"axis-{state}-{index}-token",
                        user_id=user.id,
                        client_id=oauth_client_id,
                        scope="mcp:read",
                        # "expired" is the only state without a live token; the
                        # revoked group keeps one on purpose (see docstring).
                        expires_at=(
                            now - timedelta(minutes=1)
                            if state == "expired"
                            else now + timedelta(days=1)
                        ),
                    )
                )
                mcp_client = MCPClient(
                    workspace_id=workspace.id,
                    name=f"Axis {state} client {index:02d}",
                    client_type="oauth",
                    scopes=["mcp:read"],
                    status="disabled" if state == "disabled" else "active",
                    issued_to_user_id=user.id,
                    runtime_binding_key=f"oauth_grant:{grant_id}",
                    token_metadata={"oauth_client_id": oauth_client_id},
                    last_used_at=now - timedelta(hours=1),
                )
                session.add(mcp_client)
                session.flush()
                expected[state].add(str(mcp_client.id))
        return workspace.id, expected


def test_mcp_client_active_deactivated_facet_tracks_connection_health(page_service):
    client = _client(page_service)
    workspace_id, expected = _seed_connection_state_axis_fixture(
        page_service, group_size=1
    )
    url = f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients"

    active = client.get(url, params={"status": "active", "limit": 50})
    assert active.status_code == 200, active.text
    active_payload = active.json()
    # Only the genuinely connected client is Active, even though three of the
    # four rows still carry status="active" in the database.
    assert {row["id"] for row in active_payload["data"]} == expected["active"]
    assert active_payload["status_counts"] == {"active": 1, "disabled": 3}
    assert active_payload["filtered_count"] == 1
    assert [row["connection_state"] for row in active_payload["data"]] == ["active"]

    deactivated = client.get(url, params={"status": "disabled", "limit": 50})
    assert deactivated.status_code == 200, deactivated.text
    deactivated_payload = deactivated.json()
    assert {row["id"] for row in deactivated_payload["data"]} == (
        expected["expired"] | expected["revoked"] | expected["disabled"]
    )
    assert deactivated_payload["filtered_count"] == 3
    assert {row["connection_state"] for row in deactivated_payload["data"]} == {
        "expired",
        "revoked",
        "disabled",
    }
    # The physical column is untouched: the split is connection-based.
    still_physically_active = sum(
        1 for row in deactivated_payload["data"] if row["status"] == "active"
    )
    assert still_physically_active == 2

    everything = client.get(url, params={"status": "all", "limit": 50})
    assert everything.status_code == 200, everything.text
    assert everything.json()["filtered_count"] == 4


def test_mcp_client_cursor_traverses_health_facets_exactly_once(
    page_service, monkeypatch
):
    client = _client(page_service)
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    # This is a stable-health traversal test. Production reevaluates UTC on
    # every page, so a clock rollback can revive expired refresh tokens and
    # ordinary expiry can move rows between facets mid-walk. Snapshot or
    # irreversible-expiry semantics would need a separate product decision.
    monkeypatch.setattr("backend.services.settings_service.utc_now", lambda: now)
    workspace_id, expected = _seed_connection_state_axis_fixture(
        page_service, group_size=21, now=now
    )
    url = f"/api/v1/workspaces/{workspace_id}/settings/mcp-clients"
    deactivated_expected = (
        expected["expired"] | expected["revoked"] | expected["disabled"]
    )

    # Each facet gets one newest row followed by a tied group that spans the
    # page edge. ORM defaults use wall time, so pin the committed ordering keys
    # to exercise both columns of the (created_at, id) cursor.
    with page_service.session_factory.begin() as session:
        for wanted in (expected["active"], deactivated_expected):
            for index, client_id in enumerate(sorted(wanted)):
                model = session.get(MCPClient, UUID(client_id))
                assert model is not None
                model.created_at = now - timedelta(seconds=min(index, 1))

    for status, wanted in (
        ("active", expected["active"]),
        ("disabled", deactivated_expected),
    ):
        ordered_ids = sorted(wanted)
        expected_order = [ordered_ids[0], *reversed(ordered_ids[1:])]
        seen: list[str] = []
        cursor = None
        while True:
            params = {"status": status, "limit": 20}
            if cursor:
                params["cursor"] = cursor
            response = client.get(url, params=params)
            assert response.status_code == 200, response.text
            payload = response.json()
            # Counts must stay stable on every page of the walk.
            assert payload["status_counts"] == {"active": 21, "disabled": 63}
            assert payload["filtered_count"] == len(wanted)
            seen.extend(row["id"] for row in payload["data"])
            assert len(seen) <= len(wanted), f"{status} cursor did not progress"
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        assert len(seen) == len(set(seen)), f"{status} facet returned duplicates"
        assert set(seen) == wanted, f"{status} facet dropped or added rows"
        assert seen == expected_order, f"{status} facet returned incorrect order"


def _count_selects(engine, call) -> int:
    statements = 0

    def capture(_conn, _cursor, statement, _params, _context, _many):
        nonlocal statements
        if statement.lstrip().upper().startswith("SELECT"):
            statements += 1

    event.listen(engine, "before_cursor_execute", capture)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    return statements


def test_revoked_or_missing_oauth_client_skips_the_consent_query(page_service):
    """A dead OAuth client settles the state without a consent SELECT.

    ``classify_oauth_connection_state`` answers "revoked" from
    ``oauth_client_is_live`` alone -- the consent input cannot change that
    answer -- so the consent lookup is skipped rather than issued and thrown
    away. This is the consent screen's own hot path: the screen exists to
    render exactly the branch where a connection has gone bad.

    Pinned as an exact statement count because the regression it guards was
    invisible in behaviour: the state stayed "revoked" either way, the query
    budget went up 50%.
    """

    _client(page_service)
    with page_service.session_factory() as session:
        engine = session.get_bind()
    now = utc_now()
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User))
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        session.add(
            OAuthClient(
                id="dyn_c9_revoked_client",
                client_name="Revoked App",
                redirect_uris=["http://127.0.0.1:54321/callback"],
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                application_type="native",
                scope="mcp:read",
                created_via="dcr",
                registration_identity="c9-revoked-client-registration",
                revoked_at=now,
            )
        )
        session.flush()
        session.add(
            OAuthConsent(
                user_id=user.id,
                client_id="dyn_c9_revoked_client",
                workspace_id=workspace.id,
                scope="mcp:read",
            )
        )
        user_id = user.id
        workspace_id = workspace.id

    for client_id in ("dyn_c9_revoked_client", "dyn_c9_never_registered"):
        with page_service.session_factory() as session:
            state = None

            def call(session=session, client_id=client_id):
                nonlocal state
                state, _ = derive_oauth_connection_state(
                    session,
                    client_id=client_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    physical_status="active",
                    now=now,
                )

            statements = _count_selects(engine, call)
        assert state == "revoked", client_id
        assert statements == 2, (
            f"{client_id}: expected the live-token scan and the client lookup "
            f"only, got {statements} SELECTs"
        )


def _postgres_url() -> str | None:
    value = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    return value if value and value.startswith("postgresql") else None


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires the dedicated PostgreSQL migration test database",
)
def test_postgres_mcp_client_listing_facets_split_on_the_pushed_down_keys():
    """Execute the rewritten pushdown against PostgreSQL, not just SQLite.

    The Active facet stopped being three correlated ``EXISTS`` subqueries --
    portable by construction -- and became a row-value ``IN`` that mixes a
    ``Uuid`` column with a JSON text extraction:
    ``tuple_(mcp_client.issued_to_user_id, token_metadata->>'oauth_client_id')
    .in_(...)``. SQLite is permissive about both row values and JSON typing in
    ways PostgreSQL is not, so a SQLite-only suite cannot say the shape is
    portable.

    NOTE for anyone reading a green run: this test is SKIPPED unless
    ``DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL`` points at a live
    database, which is how it stayed skipped locally while the exact-identity
    health rewrite made its old fixtures read "unknown" (rows with no
    ``runtime_binding_key``/``grant_id``). The ``postgres`` xdist group selects
    this test for the PR PostgreSQL gate and serializes its schema teardown
    with the other suites sharing that database. The fixtures name their
    credentials exactly. It also executes the PAT and
    OAuth credential reads (``load_credential_health``) on PostgreSQL. The
    always-on companion is
    ``test_mcp_access_db_consistency.test_active_connection_predicate_compiles
    _on_postgresql``, which only compiles the expression.
    """

    database_url = _postgres_url()
    assert database_url is not None
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    workspace_id = uuid4()
    live_user_id = uuid4()
    dry_user_id = uuid4()
    oauth_client_id = "pg-shared-app"
    live_grant_id, dry_grant_id = uuid4(), uuid4()
    try:
        with session_factory.begin() as session:
            session.add(
                Workspace(
                    id=workspace_id,
                    slug="pg-mcp-facets",
                    title="PG MCP facets",
                    name="PG MCP facets",
                )
            )
            for user_id, email in (
                (live_user_id, "pg-live@example.com"),
                (dry_user_id, "pg-dry@example.com"),
            ):
                session.add(
                    User(
                        id=user_id,
                        email=email,
                        password_hash="unused",
                        display_name=email,
                    )
                )
            session.add(
                OAuthClient(
                    id=oauth_client_id,
                    client_name="Postgres Shared App",
                    redirect_uris=["https://client.example/callback"],
                    scope="mcp:read",
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                )
            )
            session.flush()
            for user_id, name, grant_id in (
                (live_user_id, "PG live holder", live_grant_id),
                (dry_user_id, "PG tokenless holder", dry_grant_id),
            ):
                session.add(
                    OAuthConsent(
                        user_id=user_id,
                        client_id=oauth_client_id,
                        workspace_id=workspace_id,
                        scope="mcp:read",
                    )
                )
                session.add(
                    MCPClient(
                        workspace_id=workspace_id,
                        name=name,
                        client_type="oauth",
                        scopes=["mcp:read"],
                        status="active",
                        issued_to_user_id=user_id,
                        runtime_binding_key=f"oauth_grant:{grant_id}",
                        token_metadata={"oauth_client_id": oauth_client_id},
                    )
                )
            session.add(
                OAuthRefreshToken(
                    token_lookup_hash="sha256:pg-live-holder",
                    user_id=live_user_id,
                    client_id=oauth_client_id,
                    grant_id=live_grant_id,
                    scope="mcp:read",
                    expires_at=utc_now() + timedelta(days=7),
                )
            )

            session.add(
                WorkspaceMembership(
                    user_id=live_user_id, workspace_id=workspace_id, role="owner"
                )
            )
            session.add(
                WorkspaceMembership(
                    user_id=dry_user_id, workspace_id=workspace_id, role="owner"
                )
            )
            pat = ApiToken(
                user_id=live_user_id,
                name="PG key",
                token_lookup_hash="sha256:pg-pat",
                hashed_token="unused",
                scopes=["mcp:read"],
            )
            session.add(pat)
            session.flush()
            session.add(
                ApiTokenWorkspace(
                    token_id=pat.id,
                    workspace_id=workspace_id,
                    desired_scopes=["mcp:read"],
                    revision=1,
                    sync_state="synced",
                )
            )
            session.add(
                MCPClient(
                    workspace_id=workspace_id,
                    name="PG PAT holder",
                    client_type="generic",
                    scopes=["mcp:read"],
                    status="active",
                    issued_to_user_id=live_user_id,
                    runtime_binding_key=f"api_token:{pat.id}",
                )
            )

        actor = ActorContext(
            actor_kind="human",
            actor_id=str(live_user_id),
            user_id=live_user_id,
            workspace_id=workspace_id,
            role="owner",
            token_kind="session",
            provenance="browser_session",
        )

        def listing(status: str, query: str | None = None):
            with session_factory() as control_session, session_factory() as ws_session:
                return SettingsService().list_mcp_clients(
                    control_session=control_session,
                    workspace_session=ws_session,
                    workspace_id=workspace_id,
                    actor=actor,
                    status=status,
                    query=query,
                    cursor_secret="pg-facet-secret",
                )

        everything = listing("all")
        assert everything.status_counts.active == 2
        assert everything.status_counts.disabled == 1
        assert {row.name for row in listing("active").data} == {
            "PG live holder",
            "PG PAT holder",
        }
        assert {row.name for row in listing("disabled").data} == {
            "PG tokenless holder"
        }
        # The client_name search arm, which is the other pushed-down key set.
        assert {row.name for row in listing("all", "postgres shared").data} == {
            "PG live holder",
            "PG tokenless holder",
        }
    finally:
        drop_reflected_postgres_metadata(engine)
        engine.dispose()


def test_credential_workspace_candidates_require_a_browser_session(page_service):
    client = _client(page_service)
    response = client.get("/api/v1/auth/credential-workspaces", params={"limit": 8})
    assert response.status_code == 200
    assert response.json()["total_count"] >= 1
    assert all(
        row["role"] in {"owner", "editor", "viewer"} for row in response.json()["data"]
    )
    invalid = client.get(
        "/api/v1/auth/credential-workspaces", params={"cursor": "invalid"}
    )
    assert invalid.status_code == 422
    client.cookies.clear()
    assert client.get("/api/v1/auth/credential-workspaces").status_code == 401
    assert (
        client.get(
            "/api/v1/auth/credential-workspaces",
            headers={"Authorization": "Bearer invalid"},
        ).status_code
        == 401
    )
