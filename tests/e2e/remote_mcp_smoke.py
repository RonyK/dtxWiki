from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.base import Base
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.policy.rbac import grant_workspace_membership
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.oauth_service import _DCR_BUCKET, challenge_s256
from backend.services.page_service import PageService
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.cross_transport_remote


@dataclass(frozen=True)
class SmokeContext:
    client: Any
    page_service: PageService | None
    mcp_url: str | None
    mcp_verify: bool | str
    use_streamable_sdk: bool
    workspace_id: str
    pat_read: str
    pat_agent: str
    page_id: str
    page_slug: str
    foreign_workspace_id: str | None = None
    foreign_page_id: str | None = None
    restricted_workspace_id: str | None = None
    restricted_artifact_id: str | None = None


@dataclass(frozen=True)
class OAuthTokens:
    client_id: str
    access_token: str
    refresh_token: str


@pytest.fixture
def smoke_context(request: pytest.FixtureRequest) -> SmokeContext:
    mode = request.config.getoption("--remote-smoke-mode")
    if mode == "local":
        return _local_smoke_context()
    remote_ready = request.getfixturevalue("remote_ready")
    seed = request.getfixturevalue("remote_seed_material")
    context = _remote_smoke_context(remote_ready, seed)
    request.addfinalizer(context.client.close)
    return context


def _local_smoke_context() -> SmokeContext:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        seed_default_workspace(session)
    page_service = PageService(factory)
    app = create_app(
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            public_base_url="http://testserver",
            session_secret="test-session-secret",
            mcp_http_mode="json_stateless",
        ),
        page_service=page_service,
    )
    client = TestClient(app)
    # P10.3 T4 (D-01v2): issuing a PAT now validates live membership in
    # EVERY workspace the grant names, so signing up alone (which only
    # provisions the Personal workspace) is no longer enough to mint a
    # token for "default" -- that returns 403
    # api_token_workspace_access_denied. Grant the membership this
    # harness always implied it had.
    authenticate_test_client_in_workspace(
        client, factory, email="phase5@example.com", workspace_slug="default"
    )

    with factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        workspace_id = str(workspace.id)

    page = page_service.write_page(
        workspace_slug="default",
        slug="phase5-remote-page",
        title="Phase 5 Remote Page",
        body_md="# Phase 5 Remote Page\n\nRemote smoke content snippet.",
        actor_kind="system",
        actor_id="phase5-smoke",
        actor_provenance="browser_session",
    )
    pat_read = _issue_pat(client, ["mcp:read"], workspace_id=workspace_id)
    pat_agent = _issue_pat(
        client, ["mcp:read", "mcp:write", "mcp:agent"], workspace_id=workspace_id
    )
    return SmokeContext(
        client=client,
        page_service=page_service,
        mcp_url=None,
        mcp_verify=True,
        use_streamable_sdk=False,
        workspace_id=workspace_id,
        pat_read=pat_read,
        pat_agent=pat_agent,
        page_id=str(page.id),
        page_slug=page.slug,
    )


def _remote_smoke_context(
    config: Any,
    seed: Any,
) -> SmokeContext:
    if not config.origin.startswith("https://"):
        raise AssertionError(
            "Phase 5 remote smoke must use an https:// origin, "
            f"got {config.origin!r}"
        )
    verify: bool | str = config.ca_file or True
    client = httpx.Client(base_url=config.origin, verify=verify, timeout=20.0)
    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": seed.login_email,
            "password": seed.login_password,
        },
    )
    assert login.status_code == 200, login.text
    return SmokeContext(
        client=client,
        page_service=None,
        mcp_url=config.mcp_url,
        mcp_verify=verify,
        use_streamable_sdk=True,
        workspace_id=seed.workspace_id,
        pat_read=seed.pat_read,
        pat_agent=seed.pat_agent,
        page_id=seed.page_id,
        page_slug=seed.page_slug,
        foreign_workspace_id=seed.foreign_workspace_id,
        foreign_page_id=seed.foreign_page_id,
        restricted_workspace_id=seed.restricted_workspace_id,
        restricted_artifact_id=seed.restricted_artifact_id,
    )


def test_pat_immediate_connect_list_tools(smoke_context: SmokeContext):
    listed = _mcp_list_tools(smoke_context, smoke_context.pat_read)

    names = set(listed)
    assert {"read", "search"}.issubset(names)


