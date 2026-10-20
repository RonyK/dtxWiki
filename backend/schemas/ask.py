from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import RESTResponseBase
from backend.schemas.input_policy import InputRequest
from backend.schemas.read import GraphNeighborEntry, TreePathEntry


class AskSessionCreate(InputRequest):
    title: str | None = None
    default_topic_tags: list[str] = Field(default_factory=list)
    default_context_parent_tree_node_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")


class AskTurnCreate(InputRequest):
    user_message: str
    ttl_override_seconds: int | None = None

    model_config = ConfigDict(extra="forbid")


class AskSessionRef(BaseModel):
    id: UUID
    title: str
    started_at: datetime
    updated_at: datetime
    actor_kind: str
    title_user_edited: bool
    topic_tags: list[str]
    turn_count: int

    model_config = ConfigDict(extra="forbid")


class AskTurnDetail(BaseModel):
    id: UUID
    ask_session_id: UUID
    turn_no: int
    user_message: str | None
    assistant_message: str | None
    # Backend-only field (not surfaced in the frontend operations type); keep a
    # default so it stays optional in the generated schema.
    used_agent_input_bundle_id: UUID | None = None
    cited_context_page_ids: list[UUID]
    cited_library_artifact_ids: list[UUID]
    topic_tags: list[str]
    promoted_context_page_id: UUID | None
    context_ingress_proposal_ids: list[str]
    recommended_placement_tree_node_id: UUID | None
    placement_confidence: str | None
    promotion_state: str
    created_at: datetime
    # Backend-only field (not surfaced in the frontend operations type); keep a
    # default so it stays optional in the generated schema.
    tree_path: list[TreePathEntry] = Field(default_factory=list)
    retrieval_trace: list[dict[str, str]]
    tried_lanes: list[str] = Field(default_factory=list)
    fallback_disclosure: bool
    warning_codes: list[str] = Field(default_factory=list)
    graph_context: list[GraphNeighborEntry] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class AskSessionDetail(AskSessionRef):
    workspace_id: UUID
    ended_at: datetime | None
    actor_id: UUID | None
    default_context_parent_tree_node_id: UUID | None
    placement_tree_node_id: UUID
    # Backend-only field (not surfaced in the frontend operations type); keep a
    # default so it stays optional in the generated schema.
    tree_path: list[TreePathEntry] = Field(default_factory=list)
    turns: list[AskTurnDetail]


class AskSessionListData(BaseModel):
    items: list[AskSessionRef] = Field(default_factory=list)
    date_buckets: list[dict[str, object]] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int | None = None
    has_more: bool = False

    model_config = ConfigDict(extra="forbid")


class AskSessionListResponse(RESTResponseBase):
    data: AskSessionListData

    model_config = ConfigDict(extra="forbid")


class AskSessionDetailResponse(RESTResponseBase):
    data: AskSessionDetail

    model_config = ConfigDict(extra="forbid")


class AskTurnDetailResponse(RESTResponseBase):
    data: AskTurnDetail

    model_config = ConfigDict(extra="forbid")
