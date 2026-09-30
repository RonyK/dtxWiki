"""Registry coverage and helper unit tests for ``backend.policy.rest_scopes``.

Ensures that every bearer-accessible ``/api/v1/**`` route in the running app
is either registered in ``REST_REQUIRED_SCOPES`` or covered by
``SCOPE_EXEMPT_PREFIXES``. A drift between the registry and the route table
should fail the build, preventing newly-added endpoints from defaulting to
either allow-all or deny-all without explicit intent.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

import backend.policy.rest_scopes as rest_scopes
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.policy.rest_scopes import (
    REST_REQUIRED_SCOPES,
    REST_SESSION_ONLY_ROUTES,
    SCOPE_EXEMPT_PREFIXES,
    _is_exempt,
    iter_app_bearer_routes,
)
from backend.spi.types import ActorContext


def _build_app():
    return create_app(
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="test-session-secret",
        ),
    )


def test_every_bearer_route_is_registered_or_exempt():
    app = _build_app()
    unknown: list[tuple[str, str]] = []
    for method, path in iter_app_bearer_routes(app):
        if _is_exempt(path):
            continue
        if (
            (method, path) not in REST_REQUIRED_SCOPES
            and (method, path) not in REST_SESSION_ONLY_ROUTES
        ):
            unknown.append((method, path))
    assert not unknown, (
        "REST routes missing from REST_REQUIRED_SCOPES (or exempt list): "
        f"{sorted(unknown)}"
    )


def test_registry_has_no_orphan_entries():
    """Entries in the registry should correspond to actual routes."""
    app = _build_app()
    app_routes = set(iter_app_bearer_routes(app))
    orphans = [
        (method, path)
        for (method, path) in REST_REQUIRED_SCOPES
        if (method, path) not in app_routes
    ]
    assert not orphans, (
        "REST_REQUIRED_SCOPES has entries that do not match any app route: "
        f"{sorted(orphans)}"
    )


def test_session_only_routes_have_no_bearer_scope_entry():
    app = _build_app()
    app_routes = set(iter_app_bearer_routes(app))
    assert REST_SESSION_ONLY_ROUTES
    assert REST_SESSION_ONLY_ROUTES.issubset(app_routes)
    overlap = set(REST_REQUIRED_SCOPES).intersection(REST_SESSION_ONLY_ROUTES)
    assert not overlap, (
        "Routes cannot be both bearer-scoped and session-only: "
        f"{sorted(overlap)}"
    )


def test_session_only_bearer_is_denied_before_workspace_target_scoping(
    monkeypatch,
):
    """A bearer DELETE must not reveal anything about its workspace target."""

    target_scoping_called = False

    def unexpected_target_scoping(_request, _actor):
        nonlocal target_scoping_called
        target_scoping_called = True
        raise AssertionError("session-only bearer request reached target scoping")

    monkeypatch.setattr(
        rest_scopes,
        "scope_actor_to_request_workspace",
        unexpected_target_scoping,
    )
    route_path = "/api/v1/workspaces/{workspace_id}"
    request = Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": "/api/v1/workspaces/not-a-uuid",
            "headers": [],
            "path_params": {"workspace_id": "not-a-uuid"},
            "route": SimpleNamespace(path=route_path),
        }
    )
    actor = ActorContext(
        actor_kind="human",
        actor_id="legacy-workspace-admin-pat",
        scopes=("workspace-admin",),
        token_kind="api_token",
        provenance="personal_access_token",
    )

    with pytest.raises(HTTPException) as exc_info:
        rest_scopes.require_token_scope(request, actor)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {
        "error_code": "scope_denied",
        "message": f"REST endpoint requires a user session: DELETE {route_path}",
    }
    assert target_scoping_called is False


def test_lint_format_is_editor_gated_on_both_auth_layers():
    # I15 #4: /lint/format auth is split across two layers (bearer scope registry
    # + the read router's session role check). Pin BOTH so a future change can't
    # silently turn the stateless formatter into a viewer-writable route.
    from backend.api.read import required_role_for_request

    fmt = ("POST", "/api/v1/workspaces/{workspace_id}/lint/format")
    assert REST_REQUIRED_SCOPES[fmt] == ("draft-write",)  # bearer scope floor

    class _Req:
        # Only the routed ASGI path: authorization must never consult the
        # Host-derived request.url (see test_authorization_path.py).
        def __init__(self, method: str, path: str) -> None:
            self.method = method
            self.scope = {"path": path}

    base = "/api/v1/workspaces/ws/"
    assert required_role_for_request(_Req("POST", base + "lint/format")) == "editor"
    # the only viewer-writable POST exceptions are search + feedback
    assert required_role_for_request(_Req("POST", base + "search")) == "viewer"
    assert required_role_for_request(_Req("GET", base + "lint/rules")) == "viewer"


def test_lint_batch_decision_requires_draft_write_scope():
    batch = (
        "POST",
        "/api/v1/workspaces/{workspace_id}/lint/findings/batch/decision",
    )
    assert REST_REQUIRED_SCOPES[batch] == ("draft-write",)


def test_exempt_prefixes_match_expected_surface():
    assert SCOPE_EXEMPT_PREFIXES == (
        "/api/v1/auth",
        "/api/v1/admin",
        "/api/v1/public",
        "/oauth",
        "/.well-known",
        "/mcp",
    )


def test_is_exempt_matches_prefix_paths():
    assert _is_exempt("/api/v1/auth")
    assert _is_exempt("/api/v1/auth/login")
    # Instance server-admin surface is session-only and scope-exempt.
    assert _is_exempt("/api/v1/admin")
    assert _is_exempt("/api/v1/admin/deployment")
    # T16: the unauthenticated public read surface is scope-exempt.
    assert _is_exempt("/api/v1/public")
    assert _is_exempt("/api/v1/public/w/acme/pages/intro")
    assert _is_exempt("/oauth/token")
    assert _is_exempt("/.well-known/oauth-protected-resource")
    assert _is_exempt("/mcp")
    assert not _is_exempt("/api/v1/pages")
    assert not _is_exempt("/api/v1/workspaces")
    # Prefix-only match must not be a partial substring match.
    assert not _is_exempt("/api/v1/authentication")
