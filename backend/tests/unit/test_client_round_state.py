from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.services.client_round_state import (
    MAINTENANCE_STATE_KIND,
    RERANK_STATE_KIND,
    ClientRoundStateCodec,
    ClientRoundStateError,
)


def _codec(kind=MAINTENANCE_STATE_KIND, now=None):
    instant = now or datetime(2026, 7, 12, tzinfo=UTC)
    return ClientRoundStateCodec(
        secret="test-secret",
        kind=kind,
        ttl_seconds=900,
        now=lambda: instant,
    )


def test_client_round_state_round_trips_sorted_task_ids():
    workspace_id = uuid4()
    task_ids = [uuid4(), uuid4()]
    token = _codec().issue(workspace_id=workspace_id, task_ids=reversed(task_ids))

    claims = _codec().verify(token, workspace_id=workspace_id)

    assert claims.workspace_id == workspace_id
    assert claims.task_ids == tuple(sorted(task_ids, key=str))
    assert claims.expires_at - claims.issued_at == 900


@pytest.mark.parametrize("part", [0, 1])
def test_client_round_state_rejects_tampered_payload_or_signature(part):
    workspace_id = uuid4()
    token = _codec().issue(workspace_id=workspace_id, task_ids=[uuid4()])
    segments = token.split(".")
    segments[part] = segments[part][:-1] + ("A" if segments[part][-1] != "A" else "B")

    with pytest.raises(ClientRoundStateError, match="client_state_invalid"):
        _codec().verify(".".join(segments), workspace_id=workspace_id)


def test_client_round_state_rejects_expired_token():
    workspace_id = uuid4()
    issued_at = datetime(2026, 7, 12, tzinfo=UTC)
    token = _codec(now=issued_at).issue(workspace_id=workspace_id, task_ids=[])

    with pytest.raises(ClientRoundStateError, match="client_state_expired"):
        _codec(now=issued_at + timedelta(seconds=901)).verify(
            token,
            workspace_id=workspace_id,
        )


def test_client_round_state_rejects_cross_kind_and_workspace_tokens():
    workspace_id = uuid4()
    token = _codec().issue(workspace_id=workspace_id, task_ids=[uuid4()])

    with pytest.raises(ClientRoundStateError, match="client_state_invalid"):
        _codec(RERANK_STATE_KIND).verify(token, workspace_id=workspace_id)
    with pytest.raises(ClientRoundStateError, match="client_state_invalid"):
        _codec().verify(token, workspace_id=uuid4())
