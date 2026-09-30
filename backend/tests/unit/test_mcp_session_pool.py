from collections.abc import Container
from pathlib import Path

from backend.mcp.protocol.session_pool import manager_has_session, manager_session_pool


class _FakeManager:
    def __init__(self, pool):
        self._server_instances = pool


def test_manager_session_pool_reads_pinned_sdk_shape():
    manager = _FakeManager({"session-1": object()})

    pool = manager_session_pool(manager)

    assert isinstance(pool, Container)
    assert manager_has_session(manager, "session-1") is True
    assert manager_has_session(manager, "missing") is False


def test_manager_session_pool_returns_unknown_when_sdk_shape_changes():
    manager = object()

    assert manager_session_pool(manager) is None
    assert manager_has_session(manager, "session-1") is None


def test_mcp_http_keeps_sdk_private_pool_access_in_adapter():
    route_source = Path("backend/api/mcp_http.py").read_text(encoding="utf-8")

    assert "_server_instances" not in route_source
