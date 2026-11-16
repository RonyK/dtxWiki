"""MCP review-surface schemas.

``list_requested_changes`` returns the change requests routed to the
  caller's effective principal P (its conversation owner), with the fix
instruction and evidence, projected from ``review_service.list_reviews``.
The retained revision result supports historical service projections only.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import MCPResultEnvelope
from backend.schemas.context_write import (
    ReviewEvidenceOperation,
)

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


class SubmitRevisionForReviewResult(BaseModel):
    original_review_item_id: UUID
    new_change_set_id: UUID
    new_review_item_id: UUID
    new_review_status: str
    deduplicated: bool = False
    replayed: bool = False

    model_config = ConfigDict(extra="forbid")
