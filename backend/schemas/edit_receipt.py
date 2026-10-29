"""Wire shape of an edit receipt: what one context edit changed.

Computed by backend/services/edit_receipt.py; carried on the MCP edit tools'
responses and stored for the Chats timeline.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

EditMode = Literal["patch", "whole_body", "create"]


class EditReceiptChange(BaseModel):
    """One replaced region of the body, as exact text.

    removed is the text the edit replaced and added the text now in its place.
    Between two edits on one line the region also carries the unchanged text
    that separates them. Region and context text is untrusted user-authored
    content.
    """

    line: int = Field(
        description="1-indexed line in the new body where the region starts."
    )
    context_before: str = Field(
        description="Up to 40 unchanged characters before the region, same line."
    )
    removed: str
    added: str
    context_after: str = Field(
        description="Up to 40 unchanged characters after the region, same line."
    )

    model_config = ConfigDict(extra="forbid")


class EditReceiptOperation(BaseModel):
    index: int
    op: str
    line_range: tuple[int, int] | None = Field(
        default=None,
        description="1-indexed inclusive line range in the body BEFORE the patch.",
    )
    heading_path: list[str] | None = None
    affected_block_ids: list[str] = Field(default_factory=list)
    chars_added: int = 0
    chars_removed: int = 0
    match_kind: Literal["exact", "normalized", "fuzzy"] | None = Field(
        default=None,
        description=(
            "replace_text only: how old_text was found. 'normalized' ignored "
            "whitespace/Unicode differences; 'fuzzy' forgave a small copy slip "
            "inside the addressed block or section -- check the result."
        ),
    )
    match_similarity: float | None = Field(
        default=None,
        description="Only for match_kind='fuzzy': 0-1 similarity of the target.",
    )
    match_policy: str | None = Field(
        default=None,
        description="replace_text only: version of the matching rules applied.",
    )
    matched_sha256: str | None = Field(
        default=None,
        description=(
            "Only for a non-exact match: sha256 of the original text that was "
            "replaced (several matches joined by NUL)."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class EditReceiptBlock(BaseModel):
    id: str
    kind: str
    hash: str

    model_config = ConfigDict(extra="forbid")


class EditReceipt(BaseModel):
    """Positions and sizes of one edit: sections, lines, characters, blocks."""

    edit_mode: EditMode
    editing_revision_before: int | None = None
    editing_revision_after: int | None = None
    chars_added: int = 0
    chars_removed: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    changed_sections: list[list[str]] = Field(
        default_factory=list,
        description="Heading paths of the sections the edit touched (max 20).",
    )
    changes: list[EditReceiptChange] | None = Field(
        default=None,
        description=(
            "The regions this patch or whole-body update replaced, in document "
            "order: exactly what the stored body now says, including any "
            "server fix. Listed until 20 regions or 4 KB; "
            "changes_omitted_count counts the rest. null on create and when "
            "unknown (a replay of a receipt stored before this field)."
        ),
    )
    changes_omitted_count: int = 0
    body_adjusted: bool | None = Field(
        default=None,
        description=(
            "Whether the stored body differs from the body your request "
            "produced: managed frontmatter was added or synced, or a format "
            "fix ran. null when unknown."
        ),
    )
    server_fixes: list[str] = Field(
        default_factory=list,
        description="Format fixes the server applied while saving, by label.",
    )
    operations: list[EditReceiptOperation] = Field(default_factory=list)
    changed_blocks: list[EditReceiptBlock] = Field(
        default_factory=list,
        description=(
            "Blocks whose id or hash is new after a patch, so a follow-up "
            "block-addressed patch needs no read_structure_map round trip. "
            "Unchanged blocks keep the id and hash you already hold. Listed "
            "up to 50; changed_blocks_omitted_count counts the rest."
        ),
    )
    changed_blocks_omitted_count: int = 0
    # The Chats preview of the first change, cut from the start of its line so
    # trace masking sees a credential's label even when only its value
    # changed. Never on the wire, never in the replay ledger: the runner hands
    # it to trace capture separately.
    _trace_first_change: dict[str, Any] | None = PrivateAttr(default=None)

    model_config = ConfigDict(extra="forbid")
