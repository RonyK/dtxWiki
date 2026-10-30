"""MCP review-surface tool schemas (item 11-c / T15b).

Two AI-facing tools:
- ``list_requested_changes`` (read-only): the change-requests routed to the
  caller's effective principal P (its conversation owner), with the fix
  instruction + evidence, projected from ``review_service.list_reviews``.
- ``submit_revision_for_review`` (write, non-destructive): the AI submits a
  revised draft for an open request (state-machine transition T6). It returns
  the item to ``pending`` for a HUMAN to approve — the AI can never approve
  (Risk-6, enforced structurally: no approve-capable MCP tool exists).
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.common import MCPResultEnvelope
from backend.schemas.context_write import (
    ReviewEvidenceOperation,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.schemas.input_policy import InputRequest

CONVERSATION_TOKEN_FIELD = Field(
    default=None,
    min_length=1,
    max_length=160,
    description=(
        "Opaque MCP conversation token. Omit or pass 'new' to start a new "
        "dtxWiki MCP chat; echo the returned envelope value to append."
    ),
)


class ListRequestedChangesInput(BaseModel):
    # The principal P is inferred server-side from the caller's conversation
    # owner and is NEVER passed in the payload (mirrors list_reviews deriving
    # audience_for from auth — Risk 2 / F1).
    workspace_id: UUID
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class RequestedChangeTarget(BaseModel):
    target_type: str
    target_id: UUID
    slug: str | None = None
    title: str | None = None
    stable_url: str | None = None
    link_markdown: str | None = None

    model_config = ConfigDict(extra="forbid")


class RequestedChangeEvidence(BaseModel):
    operations: list[ReviewEvidenceOperation] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class RequestedChangeClaim(BaseModel):
    claimed: bool
    claimed_by_me: bool
    claimed_at: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class RequestedChangeItem(BaseModel):
    review_item_id: UUID
    change_set_id: UUID
    # NULL audience (legacy / omitted request_changes) renders as a broadcast.
    audience_kind: str | None = None
    reason: str | None = None
    target: RequestedChangeTarget | None = None
    evidence: RequestedChangeEvidence | None = None
    claim: RequestedChangeClaim
    latest_decision_at: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class ListRequestedChangesPayload(BaseModel):
    items: list[RequestedChangeItem] = Field(default_factory=list)
    next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class ListRequestedChangesOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ListRequestedChangesPayload | None = None

    model_config = ConfigDict(extra="forbid")


class MCPRevisionUpdateRequest(UpdateDraftRequest):
    # Submit-revision dispatches through the same context write service.
    on_stale: Literal["conflict"] = "conflict"

    model_config = ConfigDict(extra="forbid")


class SubmitRevisionForReviewInput(InputRequest):
    workspace_id: UUID
    review_item_id: UUID
    # EXACTLY ONE revision source. `update` revises an existing page version
    # (preferred: carries base_version_id/base_content_hash stale-write
    # protection); `create` writes a fresh draft.
    update: MCPRevisionUpdateRequest | None = None
    create: WriteDraftRequest | None = None
    note: str | None = Field(default=None, max_length=2000)
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "SubmitRevisionForReviewInput":
        if (self.update is None) == (self.create is None):
            raise ValueError("exactly one of 'update' or 'create' is required")
        return self


class SubmitRevisionForReviewResult(BaseModel):
    original_review_item_id: UUID
    new_change_set_id: UUID
    new_review_item_id: UUID
    new_review_status: str
    # True when an idempotent retry (same principal + same content) returned the
    # prior result instead of creating a duplicate draft.
    deduplicated: bool = False

    model_config = ConfigDict(extra="forbid")


class SubmitRevisionForReviewOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: SubmitRevisionForReviewResult | None = None

    model_config = ConfigDict(extra="forbid")
