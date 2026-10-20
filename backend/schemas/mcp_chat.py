from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.schemas.common import RESTResponseBase
from backend.schemas.input_policy import InputRequest

McpChatArtifactKind = Literal[
    "context_page", "library_item", "agent_page", "context_media_asset"
]
McpChatArtifactVerb = Literal[
    "created", "edited", "cited", "referenced", "search_match"
]
McpChatPeriod = Literal["today", "week", "month", "all"]
McpChatSort = Literal["last_activity_desc", "write_count_desc"]
McpChatTitleSource = Literal[
    "user",
    "artifact",
    "query",
    "input_text",
    "target_kind",
    "tool",
    "generic",
]
McpChatAction = Literal["search", "read", "create", "edit", "cite", "guide", "other"]
McpChatInputAvailability = Literal[
    "available", "redacted", "not_available", "not_applicable"
]
McpChatResultAvailability = Literal[
    "available", "redacted", "status_only", "not_available"
]
# Why part of a timeline row is withheld from this reader:
# - target_hidden: the call named or touched a page/source the reader cannot
#   see now, so everything but the call's shape (tool, error code, operation
#   kinds, revision numbers, which fields were sent) is withheld.
# - error_restricted: a failed call's server message, shown only to the user
#   whose client made the call. The rest of the call stays visible.
McpChatRedactionReason = Literal["target_hidden", "error_restricted"]
McpChatEditMode = Literal["patch", "whole_body", "create"]


class McpChatDisplayItem(BaseModel):
    """Permission-safe, locale-neutral fact for the Chats detail UI."""

    key: str
    kind: Literal["text", "artifact", "status"]
    text: str
    resource_kind: McpChatArtifactKind | None = None
    resource_id: UUID | None = None
    navigation_path: str | None = None

    model_config = ConfigDict(extra="forbid")


class McpChatPresentationDescriptor(BaseModel):
    """Read-time facts from which a localized chat label can be composed.

    The backend ranks locale-neutral evidence only. ``subject`` is never built
    by parsing result-summary prose or by inferring an external user's prompt.
    """

    title_source: McpChatTitleSource
    action: McpChatAction
    subject: str | None
    subject_kind: str | None
    primary_tool_name: str | None

    model_config = ConfigDict(extra="forbid")


class McpChatClientRef(BaseModel):
    # List/provenance responses carry a bounded sanitized display name and an
    # empty scopes list. Authorized detail/export responses retain the sanitized
    # full allowed display name and scopes. Keeping one additive-compatible
    # shape avoids making clients branch on two identity schemas.
    id: UUID
    name: str
    client_type: str
    brand_hint: str | None
    scopes: list[str]

    model_config = ConfigDict(extra="forbid")


class McpChatArtifactLinkPayload(BaseModel):
    id: UUID
    resource_kind: McpChatArtifactKind
    resource_id: UUID
    artifact_title: str | None
    navigation_path: str | None
    verb: McpChatArtifactVerb
    first_occurred_at: datetime
    last_occurred_at: datetime
    occurrence_count: int
    last_mcp_tool_call_id: UUID

    model_config = ConfigDict(extra="forbid")


class McpChatTimelineArtifact(BaseModel):
    resource_kind: McpChatArtifactKind
    resource_id: UUID
    artifact_title: str | None
    navigation_path: str | None
    verb: McpChatArtifactVerb

    model_config = ConfigDict(extra="forbid")


class McpChatTextPreview(BaseModel):
    """A masked prefix (about 300 characters) of one text field a call sent.

    ``text`` is ``None`` when the preview was deleted (retention or a discarded
    edit); the field it came from is still named so the row keeps its shape.
    """

    field: str
    text: str | None
    truncated: bool = False

    model_config = ConfigDict(extra="forbid")


class McpChatEditFirstChange(BaseModel):
    line: int
    before_preview: str | None = None
    after_preview: str | None = None

    model_config = ConfigDict(extra="forbid")


McpChatMatchKind = Literal["exact", "normalized", "fuzzy"]


class McpChatEditReceiptOperation(BaseModel):
    index: int
    op: str
    line_range: list[int] | None = None
    heading_path: list[str] | None = None
    affected_block_ids: list[str] = Field(default_factory=list)
    chars_added: int | None = None
    chars_removed: int | None = None
    match_kind: McpChatMatchKind | None = None
    match_similarity: float | None = None
    matched_sha256: str | None = None

    model_config = ConfigDict(extra="forbid")


