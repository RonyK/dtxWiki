from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from backend.models.coverage import KnowledgeCoverageGap
from backend.schemas.read import RankedResult, SearchPayload
from backend.tests.integration._cross_transport_helpers import (
    prepare_cross_transport_scenario,
)


def test_rest_transports_pass_target_workspace_role_to_search_service(
    page_service,
    monkeypatch,
):
    client, scenario = prepare_cross_transport_scenario(page_service)
    workspace_id = scenario.target_workspace_id
    observed: list[tuple[str, str, str]] = []

    def fake_search(**kwargs):
        actor = kwargs["actor"]
        observed.append(
            (actor.token_kind, actor.workspace_role, str(actor.workspace_id))
        )
        return SearchPayload(
            ranked_results=[],
            supported_retrieval_modes=["keyword"],
            permission_note="actor invariant captured",
            next_cursor=None,
        )

    # The REST search endpoint resolves its SearchService from app.state (per-app
    # DI), so spy there rather than on a removed module global (merged I2).
    monkeypatch.setattr(
        client.app.state,
        "search_service",
        SimpleNamespace(search=fake_search),
        raising=False,
    )

    calls = [
        ("session", {}),
        ("oauth_jwt", {"Authorization": f"Bearer {scenario.oauth_access_token}"}),
        ("api_token", {"Authorization": f"Bearer {scenario.personal_api_token}"}),
    ]
    for expected_token_kind, headers in calls:
        if headers:
            client.cookies.clear()
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/search",
            headers=headers,
            json={"query": "needle cross transport restricted", "scope": "all"},
        )
        assert response.status_code == 200, response.text
        assert observed[-1] == (
            expected_token_kind,
            "viewer",
            str(workspace_id),
        )


def test_mcp_http_passes_target_workspace_role_to_search_service(
    page_service,
    monkeypatch,
):
    client, scenario = prepare_cross_transport_scenario(page_service)
    workspace_id = scenario.target_workspace_id
    observed: list[tuple[str, str, str]] = []

    def fake_search(**kwargs):
        actor = kwargs["actor"]
        observed.append(
            (actor.token_kind, actor.workspace_role, str(actor.workspace_id))
        )
        return SearchPayload(
            ranked_results=[],
            supported_retrieval_modes=["keyword"],
            permission_note="actor invariant captured",
            next_cursor=None,
        )

    # The MCP search tool uses the container-built SearchService carried by
    # the ToolCallContext; rebind it on the app's container (the single
    # overlay rebinding point — no module global remains to patch).
    client.app.state.container.search_service = SimpleNamespace(search=fake_search)
    client.app.state.search_service = SimpleNamespace(search=fake_search)
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
                    "query": "needle cross transport restricted",
                    "scope": "all",
                },
            },
        },
    )

    assert response.status_code == 200, response.text
    assert observed == [("oauth_jwt", "viewer", str(workspace_id))]


def test_rest_search_reconciles_first_page_hits_but_not_empty_cursor_pages(
    page_service,
):
    client, scenario = prepare_cross_transport_scenario(page_service)
    workspace_id = scenario.target_workspace_id
    resolved_query = "resolved coverage query"
    cursor_query = "empty continuation query"
    with page_service.session_factory.begin() as session:
        gap = KnowledgeCoverageGap(
            workspace_id=workspace_id,
            query_norm=resolved_query,
            surface="search_rest",
            tried_lanes=["entity", "keyword"],
            nearest_candidates=[],
            rewrite_attempts=[],
            status="open",
        )
        session.add(gap)
        session.flush()
        gap_id = gap.id

    def fake_search(**kwargs):
        if kwargs.get("cursor"):
            return SearchPayload(
                ranked_results=[],
                total_count=4,
                supported_retrieval_modes=["keyword"],
                permission_note=None,
                next_cursor=None,
            )
        return SearchPayload(
            ranked_results=[
                RankedResult(
                    id=uuid4(),
                    type="context_page",
                    title="Resolved result",
                    snippet="Evidence",
                    score=1.0,
                    citation_available=True,
                )
            ],
            total_count=1,
            supported_retrieval_modes=["keyword"],
            permission_note=None,
            next_cursor=None,
        )

    client.app.state.search_service = SimpleNamespace(search=fake_search)
    client.app.state.container.search_service = client.app.state.search_service

    resolved = client.post(
        f"/api/v1/workspaces/{workspace_id}/search",
        json={"query": resolved_query},
    )
    continuation = client.post(
        f"/api/v1/workspaces/{workspace_id}/search",
        json={"query": cursor_query, "cursor": "continuation"},
    )

    assert resolved.status_code == 200
    assert continuation.status_code == 200
    assert "coverage_gap_recorded" not in continuation.json()["data"]["warnings"]
    with page_service.session_factory() as session:
        assert session.get(KnowledgeCoverageGap, gap_id).status == "resolved"
        assert (
            session.scalar(
                select(KnowledgeCoverageGap).where(
                    KnowledgeCoverageGap.workspace_id == workspace_id,
                    KnowledgeCoverageGap.query_norm == cursor_query,
                )
            )
            is None
        )


