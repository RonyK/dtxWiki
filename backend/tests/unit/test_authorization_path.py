"""Authorization decisions ignore the Host-derived ``request.url`` (C23).

Starlette rebuilds ``request.url`` from the client's Host header, so
``Host: x/search?`` turned the path of any POST into ``/search`` and
``required_role_for_request`` lowered it to viewer (readiness finding M-3:
a viewer could create agent pages). These tests hand the functions a request
whose ``url`` is polluted regardless of the installed Starlette version, so
they keep failing for code that reads ``request.url`` even after Starlette
stops producing that URL itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from starlette.datastructures import URL
from starlette.requests import Request

from backend.api.oauth import next_url_from_request
from backend.api.read import required_role_for_request
from backend.policy.rest_scopes import _request_route_path

WORKSPACE_ID = uuid4()
AGENT_PAGES_PATH = f"/api/v1/workspaces/{WORKSPACE_ID}/agent-pages"


class _HostPollutedRequest(Request):
    """What Starlette 1.0 built for ``Host: x/search?``: a URL whose path is
    the attacker's, while the router matched the real ``scope["path"]``."""

    def __init__(self, scope, polluted_url: str) -> None:
        super().__init__(scope)
        self._polluted_url = URL(polluted_url)

    @property
    def url(self) -> URL:
        return self._polluted_url


def _scope(method: str, path: str, *, route_path: str | None = None, query=b""):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query,
        "headers": [(b"host", b"x/search?")],
        "scheme": "http",
        "server": ("127.0.0.1", 21080),
    }
    if route_path is not None:
        scope["route"] = SimpleNamespace(path=route_path)
    return scope


def test_host_header_cannot_lower_the_required_role():
    request = _HostPollutedRequest(
        _scope(
            "POST",
            AGENT_PAGES_PATH,
            route_path="/api/v1/workspaces/{workspace_id}/agent-pages",
        ),
        f"http://x/search?{AGENT_PAGES_PATH}",
    )
    assert request.url.path == "/search"

    assert required_role_for_request(request) == "editor"


def test_host_header_cannot_lower_the_role_before_a_route_is_bound():
    request = _HostPollutedRequest(
        _scope("POST", AGENT_PAGES_PATH),
        f"http://x/search?{AGENT_PAGES_PATH}",
    )

    assert required_role_for_request(request) == "editor"


def test_search_and_feedback_posts_stay_viewer_level():
    for suffix in ("search", "feedback"):
        template = f"/api/v1/workspaces/{{workspace_id}}/{suffix}"
        request = _HostPollutedRequest(
            _scope(
                "POST",
                f"/api/v1/workspaces/{WORKSPACE_ID}/{suffix}",
                route_path=template,
            ),
            "http://127.0.0.1/unrelated",
        )
        assert required_role_for_request(request) == "viewer"


def test_rest_scope_registry_lookup_ignores_the_host_derived_url():
    request = _HostPollutedRequest(
        _scope("POST", AGENT_PAGES_PATH),
        f"http://x/search?{AGENT_PAGES_PATH}",
    )

    assert _request_route_path(request) == AGENT_PAGES_PATH


def test_login_redirect_target_ignores_the_host_derived_url():
    request = _HostPollutedRequest(
        _scope("GET", "/oauth/authorize", query=b"client_id=abc&state=s"),
        "http://a//evil.example/oauth/authorize?client_id=abc&state=s",
    )
    assert request.url.path.startswith("//evil.example")

    assert next_url_from_request(request) == "/oauth/authorize?client_id=abc&state=s"
