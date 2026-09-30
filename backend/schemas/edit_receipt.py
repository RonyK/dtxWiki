"""Wire shape of an edit receipt: what one context edit changed.

Computed by backend/services/edit_receipt.py; carried on the MCP edit tools'
responses and stored for the Chats timeline.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EditMode = Literal["patch", "whole_body", "create"]


class EditReceiptFirstChange(BaseModel):
    line: int = Field(description="1-indexed line of the first change in the new body.")
    before_preview: str
    after_preview: str

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
    first_change: EditReceiptFirstChange | None = None
    operations: list[EditReceiptOperation] = Field(default_factory=list)
    changed_blocks: list[EditReceiptBlock] = Field(
        default_factory=list,
        description=(
            "Blocks whose id or hash is new after a patch, so a follow-up "
            "block-addressed patch needs no read_structure_map round trip. "
            "Unchanged blocks keep the id and hash you already hold."
        ),
    )

    model_config = ConfigDict(extra="forbid")