def test_pat_read_page_round_trip(smoke_context: SmokeContext):
    payload = _mcp_call(
        smoke_context,
        smoke_context.pat_read,
        "read",
        {
            "workspace_id": smoke_context.workspace_id,
            "target_type": "context_page",
            "target_id": smoke_context.page_id,
        },
    )

    assert payload["data"]["slug"] == smoke_context.page_slug
    assert "Remote smoke content" in payload["data"]["body_markdown"]


def test_pat_write_agent_page_draft_round_trip(smoke_context: SmokeContext):
    payload = _mcp_call(
        smoke_context,
        smoke_context.pat_agent,
        "write_agent_page_draft",
        _agent_draft_args(smoke_context.workspace_id, "phase5-pat-draft"),
    )

    data = payload["data"]
    assert data["agent_page_id"]
    assert data["draft_version_id"]


def test_oauth_full_flow_tool_round_trip(smoke_context: SmokeContext):
    tokens = _issue_oauth_tokens(
        smoke_context.client,
        workspace_id=smoke_context.workspace_id,
        scope="mcp:read mcp:agent",
        state="phase5-full-flow-state-000001",
    )

    listed = _mcp_list_tools(smoke_context, tokens.access_token)
    names = set(listed)
    assert "write_agent_page_draft" in names
    read = _mcp_call(
        smoke_context,
        tokens.access_token,
        "read",
        {
            "workspace_id": smoke_context.workspace_id,
            "target_type": "context_page",
            "target_id": smoke_context.page_id,
        },
    )
    assert read["data"]["slug"] == smoke_context.page_slug
    draft = _mcp_call(
        smoke_context,
        tokens.access_token,
        "write_agent_page_draft",
        _agent_draft_args(smoke_context.workspace_id, "phase5-oauth-draft"),
    )
    assert draft["data"]["draft_version_id"]


def test_oauth_refresh_rotation_and_reuse_denied_via_jti(
    smoke_context: SmokeContext,
):
    tokens = _issue_oauth_tokens(
        smoke_context.client,
        workspace_id=smoke_context.workspace_id,
        scope="mcp:read",
        state="phase5-refresh-state-0000001",
    )
    refreshed = smoke_context.client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens.client_id,
            "refresh_token": tokens.refresh_token,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    new_access_token = refreshed.json()["access_token"]
    assert refreshed.json()["refresh_token"] != tokens.refresh_token
    assert _mcp_list_tools(smoke_context, new_access_token)
    read = _mcp_call(
        smoke_context,
        new_access_token,
        "read",
        {
            "workspace_id": smoke_context.workspace_id,
            "target_type": "context_page",
            "target_id": smoke_context.page_id,
        },
    )
    assert read["data"]["slug"] == smoke_context.page_slug

    reused = smoke_context.client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens.client_id,
            "refresh_token": tokens.refresh_token,
        },
    )
    assert reused.status_code == 400, reused.text
    assert reused.json()["error"] == "invalid_grant"

    _mcp_list_tools_expect_unauthorized(smoke_context, new_access_token)


def test_oauth_revoke_invalidates_access_jti(smoke_context: SmokeContext):
    tokens = _issue_oauth_tokens(
        smoke_context.client,
        workspace_id=smoke_context.workspace_id,
        scope="mcp:read",
        state="phase5-revoke-state-00000001",
    )
    revoked = smoke_context.client.post(
        "/oauth/revoke",
        data={
            "token": tokens.access_token,
            "token_type_hint": "access_token",
            "client_id": tokens.client_id,
        },
    )
    assert revoked.status_code == 200, revoked.text

    _mcp_list_tools_expect_unauthorized(smoke_context, tokens.access_token)


def test_cross_workspace_denied_with_keyword(smoke_context: SmokeContext):
    if smoke_context.page_service is not None:
        workspace_id, page_id = _seed_foreign_workspace(smoke_context.page_service)
    else:
        assert smoke_context.foreign_workspace_id is not None
        assert smoke_context.foreign_page_id is not None
        workspace_id = smoke_context.foreign_workspace_id
        page_id = smoke_context.foreign_page_id
    _mcp_call_expect_denied(
        smoke_context,
        smoke_context.pat_read,
        "read",
        {
            "workspace_id": workspace_id,
            "target_type": "context_page",
            "target_id": page_id,
        },
        "workspace",
    )


def test_restricted_library_denied_with_keyword(smoke_context: SmokeContext):
    if smoke_context.page_service is not None:
        workspace_id, artifact_id = _seed_viewer_restricted_workspace(
            smoke_context.page_service
        )
    else:
        assert smoke_context.restricted_workspace_id is not None
        assert smoke_context.restricted_artifact_id is not None
        workspace_id = smoke_context.restricted_workspace_id
        artifact_id = smoke_context.restricted_artifact_id
    _mcp_call_expect_denied(
        smoke_context,
        smoke_context.pat_read,
        "read",
        {
            "workspace_id": workspace_id,
            "target_type": "library_artifact",
            "target_id": artifact_id,
        },
        "restricted_library_denied",
    )