class McpChatEditReceiptBlock(BaseModel):
    """A block a patch changed, by its new address (no content)."""

    id: str
    kind: str
    hash: str

    model_config = ConfigDict(extra="forbid")


class McpChatEditReceipt(BaseModel):
    """What the server measured a context edit to have changed."""

    edit_mode: McpChatEditMode
    editing_revision_before: int | None = None
    editing_revision_after: int | None = None
    chars_added: int | None = None
    chars_removed: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    changed_sections: list[list[str]] = Field(default_factory=list)
    first_change: McpChatEditFirstChange | None = None
    operations: list[McpChatEditReceiptOperation] = Field(default_factory=list)
    changed_blocks: list[McpChatEditReceiptBlock] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class McpChatResultDetail(BaseModel):
    edit_receipt: McpChatEditReceipt | None = None

    model_config = ConfigDict(extra="forbid")


class McpChatEditOperation(BaseModel):
    """One patch operation as Chats shows it: kind, place, size, previews."""

    index: int
    op: str
    heading_path: list[str] | None = None
    line_range: list[int] | None = None
    chars_added: int | None = None
    chars_removed: int | None = None
    # How replace_text found its target: "normalized" ignored invisible
    # differences, "fuzzy" forgave a copy slip (with its similarity).
    match_kind: McpChatMatchKind | None = None
    match_similarity: float | None = None
    previews: list[McpChatTextPreview] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class McpChatEditSummary(BaseModel):
    """Read-time summary of what a write call changed.

    ``source`` says where the numbers come from: ``receipt`` is the server's
    own measurement of the applied edit; ``input`` is estimated from the
    sizes of what the call sent (calls captured before receipts existed, or a
    failed call), so a removal it cannot see is ``None``, not zero.
    """

    source: Literal["receipt", "input"]
    edit_mode: McpChatEditMode | None = None
    operation_counts: dict[str, int] = Field(default_factory=dict)
    operations: list[McpChatEditOperation] = Field(default_factory=list)
    changed_sections: list[list[str]] = Field(default_factory=list)
    chars_added: int | None = None
    chars_removed: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    body_preview: McpChatTextPreview | None = None
    first_change: McpChatEditFirstChange | None = None
    # Fields the per-call preview budget left as digest only.
    preview_budget_exhausted: int = 0
    # At least one preview of this call was deleted.
    previews_purged: bool = False
    # The call wrote into a version this reader cannot see (another author's
    # private editing session), so its previews are not shown to them.
    previews_withheld: bool = False

    model_config = ConfigDict(extra="forbid")


class McpChatOperationConflict(BaseModel):
    """Two operations of one patch that target overlapping text."""

    left_op_index: int
    right_op_index: int
    left_lines: list[int] | None = None
    right_lines: list[int] | None = None
    overlap_kind: (
        Literal["same_range", "partial", "contains", "insertion_boundary"] | None
    ) = None

    model_config = ConfigDict(extra="forbid")


class McpChatMatchCandidate(BaseModel):
    """Where a refused replace_text nearly matched (positions, no text)."""

    line_start: int
    line_end: int
    block_id: str | None = None
    similarity: float
    match_kind: McpChatMatchKind

    model_config = ConfigDict(extra="forbid")


class McpChatErrorDetail(BaseModel):
    """Where a failed call went wrong, as structure rather than prose."""

    code: str
    field: str | None = None
    operation_index: int | None = None
    conflict: McpChatOperationConflict | None = None
    candidates: list[McpChatMatchCandidate] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class McpChatSessionDiff(BaseModel):
    """The two versions whose diff shows an edit session's cumulative change.

    ``from_version_id`` is ``None`` for a brand-new page (compare against an
    empty document). Computed at read time and offered only when this reader
    can see both versions.
    """

    page_id: UUID
    from_version_id: UUID | None
    to_version_id: UUID

    model_config = ConfigDict(extra="forbid")


class McpChatTimelineItem(McpChatPresentationDescriptor):
    id: UUID
    tool_name: str
    headline: str
    schema_version: str
    input_summary: dict[str, Any]
    input_availability: McpChatInputAvailability = "not_available"
    input_display_items: list[McpChatDisplayItem] = Field(default_factory=list)
    result_status: str
    result_summary: str | None
    result_availability: McpChatResultAvailability = "not_available"
    result_display_items: list[McpChatDisplayItem] = Field(default_factory=list)
    redaction_reason: McpChatRedactionReason | None = None
    result_detail: McpChatResultDetail | None = None
    edit_summary: McpChatEditSummary | None = None
    session_diff: McpChatSessionDiff | None = None
    error_details: list[McpChatErrorDetail] = Field(default_factory=list)
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None
    latency_ms: int | None
    linked_verbs: list[McpChatArtifactVerb]
    linked_artifacts: list[McpChatTimelineArtifact]

    model_config = ConfigDict(extra="forbid")


