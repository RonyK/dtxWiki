from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.common import RESTResponseBase
from backend.schemas.input_policy import InputRequest
from backend.schemas.page import BODY_MD_MAX_LENGTH


class ContextReconciliationPage(BaseModel):
    id: UUID
    slug: str
    title: str

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationVersion(BaseModel):
    version_id: UUID
    version_no: int
    content_hash: str
    created_at: datetime

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationBranch(ContextReconciliationVersion):
    actor_kind: str
    actor_display_name: str | None = None
    base_version_id: UUID


class ContextReconciliationSide(BaseModel):
    hash: str | None = None
    text: str | None = None
    actor_kind: str | None = None
    actor_display_name: str | None = None
    at: datetime | None = None
    version_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationOperationEffect(BaseModel):
    op: Literal["insert_after"]
    content: str

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationConflict(BaseModel):
    block_id: str
    resolution_target_id: str | None = None
    kind: str
    granularity: Literal["block", "table_row"]
    parent_block_id: str | None = None
    row_index: int | None = Field(default=None, ge=0)
    line_span: tuple[int, int] | None = None
    base: ContextReconciliationSide
    head: ContextReconciliationSide
    yours: ContextReconciliationSide
    head_effects: list[ContextReconciliationOperationEffect] = Field(
        default_factory=list
    )
    yours_effects: list[ContextReconciliationOperationEffect] = Field(
        default_factory=list
    )
    identity_retired: bool
    restore_yours_available: bool

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationCleanChange(BaseModel):
    block_id: str
    resolution_target_id: str | None = None
    kind: str
    state: Literal["head_only", "branch_only"]
    head: ContextReconciliationSide | None = None
    yours: ContextReconciliationSide | None = None

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationBlock(BaseModel):
    block_id: str
    kind: str
    granularity: Literal["block", "table_row"]
    parent_block_id: str | None = None
    row_index: int | None = Field(default=None, ge=0)
    state: Literal["same", "head_only", "branch_only", "conflict"]
    label: str

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationDetail(BaseModel):
    page: ContextReconciliationPage
    branch: ContextReconciliationBranch
    base: ContextReconciliationVersion
    original_head: ContextReconciliationVersion
    head: ContextReconciliationVersion
    head_advance_count: int = Field(ge=0)
    blocks: list[ContextReconciliationBlock]
    conflicts: list[ContextReconciliationConflict]
    clean_changes: list[ContextReconciliationCleanChange]

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationDetailResponse(RESTResponseBase):
    data: ContextReconciliationDetail

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationResolution(BaseModel):
    block_id: str = Field(min_length=1, max_length=160)
    choice: Literal["keep_head", "restore_yours", "custom"]
    expected_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    content: str | None = Field(default=None, max_length=BODY_MD_MAX_LENGTH)

    @model_validator(mode="after")
    def validate_custom_content(self):
        if self.choice == "custom":
            if self.content is None or not self.content.strip():
                raise ValueError("custom resolution requires non-blank content")
        elif self.content is not None:
            raise ValueError("content is only allowed for a custom resolution")
        return self

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationRequest(InputRequest):
    expected_head_version_id: UUID
    reconciles: list[UUID] = Field(min_length=1, max_length=1)
    resolutions: list[ContextReconciliationResolution] = Field(max_length=20)
    excluded_clean_change_ids: list[
        Annotated[str, Field(min_length=1, max_length=160)]
    ] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    dry_run: bool = False

    @model_validator(mode="after")
    def validate_unique_resolutions(self):
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("reason must not be blank")
        block_ids = [resolution.block_id for resolution in self.resolutions]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("each conflict block_id may be resolved only once")
        if len(self.excluded_clean_change_ids) != len(
            set(self.excluded_clean_change_ids)
        ):
            raise ValueError("excluded_clean_change_ids must not contain duplicates")
        return self

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationPreview(BaseModel):
    head_version_id: UUID
    merged_body_markdown: str
    predicted_block_map: list[dict]
    unresolved_count: Literal[0]

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationCommit(BaseModel):
    draft_version_id: UUID
    head_version_id: UUID
    branch_status: Literal["superseded"]
    review_item_id: UUID | None

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationDryRunResult(BaseModel):
    dry_run: Literal[True]
    preview: ContextReconciliationPreview

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationCommitResult(BaseModel):
    dry_run: Literal[False]
    commit: ContextReconciliationCommit

    model_config = ConfigDict(extra="forbid")


ContextReconciliationResult = (
    ContextReconciliationDryRunResult | ContextReconciliationCommitResult
)


class ContextReconciliationResultResponse(RESTResponseBase):
    data: ContextReconciliationResult

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationDiscardRequest(InputRequest):
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_reason(self):
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("reason must not be blank")
        return self

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationBulkDiscardRequest(InputRequest):
    review_item_ids: list[UUID] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_request(self):
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("reason must not be blank")
        if len(self.review_item_ids) != len(set(self.review_item_ids)):
            raise ValueError("review_item_ids must not contain duplicates")
        return self

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationBulkDiscardSkipped(BaseModel):
    review_item_id: UUID
    error_code: str

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationBulkDiscardResult(BaseModel):
    discarded_review_item_ids: list[UUID]
    skipped: list[ContextReconciliationBulkDiscardSkipped]

    model_config = ConfigDict(extra="forbid")


class ContextReconciliationBulkDiscardResponse(RESTResponseBase):
    data: ContextReconciliationBulkDiscardResult

    model_config = ConfigDict(extra="forbid")
