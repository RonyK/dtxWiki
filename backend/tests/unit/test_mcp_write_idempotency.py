from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.mcp.operation_tools import KEYED_TOOL_COMMANDS
from backend.models.base import utc_now
from backend.models.mcp_trace import MCPToolCall, MCPWriteIdempotency
from backend.models.workspace import Workspace
from backend.schemas.context_write import UpdateDraftResult
from backend.services.mcp_operation_attempt import (
    lookup_operation_status,
    start_operation_attempt,
)
from backend.services.mcp_write_idempotency import (
    IDEMPOTENCY_WINDOW,
    execute_idempotent_write,
)


class _Result(BaseModel):
    value: str


def test_legacy_landing_match_replays_but_is_not_reemitted() -> None:
    result = UpdateDraftResult.model_validate(
        {
            "diff_summary": "legacy replay",
            "operation_receipts": [
                {
                    "op": "insert_text",
                    "outcome": "already_landed",
                    "landing_match": "raw_adjacent",
                }
            ],
        }
    )

    assert result.operation_receipts is not None
    assert result.operation_receipts[0].outcome == "already_landed"
    assert "landing_match" not in result.model_dump()["operation_receipts"][0]


def test_concurrent_miss_loser_replays_claim_winner_without_executing(
    session_factory, monkeypatch
) -> None:
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        session.add(
            MCPWriteIdempotency(
                workspace_id=workspace_id,
                actor_kind="human",
                actor_id="idempotency-race-actor",
                tool_name="patch_draft",
                idempotency_key="same-key-race",
                request_fingerprint="same-fingerprint",
                result_payload={"value": "winner"},
                expires_at=utc_now() + IDEMPOTENCY_WINDOW,
            )
        )

    real_scalar = Session.scalar
    lookup_count = 0

    def _blind_first_lookup(self, statement, *args, **kwargs):
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 1:
            # Model a PostgreSQL concurrent miss: the winner was invisible at
            # the first SELECT but committed before this transaction's claim.
            return None
        return real_scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", _blind_first_lookup)
    execute_count = 0

    def _must_not_execute() -> _Result:
        nonlocal execute_count
        execute_count += 1
        return _Result(value="loser")

    with session_factory.begin() as session:
        replay = execute_idempotent_write(
            session,
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id="idempotency-race-actor",
            tool_name="patch_draft",
            idempotency_key="same-key-race",
            fingerprint="same-fingerprint",
            result_model=_Result,
            execute=_must_not_execute,
            replay_transform=lambda result: result.model_copy(
                update={"value": f"replayed:{result.value}"}
            ),
        )

    assert replay == _Result(value="replayed:winner")
    assert execute_count == 0


