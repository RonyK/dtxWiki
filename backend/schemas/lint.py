from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.common import RESTResponseBase
from backend.schemas.input_policy import InputRequest
from backend.schemas.page import BODY_MD_MAX_LENGTH

LintDecision = Literal["acknowledge", "dismiss", "fix"]
LintBatchDecision = Literal["acknowledge", "dismiss"]


class LintScope(BaseModel):
    kind: Literal["workspace", "maintenance", "targets"]
    targets: list[dict[str, str]] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class LintFindingRead(BaseModel):
    id: UUID
    lint_run_id: UUID
    workspace_id: UUID
    target_type: str
    target_id: UUID | None = None
    rule_id: str
    severity: str
    status: str
    suggested_action: str | None = None
    message_key: str | None = None
    message_args: dict[str, Any] = Field(default_factory=dict)
    target_title: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class LintRunRead(BaseModel):
    id: UUID
    workspace_id: UUID
    lint_scope: LintScope
    trigger_type: str
    status: str
    finding_count_by_severity: dict = Field(
        description=(
            "Severity tally for this run. For a completed run this is normally"
            " the aggregate persisted at run-completion time (identical for"
            " every reader) -- EXCEPT when the requesting actor cannot read"
            " drafts (LEAK-01): that reader gets a live recount, excluding"
            " findings produced only by a draft body, via the same predicate"
            " list_findings uses. The stored value itself is never mutated, so"
            " the same run can legitimately render different totals to"
            " different actors, and a draft-reading actor's later read can"
            " differ from an earlier restricted read if drafts changed or"
            " findings were resolved in between -- neither is corruption."
        ),
    )
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class LintFindingListData(BaseModel):
    findings: list[LintFindingRead] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int | None = None
    has_more: bool = False
    # Numeric pagination is additive: legacy cursor callers keep using the
    # fields above, while page/page_size callers receive exact board-style
    # metadata. Null values identify a legacy cursor response unambiguously.
    page: int | None = None
    page_size: int | None = None
    page_count: int | None = None
    has_previous: bool | None = None
    has_next: bool | None = None

    model_config = ConfigDict(extra="forbid")


class LintRunCreateRequest(BaseModel):
    # Only workspace-wide runs are supported today; reject other scopes at the
    # boundary (J3) instead of silently linting the whole workspace. Add a member
    # here when a narrower scope (e.g. a single page) is implemented.
    lint_scope: Literal["workspace"] = "workspace"
    trigger_type: str = "manual"

    model_config = ConfigDict(extra="forbid")


class LintFindingDecisionRequest(InputRequest):
    decision: LintDecision
    reason: str | None = None

    model_config = ConfigDict(extra="forbid")


class LintFindingBatchDecisionRequest(InputRequest):
    finding_ids: list[UUID] = Field(min_length=1, max_length=100)
    decision: LintBatchDecision
    reason: str | None = None

    @model_validator(mode="after")
    def validate_unique(self) -> "LintFindingBatchDecisionRequest":
        if len(set(self.finding_ids)) != len(self.finding_ids):
            raise ValueError("duplicate finding_ids are not allowed")
        return self

    model_config = ConfigDict(extra="forbid")


class LintFindingBatchDecisionFailure(BaseModel):
    id: UUID
    error_code: str
    message: str

    model_config = ConfigDict(extra="forbid")


class LintFindingBatchDecisionData(BaseModel):
    updated: list[LintFindingRead]
    failed: list[LintFindingBatchDecisionFailure]

    model_config = ConfigDict(extra="forbid")


class LintFindingBatchDecisionResponse(RESTResponseBase):
    data: LintFindingBatchDecisionData

    model_config = ConfigDict(extra="forbid")


class LintFindingListResponse(RESTResponseBase):
    data: LintFindingListData

    model_config = ConfigDict(extra="forbid")


class LintRunResponse(RESTResponseBase):
    data: LintRunRead

    model_config = ConfigDict(extra="forbid")


class LintFindingResponse(RESTResponseBase):
    data: LintFindingRead

    model_config = ConfigDict(extra="forbid")


class LintRuleInfo(BaseModel):
    rule_id: str
    layer: str
    default_enabled: bool
    enabled: bool  # effective state = workspace policy override or default
    runtime_checkable: bool
    # A rule the publication floor depends on. Turning it off would hide the
    # finding while saves were still held by it, so the toggle is refused and
    # the row is shown as locked instead of silently disagreeing with itself.
    policy_locked: bool = False

    model_config = ConfigDict(extra="forbid")


class LintRuleListData(BaseModel):
    rules: list[LintRuleInfo] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class LintRuleListResponse(RESTResponseBase):
    data: LintRuleListData

    model_config = ConfigDict(extra="forbid")


class LintFormatRequest(BaseModel):
    # I9: cap the on-demand formatter at the same ceiling as page/context bodies
    # so it cannot be handed a larger payload than the write APIs it supports.
    markdown: str = Field(max_length=BODY_MD_MAX_LENGTH)

    model_config = ConfigDict(extra="forbid")


class LintFormatData(BaseModel):
    markdown: str

    model_config = ConfigDict(extra="forbid")


class LintFormatResponse(RESTResponseBase):
    data: LintFormatData

    model_config = ConfigDict(extra="forbid")
