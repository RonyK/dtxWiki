from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import RESTResponseBase

AGENT_PAGE_CATEGORIES = (
    "agent_guide",
    "persona",
    "skill",
    "memory",
)
SENSITIVE_AGENT_PAGE_CATEGORIES = {
    "agent_guide",
    "memory",
}


class AgentPageDraftRequest(BaseModel):
    category: Literal[
        "agent_guide",
        "persona",
        "skill",
        "memory",
    ]
    title: str
    slug: str
    body_markdown: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    base_version_id: UUID | None = None
    parent_node_id: UUID | None = None
    reason: str
    sensitivity: Literal["public", "workspace", "private", "secret_candidate"] = (
        "workspace"
    )

    model_config = ConfigDict(extra="forbid")


class AgentPageDraftResult(BaseModel):
    agent_page_id: UUID
    draft_version_id: UUID
    change_set_id: UUID
    review_item_id: UUID | None = None
    next_action: Literal["owner_activation_required", "review_required"]

    model_config = ConfigDict(extra="forbid")


class AgentPageDraftResultResponse(RESTResponseBase):
    data: AgentPageDraftResult

    model_config = ConfigDict(extra="forbid")


class AgentPageActivateRequest(BaseModel):
    version_id: UUID
    reason: str

    model_config = ConfigDict(extra="forbid")


class AgentPageRestoreRequest(BaseModel):
    version_id: UUID

    model_config = ConfigDict(extra="forbid")


class AgentPageActivateResult(BaseModel):
    agent_page_id: UUID
    active_version_id: UUID
    previous_active_version_id: UUID | None = None
    change_set_id: UUID
    audit_event_id: UUID

    model_config = ConfigDict(extra="forbid")


class BuildBundleRequest(BaseModel):
    task_intent: str
    client_id: UUID
    token_budget: int = Field(ge=512, le=200_000)
    ttl_override_seconds: int | None = None
    requested_categories: list[str] = Field(default_factory=list)
    context_refs: list[UUID] = Field(default_factory=list)
    library_artifact_refs: list[UUID] = Field(default_factory=list)
    permission_context: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class BundleItemEntry(BaseModel):
    source_kind: Literal[
        "agent_page",
        "context_page_excerpt",
        "library_artifact_excerpt",
    ]
    pinned_id: UUID
    category_or_type: str
    priority: int
    included_range: dict[str, Any] | None = None
    included_reason: str | None = None
    omission_reason: str | None = None

    model_config = ConfigDict(extra="forbid")


class BuildBundleResult(BaseModel):
    bundle_id: UUID
    status: Literal["generated", "used", "expired"]
    valid_until: datetime
    invalidation_keys: dict[str, Any]
    items: list[BundleItemEntry]

    model_config = ConfigDict(extra="forbid")


class BuildBundleResponse(RESTResponseBase):
    data: BuildBundleResult

    model_config = ConfigDict(extra="forbid")


class AgentPageBundleSummaryEntry(BaseModel):
    bundle_id: UUID
    status: Literal["generated", "used", "expired"]
    created_at: datetime
    valid_until: datetime
    used_at: datetime | None = None
    expired_at: datetime | None = None
    included: bool

    model_config = ConfigDict(extra="forbid")


class AgentPageBundleListResponse(RESTResponseBase):
    data: list[AgentPageBundleSummaryEntry]

    model_config = ConfigDict(extra="forbid")