def _issue_pat(client: Any, scopes: list[str], *, workspace_id: str) -> str:
    # P10.3 T4 (D-01v2): a PAT carries an allowed-workspace SET, and
    # `workspace_grants` is required with at least one entry. Omitting it --
    # which this helper used to do, back when a token inherited the caller's
    # current workspace -- is now a 422.
    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": workspace_id}],
            "name": "Phase 5 Smoke",
            "scopes": scopes,
        },
    )
    assert issued.status_code == 201, issued.text
    return issued.json()["data"]["plain_token"]


def _mcp_list_tools(context: SmokeContext, token: str) -> list[str]:
    if context.use_streamable_sdk:
        assert context.mcp_url is not None
        return anyio.run(_sdk_list_tools, context.mcp_url, token, context.mcp_verify)
    response = context.client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200, response.text
    return [tool["name"] for tool in response.json()["result"]["tools"]]


def _mcp_call(
    context: SmokeContext,
    token: str,
    name: str,
    arguments: dict,
) -> dict:
    if context.use_streamable_sdk:
        assert context.mcp_url is not None
        result = anyio.run(
            _sdk_call_tool,
            context.mcp_url,
            token,
            context.mcp_verify,
            name,
            arguments,
        )
        return result
    response = context.client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["result"]["structuredContent"]["envelope"][
        "result_status"
    ] == "succeeded"
    return payload["result"]["structuredContent"]


def _mcp_call_expect_denied(
    context: SmokeContext,
    token: str,
    name: str,
    arguments: dict,
    keyword: str,
) -> None:
    if context.use_streamable_sdk:
        assert context.mcp_url is not None
        try:
            result = anyio.run(
                _sdk_call_tool_raw,
                context.mcp_url,
                token,
                context.mcp_verify,
                name,
                arguments,
            )
        except Exception as exc:
            details = _exception_details(exc).lower()
            assert keyword.lower() in details, details
            return
        payload = result.model_dump(mode="json")
        assert result.isError is True
        assert keyword.lower() in str(payload).lower()
        return
    response = context.client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 403
    assert keyword.lower() in response.text.lower()


def _mcp_list_tools_expect_unauthorized(context: SmokeContext, token: str) -> None:
    if context.use_streamable_sdk:
        assert context.mcp_url is not None
        try:
            anyio.run(
                _sdk_list_tools,
                context.mcp_url,
                token,
                context.mcp_verify,
            )
        except Exception as exc:
            details = _exception_details(exc).lower()
            assert "unauthorized" in details or "401" in details, details
            return
        raise AssertionError("expected unauthorized SDK list_tools failure")
    denied = context.client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert denied.status_code == 401
    assert "unauthorized" in denied.text


async def _sdk_list_tools(mcp_url: str, token: str, verify: bool | str) -> list[str]:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        url=mcp_url,
        headers={"Authorization": f"Bearer {token}"},
        httpx_client_factory=_httpx_client_factory(verify),
    ) as (read_stream, write_stream, get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            assert get_session_id(), "SDK initialize did not establish Mcp-Session-Id"
            tools = await session.list_tools()
            assert get_session_id(), "SDK list_tools lost Mcp-Session-Id"
    return [tool.name for tool in tools.tools]


async def _sdk_call_tool(
    mcp_url: str,
    token: str,
    verify: bool | str,
    name: str,
    arguments: dict,
) -> dict:
    result = await _sdk_call_tool_raw(mcp_url, token, verify, name, arguments)
    assert result.isError is not True, result.model_dump(mode="json")
    assert result.structuredContent["envelope"]["result_status"] == "succeeded"
    return result.structuredContent


async def _sdk_call_tool_raw(
    mcp_url: str,
    token: str,
    verify: bool | str,
    name: str,
    arguments: dict,
):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        url=mcp_url,
        headers={"Authorization": f"Bearer {token}"},
        httpx_client_factory=_httpx_client_factory(verify),
    ) as (read_stream, write_stream, get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            assert get_session_id(), "SDK initialize did not establish Mcp-Session-Id"
            result = await session.call_tool(name, arguments)
            assert get_session_id(), "SDK call_tool lost Mcp-Session-Id"
            return result


def _httpx_client_factory(verify: bool | str):
    def _factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            verify=verify,
            event_hooks={"response": [_read_error_response_body]},
        )

    return _factory