def test_existing_hit_transforms_return_value_without_rewriting_stored_result(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        session.add(
            MCPWriteIdempotency(
                workspace_id=workspace_id,
                actor_kind="human",
                actor_id="idempotency-transform-actor",
                tool_name="patch_draft",
                idempotency_key="transform-key",
                request_fingerprint="transform-fingerprint",
                result_payload={"value": "committed"},
                expires_at=utc_now() + IDEMPOTENCY_WINDOW,
            )
        )

    with session_factory.begin() as session:
        replay = execute_idempotent_write(
            session,
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id="idempotency-transform-actor",
            tool_name="patch_draft",
            idempotency_key="transform-key",
            fingerprint="transform-fingerprint",
            result_model=_Result,
            execute=lambda: _Result(value="must-not-run"),
            replay_transform=lambda result: result.model_copy(
                update={"value": f"replayed:{result.value}"}
            ),
        )

    assert replay == _Result(value="replayed:committed")
    with session_factory() as session:
        stored = session.scalar(
            select(MCPWriteIdempotency).where(
                MCPWriteIdempotency.idempotency_key == "transform-key"
            )
        )
        assert stored is not None
        assert stored.result_payload == {"value": "committed"}


def test_expired_key_is_reclaimed_as_a_new_intent(session_factory, monkeypatch) -> None:
    # The receipt expired one second before "now", and the service must judge it
    # against that same "now": the host clock steps (NTP, a resumed VM), so
    # backdating against one read of it and deciding against another would
    # put the receipt on either side of its own deadline.
    now = utc_now()
    monkeypatch.setattr("backend.services.mcp_write_idempotency.utc_now", lambda: now)
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        expired = MCPWriteIdempotency(
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id="expired-idempotency-actor",
            tool_name="patch_draft",
            idempotency_key="expired-key",
            request_fingerprint="expired-fingerprint",
            result_payload={"value": "expired-result"},
            expires_at=now - timedelta(seconds=1),
        )
        session.add(expired)
        session.flush()
        expired_id = expired.id

    execute_count = 0

    def _execute_new_intent() -> _Result:
        nonlocal execute_count
        execute_count += 1
        return _Result(value="new-result")

    with session_factory.begin() as session:
        result = execute_idempotent_write(
            session,
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id="expired-idempotency-actor",
            tool_name="patch_draft",
            idempotency_key="expired-key",
            fingerprint="new-fingerprint",
            result_model=_Result,
            execute=_execute_new_intent,
        )

    assert result == _Result(value="new-result")
    assert execute_count == 1
    with session_factory() as session:
        rows = session.scalars(
            select(MCPWriteIdempotency).where(
                MCPWriteIdempotency.workspace_id == workspace_id,
                MCPWriteIdempotency.actor_id == "expired-idempotency-actor",
                MCPWriteIdempotency.tool_name == "patch_draft",
                MCPWriteIdempotency.idempotency_key == "expired-key",
            )
        ).all()
        assert len(rows) == 1
        replacement = rows[0]
        assert replacement.id != expired_id
        assert replacement.request_fingerprint == "new-fingerprint"
        assert replacement.result_payload == {"value": "new-result"}
        assert replacement.expires_at >= now + IDEMPOTENCY_WINDOW


def test_operation_lifecycle_keeps_arrival_separate_and_prefers_atomic_receipt(
    session_factory, monkeypatch, mcp_default_actor
):
    # One lifecycle traversal replaces the removed retry_only checks: status
    # starts from the arrival, a receipt proves a commit, a late worker cannot
    # touch a newer attempt, only a failure known to repeat asks for a fix, and
    # a key reused by another command reports the latest call.
    clock = [utc_now()]
    monkeypatch.setattr(
        "backend.services.mcp_operation_attempt.utc_now", lambda: clock[0]
    )
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    actor = mcp_default_actor(actor_id="operation-actor", scopes=("*",))
    scope = dict(
        workspace_id=workspace_id,
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        idempotency_key="operation-key",
    )
    arrival_scope = dict(
        workspace_id=workspace_id, actor=actor, idempotency_key="operation-key"
    )

    def status(**changes):
        with session_factory() as session:
            return lookup_operation_status(
                session, **{**scope, "commands": KEYED_TOOL_COMMANDS, **changes}
            )

    def receipt(tool_name, result):
        with session_factory.begin() as session:
            session.add(
                MCPWriteIdempotency(
                    **scope,
                    tool_name=tool_name,
                    request_fingerprint=f"{tool_name}-fixture",
                    result_payload=result,
                    created_at=clock[0],
                    expires_at=clock[0] + IDEMPOTENCY_WINDOW,
                )
            )

    def finalize(identity, **values):
        # What the final capture records on the arrival row.
        with session_factory.begin() as session:
            session.execute(
                update(MCPToolCall)
                .where(MCPToolCall.id == identity.id)
                .values(
                    completed_at=clock[0],
                    result_status="failed" if values.get("error_code") else "succeeded",
                    **values,
                )
            )

    # A receipt written outside MCP has no arrival and is never served.
    receipt("lint_fix_page", {"value": "browser"})
    unseen = status()
    assert (unseen.status, unseen.next_action) == ("not_seen", "resend_same_request")
    assert "once" in unseen.guidance and "24 hours" in unseen.guidance
    first = start_operation_attempt(
        session_factory, **arrival_scope, tool_name="patch_context_edit"
    )
    assert status().status == "in_progress"
    assert status(actor_id="another-actor").status == "not_seen"
    clock[0] += timedelta(seconds=121)
    interrupted = status()
    assert interrupted.status == "failed"
    assert interrupted.error["code"] == "operation_interrupted"
    assert interrupted.next_action == "resend_same_request"

    second = start_operation_attempt(
        session_factory, **arrival_scope, tool_name="patch_context_edit"
    )
    finalize(first, error_code="old-worker-error", retryable=False)
    assert status().status == "in_progress"
    finalize(
        second,
        error_code="validation_failed",
        result_summary="Fix the operation.",
        retryable=False,
    )
    assert status().next_action == "fix_request"
    clock[0] += timedelta(seconds=1)
    third = start_operation_attempt(
        session_factory, **arrival_scope, tool_name="patch_context_edit"
    )
    finalize(third, error_code="internal_error", retryable=None)
    unknown = status()
    assert (unknown.next_action, unknown.guidance) == (
        "resend_same_request",
        unseen.guidance,
    )
    clock[0] += timedelta(seconds=1)
    fourth = start_operation_attempt(
        session_factory, **arrival_scope, tool_name="patch_context_edit"
    )
    receipt("patch_context_edit", {"value": "saved"})
    # The worker has not finalized, yet the domain transaction proves success.
    committed = status()
    assert (committed.status, committed.next_action) == ("committed", "use_result")
    assert committed.receipt_payload == {"value": "saved"}
    clock[0] += timedelta(seconds=1)
    fifth = start_operation_attempt(
        session_factory, **arrival_scope, tool_name="discard_context_edit"
    )
    assert status().status == "in_progress"  # latest tool, not the prior patch
    receipt("discard_context_edit", {"discarded": True})
    finalize(fifth)
    assert status().receipt_payload == {"discarded": True}
    with session_factory() as session:
        rows = session.scalars(
            select(MCPToolCall)
            .where(MCPToolCall.idempotency_key == scope["idempotency_key"])
            .order_by(MCPToolCall.started_at, MCPToolCall.id)
        ).all()
        assert [row.id for row in rows] == [
            first.id,
            second.id,
            third.id,
            fourth.id,
            fifth.id,
        ]
        assert rows[0].error_code == "old-worker-error"
        assert rows[1].error_code == "validation_failed"
        assert rows[4].result_status == "succeeded"
        assert rows[3].result_detail is None
    clock[0] += IDEMPOTENCY_WINDOW + timedelta(seconds=1)
    assert status().status == "not_seen"
    with session_factory() as session:
        assert session.get(MCPToolCall, first.id) is not None
        assert session.get(MCPToolCall, fourth.id) is not None


def test_execution_failure_rolls_back_claim_and_partial_side_effect(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )

    with session_factory.begin() as session:

        def _fail_after_flush() -> _Result:
            session.add(
                MCPWriteIdempotency(
                    workspace_id=workspace_id,
                    actor_kind="human",
                    actor_id="partial-side-effect-actor",
                    tool_name="side_effect_probe",
                    idempotency_key="partial-side-effect",
                    request_fingerprint="partial-side-effect-fingerprint",
                    result_payload={"value": "must-roll-back"},
                    expires_at=utc_now() + IDEMPOTENCY_WINDOW,
                )
            )
            session.flush()
            raise RuntimeError("domain write failed")

        try:
            execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind="human",
                actor_id="atomic-claim-actor",
                tool_name="patch_draft",
                idempotency_key="atomic-claim",
                fingerprint="atomic-claim-fingerprint",
                result_model=_Result,
                execute=_fail_after_flush,
            )
        except RuntimeError as exc:
            assert str(exc) == "domain write failed"
        else:  # pragma: no cover - assertion branch
            raise AssertionError("execution failure must propagate")
        # Leaving this context commits the outer transaction intentionally.

    with session_factory() as session:
        rows = session.scalars(
            select(MCPWriteIdempotency).where(
                MCPWriteIdempotency.idempotency_key.in_(
                    ("atomic-claim", "partial-side-effect")
                )
            )
        ).all()
        assert rows == []


