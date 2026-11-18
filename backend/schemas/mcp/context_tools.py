from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.common import WIRE_EXPLICIT_FIELD, MCPResultEnvelope
from backend.schemas.context_edit import EditOperation
from backend.schemas.context_write import (
    CiteResult as ReferenceCommandResult,
)
from backend.schemas.context_write import (
    ReferenceCommand,
    SubmitFeedbackRequest,
    SubmitFeedbackResult,
)
from backend.schemas.edit_receipt import EditReceipt
from backend.schemas.input_policy import InputRequest
from backend.schemas.mcp.page_input import MCPPageInput, validate_typed_target
from backend.schemas.page import (
    BODY_MD_MAX_LENGTH,
    CONTENT_HASH_PATTERN,
    CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD,
    PAGE_LIST_CURSOR_MAX_LENGTH,
    PAGE_LIST_CURSOR_PATTERN,
    PAGE_LIST_LIMIT_MAX,
    BeginContextEditCommand,
    ContextPageCreateRequest,
    DiscardContextEditCommand,
    DiscardContextEditResult,
    DraftReviewReason,
    ListMyContextEditsResult,
    PageMetadataPatch,
    PagePayload,
    SubmitContextEditCommand,
    UpdateContextEditRequest,
)

CONVERSATION_TOKEN_FIELD = Field(
    default=None,
    min_length=1,
    max_length=160,
    description=(
        "Opaque MCP conversation token. Omit or pass 'new' to start a "
        "new dtxWiki MCP chat; echo the returned envelope value to append."
    ),
)


class CiteInput(ReferenceCommand):
    page_id: MCPPageInput
    workspace_id: UUID
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class CiteResult(ReferenceCommandResult):
    # References belong to the shared mutable draft.
    publication_state: Literal["draft"] = "draft"


class CiteToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: CiteResult | None = None

    model_config = ConfigDict(extra="forbid")


class SubmitFeedbackInput(SubmitFeedbackRequest):
    workspace_id: UUID
    target_id: MCPPageInput
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    @model_validator(mode="before")
    @classmethod
    def _typed_target(cls, value):
        return validate_typed_target(
            value, type_field="target_type", id_field="target_id"
        )

    model_config = ConfigDict(extra="forbid")


class SubmitFeedbackToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: SubmitFeedbackResult | None = None

    model_config = ConfigDict(extra="forbid")


# Context draft tools share their commands with browser REST.


