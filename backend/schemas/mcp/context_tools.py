from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.common import MCPResultEnvelope
from backend.schemas.context_edit import EditOperation
from backend.schemas.context_write import (
    CiteRequest,
    CiteResult,
    SubmitFeedbackRequest,
    SubmitFeedbackResult,
)
from backend.schemas.edit_receipt import EditReceipt
from backend.schemas.page import (
    CONTENT_HASH_PATTERN,
    CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD,
    PAGE_LIST_CURSOR_MAX_LENGTH,
    PAGE_LIST_CURSOR_PATTERN,
    PAGE_LIST_LIMIT_MAX,
    BeginContextEditRequest,
    ContextPageCreateRequest,
    DiscardContextEditRequest,
    DiscardContextEditResult,
    DraftReviewReason,
    ListMyContextEditsResult,
    PagePayload,
    SubmitContextEditRequest,
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


class CiteInput(CiteRequest):
    workspace_id: UUID
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD
    wire: Literal["stable", "mrtr_preview"] = "stable"
    resume_token: str | None = Field(default=None, max_length=4096)
    input_responses: dict[str, dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _validate_resume_pair(self):
        if (self.resume_token is None) != (self.input_responses is None):
            raise ValueError(
                "resume_token and input_responses must be provided together"
            )
        return self

    model_config = ConfigDict(extra="forbid")


class CiteToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: CiteResult | None = None
    input_required: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class SubmitFeedbackInput(SubmitFeedbackRequest):
    workspace_id: UUID
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class SubmitFeedbackToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: SubmitFeedbackResult | None = None

    model_config = ConfigDict(extra="forbid")


# P8.6: the Context-editing-state-machine tool surface
# (create_context_page/begin_context_edit/update_context_edit/
# submit_context_edit_for_review/discard_context_edit/patch_context_edit/
# list_my_context_edits), the current target surface alongside
# submit_revision_for_review (a distinct 8th tool, kept). write_draft/
# update_draft/patch_draft were deregistered as MCP tools; their backing
# ContextWriteService methods remain, used internally by branch
# reconciliation and changes-requested review resubmission. Each wraps the
# matching PageService P8 command 1:1 with Browser REST
# (backend/api/context_pages.py).


class CreateContextPageInput(ContextPageCreateRequest):
    workspace_id: UUID
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class ContextEditResult(BaseModel):
    """What an edit command saved: the version to address next and what changed.

    The body is not repeated. edit_receipt.changes quotes the replaced
    regions; when you need text the receipt does not show, read this version
    by version_id (target.version_id), which still names it after a submit.
    """

    id: UUID
    version_id: UUID
    editing_revision: int
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    version_status: str | None = None
    title: str
    slug: str
    review_item_id: UUID | None = None
    change_set_id: UUID | None = None
    publish_blocked_reasons: list[DraftReviewReason] = Field(default_factory=list)
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


class CreateContextPageToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")


class BeginContextEditInput(BeginContextEditRequest):
    workspace_id: UUID
    page_id: UUID
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class BeginContextEditToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: PagePayload | None = None

    model_config = ConfigDict(extra="forbid")


class UpdateContextEditInput(UpdateContextEditRequest):
    workspace_id: UUID
    page_id: UUID
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class UpdateContextEditToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")


class SubmitContextEditForReviewInput(SubmitContextEditRequest):
    workspace_id: UUID
    page_id: UUID
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class SubmitContextEditForReviewToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")


class DiscardContextEditInput(DiscardContextEditRequest):
    workspace_id: UUID
    page_id: UUID
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
    """Apply typed block/section edit operations to the caller's own open
    'editing' row in place.

    `operations` is the same typed union of block/section edit operations
    minus `resolve_conflict` (reconciliation-only, out of scope here: this
    tool always targets the row's own current body, never a shared
    reconciled head).
    """

    workspace_id: UUID
    page_id: UUID
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
            "response (or read_structure_map with version='my_edit')."
        ),
    )
    idempotency_key: str = CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD
    retry_only: bool = False
    conversation_token: str | None = CONVERSATION_TOKEN_FIELD

    model_config = ConfigDict(extra="forbid")


class PatchContextEditToolOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ContextEditResult | None = None

    model_config = ConfigDict(extra="forbid")
