from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import RESTResponseBase

ActivityLane = Literal["pending", "advisory", "history"]
ActivitySubTab = Literal["live", "history", "traces", "proposals"]
ActivityTimePeriod = Literal["today", "yesterday", "week", "month", "all"]
ActivityAffordance = Literal["restore_version", "undo_change", "mark_read", "open"]
ActivityEventKey = Literal[
    "page.create",
    "page.update",
    "page.patch_context_edit",
    "page.begin_context_edit",
    "page.submit_context_edit_for_review",
    "page.discard_context_edit",
    "page.autosave.draft",
    "page.autosave.overwrite",
    "page.restore_version",
    "page.publish",
    "page.unpublish",
    "page.preserve_alternative",
    "page.inline_comment",
    "context_page.draft.create",
    "context_page.draft.update",
    "context_page.draft.diverged",
    "context_page.draft.diverged_discarded",
    "context_page.draft.publish",
    "review.decision",
    "review.superseded_by_approved_publish",
    "review.superseded_by_rejected_candidate",
    "tree.node.move",
    "tree.node.rename",
    "tree.node.reorder",
    "tree.node.delete",
    "workspace.join_request.requested",
    "workspace.join_request.approved",
    "workspace.join_request.rejected",
]


class ActivityActor(BaseModel):
    actor_kind: str
    actor_id: str | None = None
    display_name: str
    is_current_user: bool = False
    # Set only for actor_kind="mcp_client": the human whose MCP access this
    # row ran under (MCPClient.issued_to_user_id), distinct from
    # display_name, which names the registered AI client itself. None for a
    # legacy client with no owner on record, or every other actor kind.
    credential_owner_display_name: str | None = None

    model_config = ConfigDict(extra="forbid")


class ActivityTarget(BaseModel):
    target_kind: str
    target_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")


class ActivityVersion(BaseModel):
    id: UUID
    number: int
    # A mutable edit must never be linked as the bytes of a past save.
    immutable: bool = False

    model_config = ConfigDict(extra="forbid")


class ActivityLocation(BaseModel):
    path: list[str] = Field(default_factory=list)
    provenance: Literal["recorded", "current", "unavailable", "root"]

    model_config = ConfigDict(extra="forbid")


class ActivityRename(BaseModel):
    before: str | None = None
    after: str | None = None

    model_config = ConfigDict(extra="forbid")


class ActivityMove(BaseModel):
    before: ActivityLocation
    after: ActivityLocation

    model_config = ConfigDict(extra="forbid")


class ActivityDetails(BaseModel):
    page_id: UUID
    version: ActivityVersion | None = None
    base_version: ActivityVersion | None = None
    previous_published_version: ActivityVersion | None = None
    restored_from_version: ActivityVersion | None = None
    editing_revision: int | None = None
    reason: str | None = None
    decision_type: Literal["approve", "reject", "request_changes"] | None = None
    rename: ActivityRename | None = None
    move: ActivityMove | None = None

    model_config = ConfigDict(extra="forbid")


class ActivityItemRead(BaseModel):
    id: UUID
    lane: ActivityLane
    event_key: ActivityEventKey | None
    event_args: dict[str, str]
    title: str
    body: str | None
    target_title: str | None = None
    details: ActivityDetails | None = None
    actor: ActivityActor | None
    target: ActivityTarget | None
    created_at: datetime
    review_item_id: UUID | None
    affordances: list[ActivityAffordance]

    model_config = ConfigDict(extra="forbid")


class ActivityListData(BaseModel):
    items: list[ActivityItemRead]
    next_cursor: str | None
    unread_count: int

    model_config = ConfigDict(extra="forbid")


class ActivityListResponse(RESTResponseBase):
    data: ActivityListData

    model_config = ConfigDict(extra="forbid")