class CreateContextPageInput(ContextPageCreateRequest):
    workspace_id: UUID
    idempotency_key: str = CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class ContextEditResult(BaseModel):
    """What an edit command saved: the version to address next and what changed.

    The body is not repeated. edit_receipt.changes quotes the replaced
    regions; when you need text the receipt does not show, read this version
    by page_id and version_id, which still name it after a submit.
    """

    page_id: UUID
    version_id: UUID
    editing_revision: int
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    version_status: str | None = None
    title: str
    slug: str
    replayed: bool = False
    placement_parent_node_id: UUID | None = None
    review_item_id: UUID | None = None
    change_set_id: UUID | None = None
    publish_blocked_reasons: list[DraftReviewReason] = Field(default_factory=list)
    lint_report: dict[str, Any] = Field(
        default_factory=lambda: {"fixes": [], "findings": []},
        description="Save-time fixes and findings for the final stored body.",
    )
    edit_receipt: EditReceipt | None = Field(
        default=None,
        description=(
            "What this write changed: sections, line/char counts, the replaced "
            "regions, per-operation positions, and (for a patch) the id and "
            "hash of every block whose id or hash changed. replace_block keeps "
            "the block's id; a block other edits changed keeps its id while "
            "its text stays close to the old text, otherwise it gets a new "
            "one. An idempotent replay returns the receipt of the call it "
            "replays, which may describe an earlier revision. null on submit."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @property
    def id(self) -> UUID:
        return self.page_id


class CreateContextPageToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")


class BeginContextEditInput(BeginContextEditCommand):
    workspace_id: UUID
    page_id: MCPPageInput
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class BeginContextEditResult(PagePayload):
    """The complete open edit, with the MCP page identity spelling."""

    # The shared REST model keeps these fields until its UI cleanup.
    # They are absent from the current MCP contract.
    alternative_draft_id: ClassVar[None] = None
    preserved_as_alternative: ClassVar[bool] = False
    superseded_review_count: ClassVar[int] = 0
    id: UUID = Field(alias="page_id")
    replayed: bool = False

    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    @property
    def page_id(self) -> UUID:
        return self.id


class BeginContextEditToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: BeginContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")


class UpdateContextEditInput(UpdateContextEditRequest):
    workspace_id: UUID
    page_id: MCPPageInput
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class UpdateContextEditToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")


class SubmitContextEditForReviewInput(SubmitContextEditCommand):
    workspace_id: UUID
    page_id: MCPPageInput
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class SubmitContextEditForReviewToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")


class DiscardContextEditInput(DiscardContextEditCommand):
    workspace_id: UUID
    page_id: MCPPageInput
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class DiscardContextEditToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: DiscardContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")


class ListMyContextEditsInput(BaseModel):
    workspace_id: UUID
    limit: int = Field(default=50, ge=1, le=PAGE_LIST_LIMIT_MAX)
    cursor: str | None = Field(
        default=None,
        max_length=PAGE_LIST_CURSOR_MAX_LENGTH,
        pattern=PAGE_LIST_CURSOR_PATTERN,
    )
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class ListMyContextEditsToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ListMyContextEditsResult | None = None

    model_config = ConfigDict(extra="forbid")


# There is no Browser REST equivalent for this command, unlike the other six
# P8.6 tools, so its wire shape lives here rather than in
# backend/schemas/page.py (which backend/schemas/context_edit.py itself
# imports from -- defining it there would be circular). This comment is
# intentionally NOT the class docstring: a docstring becomes this model's
# public JSON schema "description", and citing source paths there trips
# tests/static/test_public_schema_path_leak.py.
class PatchContextEditInput(BaseModel):
    """Apply typed block/section edit operations to the named shared draft.

    `operations` is the typed union of block/section edit operations; this
    tool always targets the row's own current body.
    """

    workspace_id: UUID
    page_id: MCPPageInput
    version_id: UUID
    operations: list[EditOperation] = Field(
        min_length=1,
        max_length=20,
        description=(
            "1-20 edits applied atomically in one revision. Every operation is "
            "resolved against the SAME current body (not against the result of "
            "the operation before it). If any operation is refused, or any two "
            "target overlapping text, nothing is changed and error_details "
            "names the operation_index. Independent replace_text ops may be "
            "combined even without `at`. Do not mix block-addressed ops "
            "(replace_block, insert_after, insert_row, delete_block) with "
            "text/section ops in one call; replace_text with a block `at` "
            "counts as a text op. replace_block takes exactly ONE top-level "
            "block and insert_row ONE table row; insert_after takes any "
            "number of blocks. Replacing or deleting a block and inserting "
            "after it in the same call overlaps: put the extra blocks in a "
            "replace_text new_text, or send a second call with the block's "
            "new hash from data.edit_receipt. Best for changing a sentence: "
            '[{"op": "replace_text", "old_text": "<sentence copied '
            'from the block>", "new_text": "<new sentence>", "at": '
            '{"by": "block", "block": "<id from read_structure_map>", '
            '"expected": "<its hash>"}}] -- a small copy slip in old_text '
            "is then forgiven (receipt match_kind='fuzzy'). Other examples: "
            '[{"op": "replace_text", "old_text": "recieve", '
            '"new_text": "receive"}] or [{"op": "replace_block", '
            '"block": "<id>", "expected": "<its hash>", '
            '"content": "New paragraph."}].'
        ),
    )

    expected_editing_revision: int = Field(
        ge=1,
        description=(
            "The editing_revision from your last create/begin/update/patch "
            "response (or read_structure_map with that version_id)."
        ),
    )
    idempotency_key: str = CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class PatchContextEditToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")


_COMMIT_VERSION_ID_FIELD = Field(
    default=None,
    description="A commit of this page; omit for the current published version.",
)


class _ContextMergeInput(InputRequest):
    workspace_id: UUID
    page_id: MCPPageInput
    version_id: UUID = Field(description="The draft to preview, merge into or publish.")
    on_conflict: Literal["keep_both", "latest"] | None = Field(
        default=None,
        description=(
            "Needed only when the changes conflict: keep_both keeps both sides, "
            "latest keeps the side saved last."
        ),
    )
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class PreviewContextMergeInput(_ContextMergeInput):
    commit_version_id: UUID | None = _COMMIT_VERSION_ID_FIELD
    body_md: str | None = Field(default=None, max_length=BODY_MD_MAX_LENGTH)
    page_metadata: PageMetadataPatch | None = Field(
        default=None,
        description=(
            "Edits to preview with body_md; publish both unchanged as "
            "result_body_md and result_page_metadata."
        ),
    )


class ContextMergeMetadata(BaseModel):
    title: str
    page_type: str
    tags: list[str]
    aliases: list[str]
    placement_parent_node_id: UUID | None

    model_config = ConfigDict(extra="forbid")


class ContextMergeConflict(BaseModel):
    ours: str
    theirs: str
    keep_both_start: int
    keep_both_end: int
    latest_start: int
    latest_end: int

    model_config = ConfigDict(extra="forbid")


class ContextMergeCandidate(BaseModel):
    body_md: str
    page_metadata: ContextMergeMetadata
    block_map: list[dict[str, Any]]
    result_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    conflicts: list[ContextMergeConflict]
    offset_unit: Literal["unicode_code_points"]
    conflict_body_md: str | None = None

    model_config = ConfigDict(extra="forbid")


class PreviewContextMergeResult(BaseModel):
    page_id: UUID
    version_id: UUID
    editing_revision: int
    published_version_id: UUID | None = Field(json_schema_extra=WIRE_EXPLICIT_FIELD)
    parent_version_ids: list[UUID]
    kind: Literal["fast_forward", "automatic_merge", "conflict"]
    # What publish_context_edit publishes for the same on_conflict and edits;
    # null only for a conflict without a choice.
    result: ContextMergeCandidate | None = Field(json_schema_extra=WIRE_EXPLICIT_FIELD)
    # The two choices, sent only for a conflict.
    keep_both: ContextMergeCandidate | None = None
    latest: ContextMergeCandidate | None = None
    metadata_conflicts: list[str]
    base_conflicted: bool
    latest_side: Literal["ours", "theirs"]
    offset_unit: Literal["unicode_code_points"]

    model_config = ConfigDict(extra="forbid")


class PreviewContextMergeToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: PreviewContextMergeResult | None = None

    model_config = ConfigDict(extra="forbid")


class BringInContextVersionInput(_ContextMergeInput):
    commit_version_id: UUID | None = _COMMIT_VERSION_ID_FIELD
    expected_editing_revision: int = Field(
        ge=1, description="The draft's editing_revision from your last read or write."
    )
    idempotency_key: str = CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD


class BringInContextVersionToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")


class PublishContextEditInput(_ContextMergeInput):
    result_body_md: str | None = Field(default=None, max_length=BODY_MD_MAX_LENGTH)
    result_page_metadata: PageMetadataPatch | None = Field(
        default=None,
        description="Send the result edits you previewed, unchanged.",
    )
    expected_editing_revision: int = Field(
        ge=1, description="editing_revision from preview_context_merge."
    )
    expected_published_version_id: UUID | None = Field(
        default=None,
        description=(
            "published_version_id from preview_context_merge; omit when it was null."
        ),
    )
    result_hash: str = Field(
        pattern=CONTENT_HASH_PATTERN,
        description="result.result_hash from preview_context_merge.",
    )
    reason: str = Field(min_length=1)
    idempotency_key: str = CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD


class PublishContextEditToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")
