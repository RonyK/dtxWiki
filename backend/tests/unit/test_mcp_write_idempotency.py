from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.mcp_trace import MCPWriteIdempotency
from backend.models.workspace import Workspace
from backend.schemas.context_write import UpdateDraftResult
from backend.services.mcp_write_idempotency import (
    IDEMPOTENCY_WINDOW,
    execute_idempotent_write,
)
from backend.services.read_errors import OperationOutcomeUnknownError


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


def test_retry_only_without_a_key_is_a_contract_violation() -> None:
    with pytest.raises(ValueError, match="retry_only requires an idempotency_key"):
        execute_idempotent_write(
            None,  # type: ignore[arg-type]
            workspace_id=None,  # type: ignore[arg-type]
            actor_kind="human",
            actor_id="retry-only-no-key-actor",
            tool_name="patch_draft",
            idempotency_key=None,
            fingerprint="irrelevant",
            result_model=_Result,
            execute=lambda: _Result(value="must-not-run"),
            retry_only=True,
        )


def test_retry_only_with_no_receipt_answers_outcome_unknown_without_executing(
    session_factory,
) -> None:
    """P8.5: a caller unsure whether its own prior send landed must get a
    read-only answer, never a fresh execution of the command it is asking
    about."""

    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )

    execute_count = 0

    def _must_not_execute() -> _Result:
        nonlocal execute_count
        execute_count += 1
        return _Result(value="should-not-happen")

    with session_factory.begin() as session:
        with pytest.raises(OperationOutcomeUnknownError):
            execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind="human",
                actor_id="retry-only-no-receipt-actor",
                tool_name="patch_draft",
                idempotency_key="never-sent-key",
                fingerprint="irrelevant",
                result_model=_Result,
                execute=_must_not_execute,
                retry_only=True,
            )

    assert execute_count == 0


def test_retry_only_with_an_expired_receipt_preserves_the_row(
    session_factory,
    monkeypatch,
) -> None:
    """P8.5: an expired receipt must NOT be read as "no receipt ever
    existed" and silently reclaimed as license to execute -- `retry_only`
    leaves the expired row in place for the real (non-retry_only) resend to
    reclaim, and answers unknown instead of guessing."""

    # Judge the receipt against the same "now" it is backdated from; see
    # test_expired_key_is_reclaimed_as_a_new_intent.
    now = utc_now()
    monkeypatch.setattr("backend.services.mcp_write_idempotency.utc_now", lambda: now)
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        expired = MCPWriteIdempotency(
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id="retry-only-expired-actor",
            tool_name="patch_draft",
            idempotency_key="retry-only-expired-key",
            request_fingerprint="expired-fingerprint",
            result_payload={"value": "expired-result"},
            expires_at=now - timedelta(seconds=1),
        )
        session.add(expired)
        session.flush()
        expired_id = expired.id

    execute_count = 0

    def _must_not_execute() -> _Result:
        nonlocal execute_count
        execute_count += 1
        return _Result(value="should-not-happen")

    with session_factory.begin() as session:
        with pytest.raises(OperationOutcomeUnknownError):
            execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind="human",
                actor_id="retry-only-expired-actor",
                tool_name="patch_draft",
                idempotency_key="retry-only-expired-key",
                fingerprint="irrelevant",
                result_model=_Result,
                execute=_must_not_execute,
                retry_only=True,
            )

    assert execute_count == 0
    with session_factory() as session:
        still_there = session.get(MCPWriteIdempotency, expired_id)
        assert still_there is not None
        assert still_there.result_payload == {"value": "expired-result"}


def test_retry_only_with_a_valid_receipt_replays_without_executing(
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
                actor_id="retry-only-valid-actor",
                tool_name="patch_draft",
                idempotency_key="retry-only-valid-key",
                request_fingerprint="valid-fingerprint",
                result_payload={"value": "already-landed"},
                expires_at=utc_now() + IDEMPOTENCY_WINDOW,
            )
        )

    execute_count = 0

    def _must_not_execute() -> _Result:
        nonlocal execute_count
        execute_count += 1
        return _Result(value="should-not-happen")

    with session_factory.begin() as session:
        replay = execute_idempotent_write(
            session,
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id="retry-only-valid-actor",
            tool_name="patch_draft",
            idempotency_key="retry-only-valid-key",
            fingerprint="valid-fingerprint",
            result_model=_Result,
            execute=_must_not_execute,
            retry_only=True,
        )

    assert replay == _Result(value="already-landed")
    assert execute_count == 0


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