def test_oauth_lifecycle_path_symmetry_invariant():
    """Fix-6 Phase 6 (path-symmetry regression): document the OAuth token
    lifecycle invariants both token-issuing paths must enforce.

    Both `exchange_authorization_code` and `refresh_access_token` MUST call
    `_validate_token_issuance` before `_issue_tokens`. If only one path
    calls it, a future hardening change can drift between the two and
    silently re-introduce the H-F5-CODEX-2 attack surface.

    This test is a static-style assertion against the shared helper: it
    reads the module source and confirms both call sites still reference
    `_validate_token_issuance`. Pre-fix the refresh path did not — that
    asymmetry was the bug.
    """
    import inspect

    from backend.services import oauth_service

    src = inspect.getsource(oauth_service)
    exchange_block_start = src.index("def exchange_authorization_code(")
    exchange_block_end = src.index("\n    def ", exchange_block_start + 1)
    exchange_block = src[exchange_block_start:exchange_block_end]
    assert "_validate_token_issuance(" in exchange_block, (
        "exchange_authorization_code must call _validate_token_issuance "
        "(invariant 1: consent existence + scope coverage + membership)"
    )

    # Refresh was split into a serialising wrapper plus the body that rotates,
    # so the span has to reach past the lock boundary: read from the public
    # entry point to the end of the locked implementation. The validation must
    # live somewhere in there, wherever that boundary happens to sit.
    refresh_block_start = src.index("def refresh_access_token(")
    locked_start = src.index("def _refresh_access_token_locked(", refresh_block_start)
    refresh_block_end = src.index("\n    def ", locked_start + 1)
    refresh_block = src[refresh_block_start:refresh_block_end]
    # Refresh needs the refusal REASON, not just a raise, so it calls the
    # shared core that _validate_token_issuance also wraps. Naming that core
    # here keeps the invariant honest: the two paths must not each own a copy
    # of the consent/scope/membership rules, which is how they drifted before.
    assert "token_issuance_refusal(" in refresh_block, (
        "the refresh path must go through the shared token-issuance check "
        "(invariant 2: same checks as exchange path — path-symmetry)"
    )
    assert "token_issuance_refusal(" in inspect.getsource(
        oauth_service._validate_token_issuance
    ), "_validate_token_issuance must delegate to the same shared check"


def test_mcp_router_identifier_consistency_path_symmetry():
    """Fix-6 Phase 6: identifier consistency check must apply both at
    `resolve_workspace_id_for_tool` (raw identifier resolution) AND at
    `resolve_target_actor` (dispatcher entry point that calls it).

    If only the resolver checked but the dispatcher swallowed the
    PolicyRejection, the bug from B-F5-CODEX-1 would silently re-emerge
    via the dispatcher's actor-only short-circuit.
    """
    import inspect

    from backend.mcp import router

    src = inspect.getsource(router)
    target_actor_block_start = src.index("def resolve_target_actor(")
    target_actor_block_end = src.index("\ndef ", target_actor_block_start + 1)
    target_actor_block = src[target_actor_block_start:target_actor_block_end]
    assert "isinstance(resolved, PolicyRejection)" in target_actor_block, (
        "resolve_target_actor must propagate PolicyRejection from "
        "resolve_workspace_id_for_tool — silent fallback re-opens "
        "B-F5-CODEX-1 (read_page identifier mismatch)"
    )

    dispatch_block_start = src.index("def dispatch(")
    dispatch_block_end = src.index("\ndef ", dispatch_block_start + 1)
    dispatch_block = src[dispatch_block_start:dispatch_block_end]
    assert "isinstance(scoped_actor, PolicyRejection)" in dispatch_block, (
        "dispatch must surface PolicyRejection error_code in the MCP "
        "envelope — path-symmetric with the resolver guard"
    )
