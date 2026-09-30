from __future__ import annotations

import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.schemas.page import BODY_MD_MAX_LENGTH

_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_HEADING_PATH_SEPARATOR = ">"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SectionIdAddress(_StrictModel):
    by: Literal["section_id"]
    section_id: str = Field(min_length=1)


class HeadingPathAddress(_StrictModel):
    by: Literal["heading_path"]
    heading_path: tuple[str, ...] = Field(
        description=(
            "Every enclosing heading from the top of the page down to the "
            "target, as written -- the page's # title included, not just the "
            'last heading: ["Guide", "Install", "Linux"]. read_context_outline '
            "lists each section's path."
        )
    )
    occurrence: int = Field(default=1, ge=1)

    @field_validator("heading_path", mode="before")
    @classmethod
    def _split_heading_path(cls, value):
        if isinstance(value, str):
            value = value.split(_HEADING_PATH_SEPARATOR)
        return value

    @field_validator("heading_path")
    @classmethod
    def _validate_heading_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(part.strip() for part in value)
        if not normalized or any(not part for part in normalized):
            raise ValueError("heading_path must contain non-blank segments")
        return normalized


class AnchorAddress(_StrictModel):
    by: Literal["anchor"] = Field(deprecated=True)
    anchor_text: str = Field(min_length=1, deprecated=True)
    context_before: str = Field(default="", deprecated=True)
    context_after: str = Field(default="", deprecated=True)
    char_offset_hint: int = Field(ge=0, deprecated=True)


type SectionAddress = Annotated[
    SectionIdAddress | HeadingPathAddress | AnchorAddress,
    Field(discriminator="by"),
]


class BlockTextAddress(_StrictModel):
    """One block from read_structure_map, as the scope of a replace_text.

    The same `block`/`expected` pair replace_block takes: the id names the
    block and `expected` (its current content hash) proves the caller saw its
    current text. A slightly wrong id or hash fails; it never picks another
    block.
    """

    by: Literal["block"]
    block: str = Field(min_length=1)
    expected: str

    @field_validator("expected")
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if _SHA256_HEX_PATTERN.fullmatch(value) is None:
            raise ValueError("expected must be a sha256 hex digest")
        return value


type TextScopeAddress = Annotated[
    SectionIdAddress | HeadingPathAddress | AnchorAddress | BlockTextAddress,
    Field(discriminator="by"),
]

_ACCEPT_FUZZY_DESCRIPTION = (
    "Only for a deprecated `at.by='anchor'` address: accept an anchor found "
    "far from char_offset_hint. It never loosens old_text matching (see "
    "match_mode) or any hash check."
)


# One text on every variant's `content`: the flattened schema keeps a shared
# field's first description, and the per-op limits belong where a caller
# fills the field in, not only in the list-level prose.
_CONTENT_DESCRIPTION = (
    "Markdown to write. replace_block: exactly ONE top-level block (for more, "
    "use replace_text or insert_after); insert_row: ONE table row; other ops: "
    "any number of blocks."
)


class _Operation(_StrictModel):
    pass


class _AnchorScopedOperation(_Operation):
    # Only operations whose `at` can be an AnchorAddress read this; block,
    # row and whole-document operations have nothing for it to loosen, so
    # they do not advertise it.
    accept_fuzzy: bool = Field(default=False, description=_ACCEPT_FUZZY_DESCRIPTION)


# The operation kinds that still carry accept_fuzzy (the subclasses of
# _AnchorScopedOperation below).
ANCHOR_SCOPED_OPERATION_KINDS = frozenset(
    {
        "replace_text",
        "insert_text",
        "replace_section",
        "insert_section",
        "delete_section",
    }
)


