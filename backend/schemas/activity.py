from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.schemas.common import RESTResponseBase

ActivityLane = Literal["pending", "advisory", "history"]
ActivitySubTab = Literal["live", "history", "traces", "proposals"]
ActivityTimePeriod = Literal["today", "yesterday", "week", "month", "all"]
ActivityAffordance = Literal["restore_version", "undo_change", "mark_read", "open"]
ActivityEventKey = Literal[
    "context_page.draft.publish",
    "tree.node.move",
    "tree.node.rename",
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


class ActivityItemRead(BaseModel):
    id: UUID
    lane: ActivityLane
    event_key: ActivityEventKey | None
    event_args: dict[str, str]
    title: str
    body: str | None
    target_title: str | None = None
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


class ActivityNavigation(BaseModel):
    target_kind: str
    target_id: UUID
    title: str | None = None
    href: str

    model_config = ConfigDict(extra="forbid")


class ActivityFeedEvent(ActivityItemRead):
    operation: str = "log"
    result_status: str | None = None
    tool_name: str | None = None
    public_error_code: str | None = None
    duration_ms: int | None = None
    navigation: ActivityNavigation | None = None


class ActivityFeedEntry(BaseModel):
    entry_type: Literal["event", "read_group"]
    id: str
    event: ActivityFeedEvent
    event_count: int
    first_occurred_at: datetime
    last_occurred_at: datetime
    group_token: str | None = None

    model_config = ConfigDict(extra="forbid")


class ActivityFeedLaneCount(BaseModel):
    returned_entry_count: int
    represented_event_count: int
    # None means this capped overview has not established this lane's end.
    has_more: bool | None

    model_config = ConfigDict(extra="forbid")


class ActivityFeedData(BaseModel):
    entries: list[ActivityFeedEntry]
    next_cursor: str | None
    # Attention items in the current query, independent of grouping or preview.
    unread_count: int
    returned_entry_count: int
    represented_event_count: int
    has_more: bool
    lanes: dict[ActivityLane, ActivityFeedLaneCount]

    model_config = ConfigDict(extra="forbid")


class ActivityFeedResponse(RESTResponseBase):
    data: ActivityFeedData

    model_config = ConfigDict(extra="forbid")


class ActivityReadGroupData(BaseModel):
    group_id: str
    events: list[ActivityFeedEvent]
    event_count: int

    model_config = ConfigDict(extra="forbid")


class ActivityReadGroupResponse(RESTResponseBase):
    data: ActivityReadGroupData

    model_config = ConfigDict(extra="forbid")
