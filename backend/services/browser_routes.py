"""Registered workspace browser paths, without destination access decisions.

The router and authored-link readers share this grammar. A successful match
identifies a destination; its existing reader and route guards authorize it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote

IDENTITY_QUERY_KEYS = (
    "target",
    "edit",
    "review",
    "finding",
    "new",
    "newFolder",
    "parentNodeId",
    "parentId",
    "workspaceId",
    "workspace_id",
    "ws",
)
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BrowserRoute:
    path: str
    section: str
    kind: str = "overview"
    target_param: str | None = None
    create: bool = False
    edit: bool = False
    choices: tuple[tuple[str, tuple[str, ...]], ...] = ()


_WS = "/workspaces/:workspaceId"
BROWSER_ROUTES = {
    "workspace_directory": BrowserRoute("/workspaces/find", "workspaces"),
    "workspace_home": BrowserRoute(_WS, "dashboard"),
    "workspace_graph": BrowserRoute(f"{_WS}/graph", "graph"),
    "workspace_dashboard": BrowserRoute(f"{_WS}/dashboard", "dashboard"),
    "context": BrowserRoute(f"{_WS}/context", "context"),
    "context_folder_new": BrowserRoute(
        f"{_WS}/context/folders/new", "context", create=True
    ),
    "context_folder": BrowserRoute(
        f"{_WS}/context/folders/:nodeId", "context", "folder", "nodeId"
    ),
    "context_page_new": BrowserRoute(
        f"{_WS}/context/pages/new", "context", create=True
    ),
    "context_page": BrowserRoute(
        f"{_WS}/context/pages/:pageId", "context", "page", "pageId"
    ),
    "context_page_edit": BrowserRoute(
        f"{_WS}/context/pages/:pageId/edit", "context", "page", "pageId", edit=True
    ),
    "library": BrowserRoute(f"{_WS}/library", "library"),
    "library_item_new": BrowserRoute(
        f"{_WS}/library/items/new", "library", create=True
    ),
    "library_item": BrowserRoute(
        f"{_WS}/library/items/:itemId", "library", "item", "itemId"
    ),
    "library_artifact": BrowserRoute(
        f"{_WS}/library/artifacts/:artifactId", "library", "artifact", "artifactId"
    ),
    "library_folder_new": BrowserRoute(
        f"{_WS}/library/folders/new", "library", create=True
    ),
    "library_folder": BrowserRoute(
        f"{_WS}/library/folders/:nodeId", "library", "folder", "nodeId"
    ),
    "agent": BrowserRoute(f"{_WS}/agent", "agent"),
    "agent_page_new": BrowserRoute(f"{_WS}/agent/pages/new", "agent", create=True),
    "agent_page": BrowserRoute(
        f"{_WS}/agent/pages/:agentPageId", "agent", "page", "agentPageId"
    ),
    "agent_page_edit": BrowserRoute(
        f"{_WS}/agent/pages/:agentPageId/edit",
        "agent",
        "page_edit",
        "agentPageId",
        edit=True,
    ),
    "agent_folder_new": BrowserRoute(f"{_WS}/agent/folders/new", "agent", create=True),
    "agent_folder": BrowserRoute(
        f"{_WS}/agent/folders/:nodeId", "agent", "folder", "nodeId"
    ),
    "activity": BrowserRoute(f"{_WS}/activity", "activity"),
    "activity_lane": BrowserRoute(
        f"{_WS}/activity/:lane",
        "activity",
        choices=(("lane", ("pending", "advisory", "history")),),
    ),
    "review": BrowserRoute(f"{_WS}/review", "review"),
    "review_item": BrowserRoute(
        f"{_WS}/review/:reviewItemId", "review", "item", "reviewItemId"
    ),
    "lint": BrowserRoute(f"{_WS}/lint", "lint"),
    "lint_finding": BrowserRoute(
        f"{_WS}/lint/findings/:findingId", "lint", "item", "findingId"
    ),
    "chats": BrowserRoute(f"{_WS}/chats", "chats"),
    "chat": BrowserRoute(f"{_WS}/chats/:chatId", "chats", "item", "chatId"),
    "ask": BrowserRoute(f"{_WS}/ask", "ask"),
    "ask_session": BrowserRoute(
        f"{_WS}/ask/:askSessionId", "ask", "item", "askSessionId"
    ),
    "search": BrowserRoute(f"{_WS}/search", "search"),
    "settings": BrowserRoute(f"{_WS}/settings", "settings"),
    "settings_integrations": BrowserRoute(f"{_WS}/settings/integrations", "settings"),
    "setup": BrowserRoute(f"{_WS}/setup", "setup"),
    "inbox": BrowserRoute(f"{_WS}/inbox", "inbox"),
}


@dataclass(frozen=True)
class BrowserRouteMatch:
    route: BrowserRoute
    params: dict[str, str]


def match_browser_route(path: str) -> BrowserRouteMatch | None:
    """Match the authored path without dot-segment or slash normalization."""
    if re.search(r"[\\\x00-\x20\x7f]|%(?:2f|5c|2e|25|00)|%(?![0-9a-f]{2})", path, re.I):
        return None
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeError:
        return None
    if "//" in decoded:
        return None
    parts = decoded.removesuffix("/").split("/")
    for route in BROWSER_ROUTES.values():
        expected = route.path.split("/")
        if len(expected) != len(parts):
            continue
        params = {}
        choices = dict(route.choices)
        for pattern, value in zip(expected, parts, strict=True):
            if not pattern.startswith(":"):
                if not value.isascii() or value.lower() != pattern.lower():
                    break
            else:
                name = pattern[1:]
                if name in choices:
                    if value not in choices[name]:
                        break
                    params[name] = value
                elif _UUID.fullmatch(value):
                    params[name] = value.lower()
                else:
                    break
        else:
            return BrowserRouteMatch(route, params)
    return None


def safe_browser_query(query: str, path: str = "") -> bool:
    """Preserve presentation state while rejecting target identity overrides."""
    params = parse_qs(query, keep_blank_values=True)
    if set(IDENTITY_QUERY_KEYS) & params.keys():
        return False
    parents = params.get("parent", ())
    if not parents:
        return True
    match = match_browser_route(path)
    return bool(
        match
        and match.route.create
        and len(parents) == 1
        and _UUID.fullmatch(parents[0])
    )


def render_typescript() -> str:
    """Export immutable syntax data; frontend matching has no backend imports."""
    routes = {
        name: {
            "path": route.path,
            "section": route.section,
            "kind": route.kind,
            "targetParam": route.target_param,
            "create": route.create,
            "edit": route.edit,
            "choices": dict(route.choices),
        }
        for name, route in BROWSER_ROUTES.items()
    }
    identity_keys = json.dumps(IDENTITY_QUERY_KEYS)
    sections = json.dumps(
        list(
            dict.fromkeys(
                route.section
                for route in BROWSER_ROUTES.values()
                if ":workspaceId" in route.path
            )
        )
    )
    return (
        "// Generated by scripts/export_browser_routes.py; do not edit.\n"
        "// Route matching identifies destinations; existing guards authorize them.\n\n"
        f"export const BROWSER_ROUTES = {json.dumps(routes, indent=2)} as const;\n\n"
        f"export const IDENTITY_QUERY_KEYS = {identity_keys} as const;\n"
        f"export const WORKSPACE_SECTIONS = {sections} as const;\n"
    )
