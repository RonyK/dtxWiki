import json
from collections import OrderedDict
from types import SimpleNamespace

from backend.api.mcp_http import (
    _SESSION_OWNER_CAP,
    _TERMINATED_SESSION_CAP,
    _mcp_session_capacity_error,
    _record_mcp_session_owner,
    _record_terminated_session_id,
    _validate_mcp_session_owner,
)
from backend.policy.auth import ActorContext


def _owner_actor() -> ActorContext:
    return ActorContext(actor_kind="user", actor_id="user-1")


def _owner_state() -> SimpleNamespace:
    return SimpleNamespace(
        mcp_session_owners={},
        mcp_terminated_session_ids=OrderedDict(),
    )


def _manager_with_sessions(session_ids) -> SimpleNamespace:
    return SimpleNamespace(
        _server_instances={session_id: object() for session_id in session_ids}
    )


def test_terminated_session_ledger_evicts_fifo():
    state = SimpleNamespace(mcp_terminated_session_ids=OrderedDict())

    for index in range(_TERMINATED_SESSION_CAP + 3):
        _record_terminated_session_id(state, f"session-{index}")

    ledger = state.mcp_terminated_session_ids
    assert len(ledger) == _TERMINATED_SESSION_CAP
    assert "session-0" not in ledger
    assert "session-1" not in ledger
    assert "session-2" not in ledger
    assert f"session-{_TERMINATED_SESSION_CAP + 2}" in ledger


def test_terminated_session_duplicate_moves_to_fifo_tail():
    state = SimpleNamespace(mcp_terminated_session_ids=OrderedDict())

    for index in range(_TERMINATED_SESSION_CAP):
        _record_terminated_session_id(state, f"session-{index}")
    _record_terminated_session_id(state, "session-0")
    _record_terminated_session_id(state, "session-new")

    ledger = state.mcp_terminated_session_ids
    assert "session-0" in ledger
    assert "session-1" not in ledger
    assert "session-new" in ledger


def test_session_owner_cap_full_of_live_sessions_rejects_new_entry():
    state = _owner_state()
    actor = _owner_actor()
    live_ids = [f"live-{index}" for index in range(_SESSION_OWNER_CAP)]
    manager = _manager_with_sessions([*live_ids, "fresh"])

    for session_id in live_ids:
        _record_mcp_session_owner(state, session_id, actor, "fp", manager=manager)
    _record_mcp_session_owner(state, "fresh", actor, "fp", manager=manager)

    owners = state.mcp_session_owners
    assert len(owners) == _SESSION_OWNER_CAP
    assert "fresh" not in owners
    assert "live-0" in owners
    assert f"live-{_SESSION_OWNER_CAP - 1}" in owners


def test_session_owner_cap_evicts_dead_sessions_before_live():
    state = _owner_state()
    actor = _owner_actor()
    dead_ids = [f"dead-{index}" for index in range(3)]
    live_ids = [f"live-{index}" for index in range(_SESSION_OWNER_CAP - 3)]
    manager = _manager_with_sessions([*live_ids, "fresh"])

    for session_id in [*dead_ids, *live_ids]:
        _record_mcp_session_owner(state, session_id, actor, "fp", manager=manager)
    _record_mcp_session_owner(state, "fresh", actor, "fp", manager=manager)

    owners = state.mcp_session_owners
    assert len(owners) == _SESSION_OWNER_CAP - 2
    assert "fresh" in owners
    assert "live-0" in owners
    for session_id in dead_ids:
        assert session_id not in owners


def test_session_owner_cap_prunes_terminated_sessions_without_manager():
    state = _owner_state()
    actor = _owner_actor()

    for index in range(_SESSION_OWNER_CAP):
        _record_mcp_session_owner(state, f"live-{index}", actor, "fp", manager=None)
    _record_terminated_session_id(state, "live-0")
    _record_mcp_session_owner(state, "fresh", actor, "fp", manager=None)

    owners = state.mcp_session_owners
    assert len(owners) == _SESSION_OWNER_CAP
    assert "live-0" not in owners
    assert "fresh" in owners
    assert "live-1" in owners


def test_session_capacity_error_only_when_full_of_live_sessions():
    state = _owner_state()
    actor = _owner_actor()
    live_ids = [f"live-{index}" for index in range(_SESSION_OWNER_CAP - 1)]
    manager = _manager_with_sessions(live_ids)

    for session_id in live_ids:
        _record_mcp_session_owner(state, session_id, actor, "fp", manager=manager)
    assert _mcp_session_capacity_error(state, manager, None) is None

    _record_mcp_session_owner(state, "dead", actor, "fp", manager=manager)
    assert _mcp_session_capacity_error(state, manager, None) is None
    assert "dead" not in state.mcp_session_owners

    manager_full = _manager_with_sessions([*live_ids, "last-live"])
    _record_mcp_session_owner(state, "last-live", actor, "fp", manager=manager_full)
    response = _mcp_session_capacity_error(state, manager_full, None)
    assert response is not None
    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["error"]["data"]["error"] == "session_capacity_exhausted"
    assert len(state.mcp_session_owners) == _SESSION_OWNER_CAP


def test_oauth_refresh_can_recover_same_mcp_session_by_stable_grant():
    from uuid import uuid4

    state = _owner_state()
    grant_id = uuid4()
    actor_before = ActorContext(
        actor_kind="human",
        actor_id="user-1",
        user_id=uuid4(),
        token_kind="oauth_jwt",
        token_id=grant_id,
    )
    actor_after = ActorContext(
        actor_kind="human",
        actor_id=actor_before.actor_id,
        user_id=actor_before.user_id,
        token_kind="oauth_jwt",
        token_id=grant_id,
    )
    _record_mcp_session_owner(state, "session-1", actor_before, "old-bearer")

    assert (
        _validate_mcp_session_owner(
            state, "session-1", actor_after, "refreshed-bearer"
        )
        is None
    )

    different_grant = ActorContext(
        actor_kind="human",
        actor_id=actor_before.actor_id,
        user_id=actor_before.user_id,
        token_kind="oauth_jwt",
        token_id=uuid4(),
    )
    mismatch = _validate_mcp_session_owner(
        state, "session-1", different_grant, "other-bearer"
    )
    assert mismatch is not None
    assert mismatch.status_code == 403