def without_legacy_operation_fields(operations: object) -> object:
    """Stored operation dumps, minus fields a newer model no longer accepts.

    Until tool contract revision 9, accept_fuzzy sat on every operation, so
    model_dump() wrote it into every durable operation list (diverged-branch
    reconcile manifests). The field now exists only on anchor-capable
    operations and the models forbid extra keys, so re-validating an older
    manifest would fail. Dropping the key there is lossless: those operations
    never read it. Only a boolean is dropped -- the shape older code wrote --
    so a corrupted value still fails validation, and anything that is not a
    list of dicts passes through for the model to reject.
    """

    if not isinstance(operations, list):
        return operations
    return [
        (
            {
                key: value
                for key, value in operation.items()
                if key != "accept_fuzzy"
                or type(value) is not bool
                or operation.get("op") in ANCHOR_SCOPED_OPERATION_KINDS
            }
            if isinstance(operation, dict)
            else operation
        )
        for operation in operations
    ]


class ReplaceTextOperation(_AnchorScopedOperation):
    op: Literal["replace_text"]
    old_text: str = Field(min_length=1)
    new_text: str
    expected_occurrences: int = Field(default=1, ge=1)
    at: TextScopeAddress | None = Field(
        default=None,
        description=(
            "Where to look. Best: {by:'block', block, expected} from "
            "read_structure_map (version='my_edit'); or a section. Omit to "
            "search the whole document (exact and whitespace/Unicode-"
            "insensitive matching only)."
        ),
    )
    match_mode: Literal["exact", "safe"] = Field(
        default="safe",
        description=(
            "'safe' (default): exact first; then ignore invisible differences "
            "(line endings, Unicode form, repeated spaces, curly quotes, "
            "dashes); then, only inside a block or section `at` and for a "
            "single occurrence of 12+ characters, accept a small copy slip if "
            "exactly one place is that close. Code, URLs, link targets, HTML, "
            "math, Markdown markup, digits, identifiers and each line's "
            "indentation and list/heading marker must match exactly even "
            "here. Refuses with text_match_ambiguous / "
            "text_match_low_confidence / text_match_not_found instead of "
            "guessing. 'exact': byte-for-byte only."
        ),
    )

    @field_validator("new_text")
    @classmethod
    def _new_text_not_blank(cls, value: str) -> str:
        # I13: a whitespace-only replacement normalizes away under Auto-fix and
        # could otherwise be mistaken for a format-only no-op.
        if value and not value.strip():
            raise ValueError("new_text must not be only whitespace")
        return value


class InsertTextOperation(_AnchorScopedOperation):
    op: Literal["insert_text"]
    at: AnchorAddress
    new_text: str = Field(min_length=1, max_length=BODY_MD_MAX_LENGTH)
    side: Literal["before", "after"]

    @field_validator("new_text")
    @classmethod
    def _new_text_not_blank(cls, value: str) -> str:
        # I13.
        if not value.strip():
            raise ValueError("new_text must not be only whitespace")
        return value


class AppendDocumentOperation(_Operation):
    op: Literal["append_document"]
    content: str = Field(
        min_length=1, max_length=BODY_MD_MAX_LENGTH, description=_CONTENT_DESCRIPTION
    )

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        # I13.
        if not value.strip():
            raise ValueError("content must not be only whitespace")
        return value


class ReplaceDocumentOperation(_Operation):
    op: Literal["replace_document"]
    content: str = Field(
        min_length=1, max_length=BODY_MD_MAX_LENGTH, description=_CONTENT_DESCRIPTION
    )
    expected_content_hash: str

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        # I13.
        if not value.strip():
            raise ValueError("content must not be only whitespace")
        return value

    @field_validator("expected_content_hash")
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if _SHA256_HEX_PATTERN.fullmatch(value) is None:
            raise ValueError("expected_content_hash must be a sha256 hex digest")
        return value


class ReplaceSectionOperation(_AnchorScopedOperation):
    op: Literal["replace_section"]
    at: SectionAddress
    content: str = Field(
        min_length=1, max_length=BODY_MD_MAX_LENGTH, description=_CONTENT_DESCRIPTION
    )
    expected_section_hash: str

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        # I13.
        if not value.strip():
            raise ValueError("content must not be only whitespace")
        return value

    @field_validator("expected_section_hash")
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if _SHA256_HEX_PATTERN.fullmatch(value) is None:
            raise ValueError("expected_section_hash must be a sha256 hex digest")
        return value