async def _read_error_response_body(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        await response.aread()
    except Exception as exc:
        response.extensions["remote_smoke_error_body_read_failure"] = (
            f"{type(exc).__name__}: {exc}"
        )


def _exception_details(exc: BaseException) -> str:
    parts: list[str] = []
    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        parts.append(str(current))
        if isinstance(current, httpx.HTTPStatusError):
            parts.append(str(current.response.status_code))
            parts.append(current.response.reason_phrase)
            parts.append(str(current.response.url))
            parts.append(_response_text_or_marker(current.response))
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if current.__context__ is not None:
            stack.append(current.__context__)
    return "\n".join(part for part in parts if part)


def _response_text_or_marker(response: httpx.Response) -> str:
    try:
        return response.text
    except Exception as exc:
        if marker := response.extensions.get("remote_smoke_error_body_read_failure"):
            return str(marker)
        return f"{type(exc).__name__}: {exc}"


def _issue_oauth_tokens(
    client: Any,
    *,
    workspace_id: str,
    scope: str,
    state: str,
) -> OAuthTokens:
    _DCR_BUCKET.clear()
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Phase 5 Remote Smoke",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": scope,
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    verifier = "v" * 64
    auth_payload = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": scope,
        "state": state,
        "workspace_id": workspace_id,
    }
    consent = client.get("/oauth/authorize", params=auth_payload)
    assert consent.status_code == 200, consent.text
    # P10.3 T3: the consent screen is a workspace CHECKLIST now. The GET's
    # `workspace_id` survives only as a preselect HINT; the POST must carry
    # the chosen set as repeatable `workspace_ids`, and an empty set is
    # refused outright ("workspace_ids is required").
    consent_form = {
        key: value
        for key, value in auth_payload.items()
        if key != "workspace_id"
    }
    authorized = client.post(
        "/oauth/authorize",
        data={**consent_form, "workspace_ids": workspace_id, "decision": "allow"},
        follow_redirects=False,
    )
    # 303, not 302: the consent POST is a form submission, so the client has
    # to switch to GET for the callback.
    assert authorized.status_code == 303, authorized.text
    code = parse_qs(urlparse(authorized.headers["location"]).query)["code"][0]
    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200, token_response.text
    payload = token_response.json()
    return OAuthTokens(
        client_id=client_id,
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
    )


def _agent_draft_args(workspace_id: str, slug: str) -> dict:
    return {
        "workspace_id": workspace_id,
        "category": "instruction",
        "title": slug.replace("-", " ").title(),
        "slug": slug,
        "body_markdown": f"# {slug}\n\nPhase 5 smoke draft.",
        "reason": "Phase 5 remote smoke",
    }


def _seed_foreign_workspace(page_service: PageService) -> tuple[str, str]:
    with page_service.session_factory.begin() as session:
        workspace = seed_default_workspace(
            session,
            slug="phase5-foreign",
            title="Phase 5 Foreign",
        )
    page = page_service.write_page(
        workspace_slug="phase5-foreign",
        slug="phase5-foreign-page",
        title="Phase 5 Foreign Page",
        body_md="# Phase 5 Foreign Page\n\nInvisible foreign page.",
        actor_kind="system",
        actor_id="phase5-smoke",
        actor_provenance="browser_session",
    )
    return str(workspace.id), str(page.id)


def _seed_viewer_restricted_workspace(page_service: PageService) -> tuple[str, str]:
    with page_service.session_factory.begin() as session:
        user = session.execute(select(User)).scalar()
        assert user is not None
        workspace = seed_default_workspace(
            session,
            slug="phase5-viewer",
            title="Phase 5 Viewer",
        )
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role="viewer",
        )
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/phase5-restricted",
            title="Phase 5 Restricted Source",
            trust_level="restricted",
            raw_text="# Restricted Library item\n\nrestricted Library item needle",
        )
        from backend.app.container import ServiceRegistry

        services = ServiceRegistry()
        artifact = ExtractionService(
            media_installer_factory=services.validated_media_installer_for_session
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        workspace_id = str(workspace.id)
        artifact_id = str(artifact.id)
    page = page_service.write_page(
        workspace_slug="phase5-viewer",
        slug="phase5-viewer-page",
        title="Phase 5 Viewer Page",
        body_md="# Phase 5 Viewer Page\n\nVisible viewer page.",
        actor_kind="system",
        actor_id="phase5-smoke",
        actor_provenance="browser_session",
    )
    assert page.id
    return workspace_id, artifact_id
