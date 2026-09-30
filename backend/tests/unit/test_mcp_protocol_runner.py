from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.mcp.protocol.runner import _persist_trace
from backend.policy.auth import ActorContext


class _SessionFactory:
    @contextmanager
    def begin(self):
        yield object()


def test_correctness_trace_failure_can_be_fail_closed(monkeypatch) -> None:
    workspace_id = uuid4()
    actor = ActorContext(
        actor_kind="mcp_client",
        actor_id="read-ledger-test",
        workspace_id=workspace_id,
    )

    def _fail(**_kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(
        "backend.mcp.protocol.runner.persist_mcp_tool_trace",
        _fail,
    )
    common = {
        "session_factory": _SessionFactory(),
        "actor": actor,
        "name": "read_structure_map",
        "arguments": {"workspace_id": str(workspace_id)},
        "payload": {"envelope": {"result_status": "succeeded"}},
        "mcp_session_id": str(uuid4()),
        "started_at": datetime.now(UTC),
        "latency_ms": 1,
        "trace_workspace_id": workspace_id,
    }

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        _persist_trace(**common, fail_closed=True)

    # Ordinary telemetry retains the historical best-effort boundary.
    _persist_trace(**common, fail_closed=False)