def test_upgrade_replays_legacy_fingerprint_and_landing_match_together(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        session.add(
            MCPWriteIdempotency(
                workspace_id=workspace_id,
                actor_kind="human",
                actor_id="legacy-patch-replay-actor",
                tool_name="patch_draft",
                idempotency_key="legacy-patch-replay-key",
                request_fingerprint="legacy-request-fingerprint",
                result_payload={
                    "diff_summary": "legacy patch replay",
                    "operation_receipts": [
                        {
                            "op": "insert_text",
                            "outcome": "already_landed",
                            "landing_match": "normalized_block",
                        }
                    ],
                },
                expires_at=utc_now() + IDEMPOTENCY_WINDOW,
            )
        )

    execute_count = 0

    def _must_not_execute() -> UpdateDraftResult:
        nonlocal execute_count
        execute_count += 1
        return UpdateDraftResult(diff_summary="duplicate execution")

    with session_factory.begin() as session:
        replay = execute_idempotent_write(
            session,
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id="legacy-patch-replay-actor",
            tool_name="patch_draft",
            idempotency_key="legacy-patch-replay-key",
            fingerprint="new-base-aware-fingerprint",
            compatible_fingerprints=("legacy-request-fingerprint",),
            result_model=UpdateDraftResult,
            execute=_must_not_execute,
        )

    assert execute_count == 0
    assert replay.operation_receipts is not None
    assert replay.operation_receipts[0].outcome == "already_landed"
    assert "landing_match" not in replay.model_dump()["operation_receipts"][0]