class McpChatSessionRef(McpChatPresentationDescriptor):
    id: UUID
    workspace_id: UUID
    client: McpChatClientRef
    mcp_session_id: str
    title: str
    summary: str
    started_at: datetime
    last_activity_at: datetime
    tool_call_count: int
    write_count: int
    error_count: int
    # `error_count` sums failed, rejected_by_policy and conflict, which makes
    # "the tool broke" indistinguishable from "workspace policy said no" -- two
    # very different signals on an audit screen.
    failed_count: int = 0
    rejected_count: int = 0
    archived_at: datetime | None
    conversation_group_id: UUID | None = None
    title_user_edited: bool
    # What the client said it was doing, and which field that sentence came
    # from. The source travels with the text so the screen can label it as the
    # agent's own words rather than let a reader take it for their own.
    intent_summary: str | None = None
    intent_source: Literal["write_reason", "search_query", "claim_text"] | None = None
    linked_artifacts: list[McpChatArtifactLinkPayload]

    model_config = ConfigDict(extra="forbid")


class McpChatListPayload(BaseModel):
    items: list[McpChatSessionRef]
    next_cursor: str | None
    clients: list[McpChatClientRef]

    model_config = ConfigDict(extra="forbid")


class McpChatListResponse(RESTResponseBase):
    data: McpChatListPayload

    model_config = ConfigDict(extra="forbid")


class McpChatDetail(McpChatSessionRef):
    timeline: list[McpChatTimelineItem]
    frozen_summary: str | None
    frozen_at: datetime | None

    model_config = ConfigDict(extra="forbid")


class McpChatDetailResponse(RESTResponseBase):
    data: McpChatDetail

    model_config = ConfigDict(extra="forbid")


class McpChatTitleUpdateRequest(InputRequest):
    title: str = Field(min_length=1, max_length=120)

    model_config = ConfigDict(extra="forbid")

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        # min_length=1 is checked against the raw string, so a whitespace-only
        # title ("   ") would pass and then strip() to "" in the service,
        # persisting a blank title and permanently locking out auto-title
        # regeneration (title_user_edited=True). Reject post-strip and return
        # the normalized value (S3, pairs with the frontend saveTitle guard).
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped


class McpChatArchiveResult(BaseModel):
    chat_session_id: UUID
    archived_at: datetime | None = None
    audit_event_id: UUID

    model_config = ConfigDict(extra="forbid")


class McpChatArchiveResponse(RESTResponseBase):
    data: McpChatArchiveResult

    model_config = ConfigDict(extra="forbid")


class McpChatPreviewPurgeResult(BaseModel):
    chat_session_id: UUID
    # Tool calls in this chat that had at least one preview cleared.
    purged_call_count: int
    audit_event_id: UUID

    model_config = ConfigDict(extra="forbid")


class McpChatPreviewPurgeResponse(RESTResponseBase):
    data: McpChatPreviewPurgeResult

    model_config = ConfigDict(extra="forbid")


class McpChatProvenanceEntry(BaseModel):
    verb: McpChatArtifactVerb
    occurrence_count: int
    last_occurred_at: datetime
    chat_session: McpChatSessionRef

    model_config = ConfigDict(extra="forbid")


class McpChatProvenancePayload(BaseModel):
    items: list[McpChatProvenanceEntry]

    model_config = ConfigDict(extra="forbid")


class McpChatProvenanceResponse(RESTResponseBase):
    data: McpChatProvenancePayload

    model_config = ConfigDict(extra="forbid")


class McpChatArtifactLinksPayload(BaseModel):
    items: list[McpChatArtifactLinkPayload]
    next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class McpChatArtifactLinksResponse(RESTResponseBase):
    data: McpChatArtifactLinksPayload

    model_config = ConfigDict(extra="forbid")


class McpChatExportPayload(BaseModel):
    exported_at: datetime
    workspace_id: UUID
    range: dict[str, datetime | None]
    chats: list[McpChatDetail]
    next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class McpChatExportResponse(RESTResponseBase):
    data: McpChatExportPayload

    model_config = ConfigDict(extra="forbid")