class InsertSectionOperation(_AnchorScopedOperation):
    op: Literal["insert_section"]
    at: SectionAddress
    position: Literal["before", "after", "append_child"]
    title: str = Field(min_length=1)
    content: str = Field(
        min_length=1, max_length=BODY_MD_MAX_LENGTH, description=_CONTENT_DESCRIPTION
    )

    @field_validator("title")
    @classmethod
    def _title_valid(cls, value: str) -> str:
        # I13.
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("section title must be one non-blank line")
        return value

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        # I13.
        if not value.strip():
            raise ValueError("content must not be only whitespace")
        return value


class DeleteSectionOperation(_AnchorScopedOperation):
    op: Literal["delete_section"]
    at: SectionAddress
    expected_section_hash: str

    @field_validator("expected_section_hash")
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if _SHA256_HEX_PATTERN.fullmatch(value) is None:
            raise ValueError("expected_section_hash must be a sha256 hex digest")
        return value


class ReplaceBlockOperation(_Operation):
    # Block-addressed operations use an opaque block id from
    # read_structure_map and verify it against the current content hash.
    # from read_structure_map, verified against `expected` (the block's
    # current content hash) before the edit lands -- no text/anchor
    # resolution involved.
    op: Literal["replace_block"]
    block: str = Field(min_length=1)
    expected: str
    content: str = Field(
        min_length=1, max_length=BODY_MD_MAX_LENGTH, description=_CONTENT_DESCRIPTION
    )

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be only whitespace")
        return value

    @field_validator("expected")
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if _SHA256_HEX_PATTERN.fullmatch(value) is None:
            raise ValueError("expected must be a sha256 hex digest")
        return value


class InsertAfterOperation(_Operation):
    op: Literal["insert_after"]
    block: str = Field(min_length=1)
    expected: str
    content: str = Field(
        min_length=1, max_length=BODY_MD_MAX_LENGTH, description=_CONTENT_DESCRIPTION
    )

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be only whitespace")
        return value

    @field_validator("expected")
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if _SHA256_HEX_PATTERN.fullmatch(value) is None:
            raise ValueError("expected must be a sha256 hex digest")
        return value


class InsertRowOperation(_Operation):
    op: Literal["insert_row"]
    row: str = Field(min_length=1)
    expected: str
    content: str = Field(min_length=1, description=_CONTENT_DESCRIPTION)

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be only whitespace")
        return value

    @field_validator("expected")
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if _SHA256_HEX_PATTERN.fullmatch(value) is None:
            raise ValueError("expected must be a sha256 hex digest")
        return value


class DeleteBlockOperation(_Operation):
    op: Literal["delete_block"]
    block: str = Field(min_length=1)
    expected: str

    @field_validator("expected")
    @classmethod
    def _valid_hash(cls, value: str) -> str:
        if _SHA256_HEX_PATTERN.fullmatch(value) is None:
            raise ValueError("expected must be a sha256 hex digest")
        return value


type EditOperation = Annotated[
    ReplaceTextOperation
    | InsertTextOperation
    | AppendDocumentOperation
    | ReplaceDocumentOperation
    | ReplaceSectionOperation
    | InsertSectionOperation
    | DeleteSectionOperation
    | ReplaceBlockOperation
    | InsertAfterOperation
    | InsertRowOperation
    | DeleteBlockOperation,
    Field(discriminator="op"),
]


