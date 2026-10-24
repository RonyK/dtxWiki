from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection
from datetime import timedelta
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.mcp_trace import MCPWriteIdempotency
from backend.schemas.edit_receipt import EditReceipt
from backend.schemas.page import PagePayload
from backend.services.db_write_intent import acquire_write_intent
from backend.services.read_errors import (
    IdempotencyKeyConflictError,
    OperationOutcomeUnknownError,
)

IDEMPOTENCY_WINDOW = timedelta(hours=24)
_PENDING_RESULT_PAYLOAD = {"_dtxwiki_idempotency_state": "pending"}


def request_fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def execute_idempotent_write[ResultT: BaseModel](
    session: Session,
    *,
    workspace_id: UUID,
    actor_kind: str,
    actor_id: str,
    tool_name: str,
    idempotency_key: str | None,
    fingerprint: str,
    compatible_fingerprints: Collection[str] = (),
    result_model: type[ResultT],
    execute: Callable[[], ResultT],
    replay_transform: Callable[[ResultT], ResultT] | None = None,
    retry_only: bool = False,
) -> ResultT:
    if idempotency_key is None:
        if retry_only:
            raise ValueError("retry_only requires an idempotency_key")
        return execute()

    # pysqlite does not emit a real transaction for a SELECT-only outer scope.
    # If the first DML happens below a SAVEPOINT, its implicit BEGIN IMMEDIATE
    # can discard that savepoint (and a nested domain writer then fails with
    # ``no such savepoint``). Establish SQLite's writer transaction before any
    # idempotency savepoint. PostgreSQL remains governed by the row/unique-key
    # locking below because this helper is a no-op there.
    acquire_write_intent(session)

    now = utc_now()
    scope = (
        MCPWriteIdempotency.workspace_id == workspace_id,
        MCPWriteIdempotency.actor_kind == actor_kind,
        MCPWriteIdempotency.actor_id == actor_id,
        MCPWriteIdempotency.tool_name == tool_name,
        MCPWriteIdempotency.idempotency_key == idempotency_key,
    )
    row = session.scalar(
        select(MCPWriteIdempotency).where(*scope).with_for_update()
    )
    if row is not None and row.expires_at > now:
        return _replay_result(
            row,
            fingerprint=fingerprint,
            compatible_fingerprints=compatible_fingerprints,
            result_model=result_model,
            replay_transform=replay_transform,
        )
    if retry_only:
        # No valid receipt: answer "unknown", never fall through to a claim +
        # execute below. Deleting an expired row here would be indistinguishable
        # from "no receipt ever existed", which is exactly the license-to-
        # execute reading this mode exists to refuse -- so an expired row is
        # left in place for the real (non-retry_only) resend to reclaim.
        raise OperationOutcomeUnknownError()
    if row is not None:
        session.execute(
            delete(MCPWriteIdempotency).where(MCPWriteIdempotency.id == row.id)
        )
        session.flush()

    # Claim the key before executing the side effect. A check-then-execute-then-
    # insert sequence lets two PostgreSQL READ COMMITTED transactions both miss
    # and both mutate content before the unique constraint selects a winner.
    # Here the losing INSERT waits for the winner and then replays its durable
    # result without calling execute(). The savepoint contains the claim and
    # domain write so a caught execution error cannot leave either durable.
    claim = MCPWriteIdempotency(
        workspace_id=workspace_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        tool_name=tool_name,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        result_payload=_PENDING_RESULT_PAYLOAD,
        expires_at=now + IDEMPOTENCY_WINDOW,
    )
    nested = session.begin_nested()
    try:
        session.add(claim)
        session.flush()
    except IntegrityError:
        nested.rollback()
        if claim in session:
            session.expunge(claim)
        winner = session.scalar(select(MCPWriteIdempotency).where(*scope))
        if winner is None:
            raise
        return _replay_result(
            winner,
            fingerprint=fingerprint,
            compatible_fingerprints=compatible_fingerprints,
            result_model=result_model,
            replay_transform=replay_transform,
        )

    try:
        # The claim, domain mutation, and durable result are one atomic unit.
        # A caller may legitimately catch an execution error and continue its
        # outer transaction; rolling back this savepoint prevents that outer
        # commit from preserving a visible pending claim or partial side effect.
        result = execute()
        payload = result.model_dump(mode="json")
        if isinstance(result, PagePayload) and result._edit_receipt is not None:
            # Keep command metadata internal; PagePayload's REST schema and
            # ordinary model serialization still omit this private receipt.
            payload["_edit_receipt"] = result._edit_receipt.model_dump(mode="json")
        claim.result_payload = payload
        session.flush()
    except BaseException:
        nested.rollback()
        raise
    nested.commit()
    return result


def _replay_result[ResultT: BaseModel](
    row: MCPWriteIdempotency,
    *,
    fingerprint: str,
    compatible_fingerprints: Collection[str] = (),
    result_model: type[ResultT],
    replay_transform: Callable[[ResultT], ResultT] | None = None,
) -> ResultT:
    if (
        row.request_fingerprint != fingerprint
        and row.request_fingerprint not in compatible_fingerprints
    ):
        raise IdempotencyKeyConflictError()
    if row.result_payload == _PENDING_RESULT_PAYLOAD:
        # A pending row must be uncommitted and therefore invisible to another
        # transaction. If one is ever observable, fail closed instead of
        # executing a second mutation against an indeterminate first attempt.
        raise RuntimeError("idempotency claim has no committed result")
    payload = dict(row.result_payload)
    receipt = payload.pop("_edit_receipt", None)
    result = result_model.model_validate(payload)
    if isinstance(result, PagePayload) and receipt is not None:
        result._edit_receipt = EditReceipt.model_validate(receipt)
    if replay_transform is not None:
        return replay_transform(result)
    return result