class ResolveConflictOperation(_StrictModel):
    """Explicit decision for one server-reported reconciliation conflict.

    ``block`` is always the durable base identity from ``conflicts[].block_id``.
    The server resolves any current row identity and verifies ``expected``;
    callers never submit a block map, row ordinal, or inferred diff.
    """

    op: Literal["resolve_conflict"]
    block: str = Field(
        min_length=1,
        max_length=160,
        description="The original base block/row id reported by conflicts[].block_id.",
    )
    resolution: Literal["keep_head", "restore_yours", "custom"] = Field(
        description=(
            "Retain current HEAD, replay this branch's durable authored "
            "operation, or use explicit custom content. Retired identities fail "
            "closed unless the manifest proves a deterministic insertion "
            "position; the server never guesses by ordinal."
        )
    )
    expected: str | None = Field(
        default=None,
        description=(
            "Required when the conflict still has a current HEAD target: the "
            "sha256 hash returned by the server for that target. Must be null "
            "when the identity is retired."
        ),
    )
    content: str | None = Field(
        default=None,
        max_length=BODY_MD_MAX_LENGTH,
        description="Required only for resolution=custom.",
    )

    @field_validator("expected")
    @classmethod
    def _valid_optional_hash(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_HEX_PATTERN.fullmatch(value) is None:
            raise ValueError("expected must be a sha256 hex digest")
        return value

    @model_validator(mode="after")
    def _valid_resolution_content(self):
        if self.resolution == "custom":
            if self.content is None or not self.content.strip():
                raise ValueError("custom resolution requires non-blank content")
        elif self.content is not None:
            raise ValueError("content is only allowed for a custom resolution")
        return self


type PatchDraftOperation = Annotated[
    ReplaceTextOperation
    | InsertTextOperation
    | AppendDocumentOperation
    | ReplaceDocumentOperation
    | ReplaceSectionOperation
    | InsertSectionOperation
    | DeleteSectionOperation
    | ReplaceBlockOperation
    | InsertAfterOperation
    | InsertRowOperation
    | DeleteBlockOperation
    | ResolveConflictOperation,
    Field(discriminator="op"),
]


class PatchDraftRequest(_StrictModel):
    page_id: UUID
    operations: list[PatchDraftOperation] = Field(max_length=20)
    reason: str = Field(min_length=1)
    base: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Preferred: document.version_id from read_structure_map. Omit only "
            "when this conversation has a durable version-bearing read ledger; "
            "otherwise the server returns base_required. A legacy content hash "
            "is accepted only when it identifies exactly one version on this "
            "page; duplicate hashes fail closed with base_ambiguous."
        ),
    )
    dry_run: bool = False
    needs_review: bool = False
    reconciles: list[UUID] | None = Field(
        default=None,
        max_length=1,
        description=(
            "Design doc section 6.2: resubmission after a 'diverged' push. "
            "Names the diverged branch version (from that response's "
            "alternative_draft_id) this request resolves -- `operations` "
            "must address every block listed under conflicts. For a conflict "
            "whose head.identity_retired is true, use resolve_conflict with "
            "keep_head or restore_yours instead of guessing a positional "
            "target. The server auto-carries the branch's own clean, "
            "non-conflicting contributions. At most one id today (a single "
            "value, list-shaped for forward compatibility with Flow 3)."
        ),
    )
    excluded_clean_change_ids: list[
        Annotated[str, Field(min_length=1, max_length=160)]
    ] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Optional server-reported clean_changes[].block_id values whose "
            "branch contribution must not be carried into this reconciliation. "
            "Unknown, conflicting, or HEAD-only identities fail closed."
        ),
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description=(
            "Optional retry key. When omitted, the server derives one from the "
            "conversation, this request's own content, and the effective base "
            "version, so a byte-identical "
            "retry is replayed from the idempotency ledger without ever "
            "reaching the edit engine -- a client never has to supply this by "
            "hand. Pass an explicit key only to control replay scope yourself "
            "(e.g. across conversations), and a distinct key to force an "
            "intentional repeat of an otherwise identical request. Anchor "
            "addressing is deprecated; prefer block/row IDs and hashes from "
            "read_structure_map. A keyed legacy anchor naming one unique full "
            "table row is still resolvable "
            "after Auto-fix has re-padded that row."
        ),
    )

    @model_validator(mode="after")
    def _require_operations_unless_reconciling(self):
        if not self.operations and not self.reconciles:
            raise ValueError("operations must not be empty unless reconciles is set")
        if self.excluded_clean_change_ids and not self.reconciles:
            raise ValueError(
                "excluded_clean_change_ids is only valid when reconciles is set"
            )
        if len(self.excluded_clean_change_ids) != len(
            set(self.excluded_clean_change_ids)
        ):
            raise ValueError("excluded_clean_change_ids must not contain duplicates")
        return self
