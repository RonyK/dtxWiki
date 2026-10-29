from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import re
from typing import Any
from uuid import UUID

from backend.schemas.mcp.upload_tools import MAX_ENCODED_IMAGE_CHARS
from backend.services.mcp_display_text import (
    grapheme_clusters,
    mask_secret_tokens,
    sanitize_display_lines,
)

UPLOAD_IMAGE_TOOL = "upload_image"
_WRITE_BODY_FIELDS = {
    "write_agent_page_draft": "body_markdown",
    # P8.6: create_context_page/update_context_edit carry large page content
    # under the P8 state-machine's field name (`body_md`, not
    # `body_markdown`).
    "create_context_page": "body_md",
    "update_context_edit": "body_md",
}
# A whole-body autosave re-sends the entire document every time, so its
# prefix is the same document preamble on every call and says nothing about
# what changed. It keeps the digest only; Chats reads the server's edit
# receipt (first change, changed sections) for this tool instead.
_DIGEST_ONLY_BODY_TOOLS = frozenset({"update_context_edit"})
_SUBMIT_REVISION_TOOL = "submit_revision_for_review"
_PATCH_CONTEXT_EDIT_TOOL = "patch_context_edit"
# patch_draft operations carry document content in several differently named
# fields (old_text/new_text/content/title, plus anchor_text/context_before/
# context_after when addressing by anchor) rather than one body field, so the
# single-field _WRITE_BODY_FIELDS map above cannot express its shape.
#
# This is deliberately an ALLOWLIST of known-safe metadata, not a blocklist of
# known content fields: any field on an operation or address that is not
# listed here is treated as content and reduced to a digest. That way a new
# content-bearing field added to the operation schema later is redacted by
# default, instead of silently passing through verbatim the way a blocklist
# would.
_PATCH_DRAFT_OPERATION_METADATA_FIELDS = frozenset(
    {
        "op",
        "accept_fuzzy",
        "match_mode",
        "expected_occurrences",
        "expected_content_hash",
        "expected_section_hash",
        "block",
        "row",
        "expected",
        "resolution",
        "position",
        "side",
    }
)
_PATCH_DRAFT_ADDRESS_METADATA_FIELDS = frozenset(
    {
        "by",
        "section_id",
        "heading_path",
        "occurrence",
        "char_offset_hint",
        # replace_text's block scope: an opaque id and a content hash.
        "block",
        "expected",
    }
)
_PATCH_DRAFT_OPERATION_CONTENT_FIELDS = frozenset(
    {"old_text", "new_text", "content", "title"}
)
_PATCH_DRAFT_ADDRESS_CONTENT_FIELDS = frozenset(
    {"anchor_text", "context_before", "context_after"}
)
_PATCH_DRAFT_OPERATIONS = frozenset(
    {
        "replace_text",
        "insert_text",
        "append_document",
        "replace_document",
        "replace_section",
        "insert_section",
        "delete_section",
        "replace_block",
        "insert_after",
        "insert_row",
        "delete_block",
        "resolve_conflict",
    }
)
# The operation kinds a patch may carry, for readers that summarize one.
PATCH_OPERATION_KINDS = _PATCH_DRAFT_OPERATIONS
_PATCH_CONTEXT_EDIT_TOP_LEVEL_FIELDS = frozenset(
    {
        "workspace_id",
        "page_id",
        "version_id",
        "expected_editing_revision",
        "idempotency_key",
        "retry_only",
        "conversation_token",
    }
)
_PATCH_DRAFT_POSITIONS = frozenset({"before", "after", "append_child"})
_PATCH_DRAFT_SIDES = frozenset({"before", "after"})
_PATCH_DRAFT_ADDRESS_KINDS = frozenset(
    {"section_id", "heading_path", "anchor", "block"}
)
_HEX_CHARS = frozenset("0123456789abcdef")
_UPLOAD_ARGUMENT_FIELDS = (
    "workspace_id",
    "filename",
    "mime_type",
    "marker_uuid",
    "page_id",
    "editing_version_id",
)
_UPLOAD_RESULT_FIELDS = (
    "asset_id",
    "logical_ref",
    "content_url",
    "mime_type",
    "bytes",
    "sha256",
    "marker_uuid",
    "reused",
)

# Edit previews (Chats "what did this call write"). Only the fields a call
# actually edits keep a short, masked prefix beside their digest -- the patch
# operations' old_text/new_text/content/title and the create/draft/revision
# bodies. Landmarks (anchor_text/context_before/context_after) name text the
# caller did not change, so they stay digest-only, and so does every field
# this module does not recognize. Masking is best-effort (see
# mcp_display_text); the field allowlist is the actual privacy boundary.
PREVIEW_MAX_CHARS = 300
PREVIEW_CALL_BUDGET_CHARS = 6000
PREVIEW_SUFFIX = "_preview"
PREVIEW_BUDGET_EXHAUSTED_FIELD = "preview_budget_exhausted"
PREVIEW_ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
_BYTE_ORDER_MARK = "\N{ZERO WIDTH NO-BREAK SPACE}"
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?:.*?\r?\n)?(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_LEADING_MEDIA_EMBEDS_RE = re.compile(r"\A(?:\s*!\[[^\]\n]*\]\(dtx-media://[^)\n]*\))+")
_EDIT_RECEIPT_MODES = frozenset({"patch", "whole_body", "create"})
_EDIT_RECEIPT_COUNT_FIELDS = (
    "chars_added",
    "chars_removed",
    "lines_added",
    "lines_removed",
)
_EDIT_RECEIPT_MAX_SECTIONS = 20
_EDIT_RECEIPT_MAX_OPERATIONS = 200
_EDIT_RECEIPT_MAX_HEADING_DEPTH = 12
_EDIT_RECEIPT_MAX_BLOCK_IDS = 200
_EDIT_RECEIPT_HEADING_MAX_CHARS = 200
_EDIT_RECEIPT_BLOCK_ID_MAX_CHARS = 160
_BLOCK_ID_RE = re.compile(r"[A-Za-z0-9_.:\-]+")
_BLOCK_KIND_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
# A failed edit's error details, reduced to what locates the failure: the
# code, a schema path, an operation index and -- for an overlap -- the
# two operations and their line ranges. The details' prose (message/fix)
# is never kept.
_MAX_ERROR_DETAILS = 20
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ERROR_FIELD_RE = re.compile(r"[A-Za-z0-9_.:\[\]\-]{1,160}")
_OPERATION_FIELD_RE = re.compile(r"operations(?:\[(\d+)\]|\.(\d+))")
# The one server-written format parsed here: context_editing's
# _overlap_error message. The e2e Chats test pins it against a real
# overlapping patch, so a format change fails there, not silently.
_OVERLAP_MESSAGE_RE = re.compile(
    r"operations\[(\d+)\] \([a-z_]+, lines? (\d+)(?:-(\d+))?\) and "
    r"operations\[(\d+)\] \([a-z_]+, lines? (\d+)(?:-(\d+))?\) "
    r"target overlapping text: ([a-z ]+)"
)
_OVERLAP_KINDS = {
    "same range": "same_range",
    "partial overlap": "partial",
    "one range contains the other": "contains",
    "insertion point on a range boundary": "insertion_boundary",
}


class _PreviewBudget:
    """Per-call cap on preview text so a long batch cannot store a document.

    Once a preview does not fit, it and every later field keep only their
    digest; the count lands on the summary as ``preview_budget_exhausted``.
    Skipping everything after the first miss (rather than squeezing in later,
    shorter previews) keeps the stored previews a contiguous prefix of the
    batch, which is what a "N more not shown" note describes.
    """

    def __init__(self) -> None:
        self.remaining = PREVIEW_CALL_BUDGET_CHARS
        self.exhausted = 0

    def take(self, value: str, *, body: bool) -> dict[str, Any] | None:
        if self.exhausted:
            self.exhausted += 1
            return None
        preview = text_preview(value, body=body)
        if len(preview["text"]) > self.remaining:
            self.exhausted += 1
            return None
        self.remaining -= len(preview["text"])
        return preview

    def stamp(self, summary: dict[str, Any]) -> None:
        if self.exhausted:
            summary[PREVIEW_BUDGET_EXHAUSTED_FIELD] = self.exhausted


def text_preview(
    value: str,
    *,
    body: bool = False,
    limit: int = PREVIEW_MAX_CHARS,
) -> dict[str, Any]:
    """Masked prefix of ``value`` as ``{"text", "truncated"}``.

    ``body`` strips what every document starts with rather than what it says:
    YAML frontmatter and leading ``dtx-media://`` image embeds. The cut lands
    on a grapheme boundary and, when anything was dropped, ends in an
    ellipsis. Line breaks survive (CRLF/CR as LF) so lists, tables and code
    stay readable; credentials are masked over the whole text first, so one
    split across a line break is still caught. Sanitizing collapses spaces
    and drops URL tails, so a window somewhat larger than ``limit`` is read;
    the rest of a large body is never scanned.
    """

    text = _strip_body_preamble(value) if body else value
    window = text[: limit * 4]
    safe = mask_secret_tokens(sanitize_display_lines(mask_secret_tokens(window)))
    kept: list[str] = []
    kept_chars = 0
    cut = False
    for cluster in grapheme_clusters(safe):
        if kept_chars + len(cluster) > limit:
            cut = True
            break
        kept.append(cluster)
        kept_chars += len(cluster)
    truncated = cut or len(text) > len(window)
    preview = "".join(kept)
    if truncated:
        preview = preview.rstrip() + PREVIEW_ELLIPSIS
    return {"text": preview, "truncated": truncated}


def _strip_body_preamble(value: str) -> str:
    text = value.removeprefix(_BYTE_ORDER_MARK)
    text = _FRONTMATTER_RE.sub("", text, count=1)
    return _LEADING_MEDIA_EMBEDS_RE.sub("", text, count=1).lstrip()


class _RedactedToolArguments(dict[str, Any]):
    """In-process trust marker for a summary produced by this module.

    JSON-RPC input always arrives as a plain ``dict``, so callers cannot forge
    this marker by supplying another argument key. Keeping the marker in the
    Python type (rather than in the durable JSON object) lets repeated capture
    layers preserve derived fields without ever trusting caller-provided
    digest or size values.
    """


def redact_tool_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the only argument summary durable capture may receive."""

    # MCP-07: capture invokes this repeatedly. Only summaries created here may
    # carry derived bytes/digest fields across those passes. A raw JSON-RPC
    # caller can add fields named ``bytes`` and ``sha256`` even though Pydantic
    # later rejects them; treating those names as an idempotence signal would
    # let a failed upload forge its durable forensic fingerprint.
    if isinstance(arguments, _RedactedToolArguments):
        return copy.deepcopy(arguments)

    body_field = _WRITE_BODY_FIELDS.get(tool_name)
    if body_field is not None:
        budget = _PreviewBudget()
        summary = _redact_body_field(
            arguments,
            body_field,
            budget=None if tool_name in _DIGEST_ONLY_BODY_TOOLS else budget,
        )
        summary.pop(PREVIEW_BUDGET_EXHAUSTED_FIELD, None)
        budget.stamp(summary)
        return summary
    if tool_name == _SUBMIT_REVISION_TOOL:
        summary = _RedactedToolArguments(copy.deepcopy(arguments))
        summary.pop(PREVIEW_BUDGET_EXHAUSTED_FIELD, None)
        budget = _PreviewBudget()
        for revision_field in ("update", "create"):
            revision = summary.get(revision_field)
            if isinstance(revision, dict):
                summary[revision_field] = _redact_body_field(
                    revision,
                    "body_markdown",
                    marked=False,
                    budget=budget,
                )
        budget.stamp(summary)
        return summary
    if tool_name == _PATCH_CONTEXT_EDIT_TOOL:
        return _redact_patch_context_edit(arguments)
    if tool_name != UPLOAD_IMAGE_TOOL:
        return copy.deepcopy(arguments)

    summary = _RedactedToolArguments(
        {
            field: copy.deepcopy(arguments[field])
            for field in _UPLOAD_ARGUMENT_FIELDS
            if field in arguments
        }
    )
    encoded = arguments.get("content_base64")
    if isinstance(encoded, str) and encoded:
        summary = _RedactedToolArguments(_scrub(summary, secret=encoded))
    if isinstance(encoded, str) and len(encoded) <= MAX_ENCODED_IMAGE_CHARS:
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            pass
        else:
            summary["bytes"] = len(decoded)
            summary["sha256"] = hashlib.sha256(decoded).hexdigest()

    data = (payload or {}).get("data")
    if isinstance(data, dict):
        for field in _UPLOAD_RESULT_FIELDS:
            if field in data:
                summary[field] = copy.deepcopy(data[field])
    return summary


def _redact_body_field(
    arguments: dict[str, Any],
    body_field: str,
    *,
    marked: bool = True,
    budget: _PreviewBudget | None,
) -> dict[str, Any]:
    """Replace ``body_field`` with its digest and, given a budget, a preview.

    The preview is computed here, while the raw body is still in scope: a
    summary this module already produced short-circuits on re-entry
    (``_RedactedToolArguments``), so a later pass could neither add it nor be
    trusted to. Caller-sent values under the derived names are dropped first.
    """

    summary_type = _RedactedToolArguments if marked else dict
    summary = summary_type(copy.deepcopy(arguments))
    digest_field = f"{body_field}_digest"
    preview_field = f"{body_field}{PREVIEW_SUFFIX}"
    summary.pop(digest_field, None)
    summary.pop(preview_field, None)
    body = summary.pop(body_field, None)
    if isinstance(body, str):
        encoded = body.encode("utf-8")
        summary[digest_field] = {
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "chars": len(body),
            "bytes": len(encoded),
        }
        if budget is not None:
            preview = budget.take(body, body=True)
            if preview is not None:
                summary[preview_field] = preview
    return summary


def _redact_patch_context_edit(arguments: dict[str, Any]) -> dict[str, Any]:
    # patch_context_edit's `operations` reuse the same typed union of
    # block/section edit operations (minus resolve_conflict, which never
    # appears here), so the operation/address redaction below (originally
    # written for the now-deregistered patch_draft MCP tool) is shared
    # verbatim; only the
    # top-level field allowlist differs (no reason/base/dry_run/needs_review/
    # reconciles -- this tool always targets the caller's own editing row).
    summary = _RedactedToolArguments()
    unknown_fields: list[dict[str, Any]] = []
    for field, value in arguments.items():
        if field == "operations":
            continue
        if field in _PATCH_CONTEXT_EDIT_TOP_LEVEL_FIELDS:
            if _valid_patch_context_edit_top_level_metadata(field, value):
                summary[field] = copy.deepcopy(value)
            else:
                summary[f"{field}_digest"] = _value_digest(value)
            continue
        unknown_fields.append(_unknown_field_digest(field, value))

    operations = arguments.get("operations")
    budget = _PreviewBudget()
    if isinstance(operations, list):
        summary["operations"] = [
            _redact_patch_draft_operation(operation, budget)
            if isinstance(operation, dict)
            else {"invalid_operation_digest": _canonical_value_digest(operation)}
            for operation in operations
        ]
    elif "operations" in arguments:
        summary["operations_digest"] = _canonical_value_digest(operations)
    if unknown_fields:
        summary["unknown_fields"] = unknown_fields
    budget.stamp(summary)
    return summary


def _valid_patch_context_edit_top_level_metadata(field: str, value: Any) -> bool:
    if field in {"workspace_id", "page_id", "version_id"}:
        if not isinstance(value, (str, UUID)):
            return False
        try:
            UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            return False
        return True
    if field == "expected_editing_revision":
        return type(value) is int and value >= 1
    if field == "retry_only":
        return type(value) is bool
    if field in {"idempotency_key", "conversation_token"}:
        return value is None or (isinstance(value, str) and 1 <= len(value) <= 160)
    return False


def _redact_patch_draft_operation(
    operation: dict[str, Any],
    budget: _PreviewBudget,
) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    unknown_fields: list[dict[str, Any]] = []
    # replace_document's content is a whole document, so its preview skips the
    # same preamble as the create/draft bodies.
    whole_body = operation.get("op") == "replace_document"
    for field, value in operation.items():
        if field == "at":
            if isinstance(value, dict):
                redacted[field] = _redact_patch_draft_address(value)
            else:
                redacted["at_digest"] = _value_digest(value)
            continue
        if field in _PATCH_DRAFT_OPERATION_METADATA_FIELDS and (
            _valid_patch_operation_metadata(field, value)
        ):
            redacted[field] = copy.deepcopy(value)
            continue
        if (
            field in _PATCH_DRAFT_OPERATION_METADATA_FIELDS
            or field in _PATCH_DRAFT_OPERATION_CONTENT_FIELDS
        ):
            redacted[f"{field}_digest"] = _value_digest(value)
            if field in _PATCH_DRAFT_OPERATION_CONTENT_FIELDS and isinstance(
                value, str
            ):
                preview = budget.take(value, body=whole_body and field == "content")
                if preview is not None:
                    redacted[f"{field}{PREVIEW_SUFFIX}"] = preview
            continue
        unknown_fields.append(_unknown_field_digest(field, value))
    if unknown_fields:
        redacted["unknown_fields"] = unknown_fields
    return redacted


def _redact_patch_draft_address(address: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    unknown_fields: list[dict[str, Any]] = []
    for field, value in address.items():
        if field in _PATCH_DRAFT_ADDRESS_METADATA_FIELDS and (
            _valid_patch_address_metadata(field, value)
        ):
            redacted[field] = copy.deepcopy(value)
            continue
        if (
            field in _PATCH_DRAFT_ADDRESS_METADATA_FIELDS
            or field in _PATCH_DRAFT_ADDRESS_CONTENT_FIELDS
        ):
            redacted[f"{field}_digest"] = _value_digest(value)
            continue
        unknown_fields.append(_unknown_field_digest(field, value))
    if unknown_fields:
        redacted["unknown_fields"] = unknown_fields
    return redacted


# Server-minted block/row ids (backend/utils/block_identity.py): random
# 128-bit or seeded 256-bit hex. Anything else a caller sends in an id field
# -- including free text -- is content, so it is digested like content.
_MINTED_ID_RE = re.compile(r"[br]_(?:[0-9a-f]{32}|[0-9a-f]{64})")


def _is_minted_id(value: Any) -> bool:
    return isinstance(value, str) and _MINTED_ID_RE.fullmatch(value) is not None


def _valid_patch_operation_metadata(field: str, value: Any) -> bool:
    if field == "op":
        return isinstance(value, str) and value in _PATCH_DRAFT_OPERATIONS
    if field == "accept_fuzzy":
        return type(value) is bool
    if field == "match_mode":
        return isinstance(value, str) and value in {"exact", "safe"}
    if field == "expected_occurrences":
        return type(value) is int and value >= 1
    if field in {"expected_content_hash", "expected_section_hash"}:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(char in _HEX_CHARS for char in value)
        )
    if field in {"block", "row"}:
        return _is_minted_id(value)
    if field == "expected":
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(char in _HEX_CHARS for char in value)
        )
    if field == "position":
        return isinstance(value, str) and value in _PATCH_DRAFT_POSITIONS
    if field == "side":
        return isinstance(value, str) and value in _PATCH_DRAFT_SIDES
    if field == "resolution":
        return isinstance(value, str) and value in {"keep_head", "restore_yours"}
    return False


def _valid_patch_address_metadata(field: str, value: Any) -> bool:
    if field == "by":
        return isinstance(value, str) and value in _PATCH_DRAFT_ADDRESS_KINDS
    if field == "section_id":
        return isinstance(value, str) and bool(value)
    if field == "block":
        return _is_minted_id(value)
    if field == "expected":
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(char in _HEX_CHARS for char in value)
        )
    if field == "heading_path":
        if isinstance(value, str):
            return bool(value.strip())
        return (
            isinstance(value, (list, tuple))
            and bool(value)
            and all(isinstance(part, str) and bool(part.strip()) for part in value)
        )
    if field == "occurrence":
        return type(value) is int and value >= 1
    if field == "char_offset_hint":
        return type(value) is int and value >= 0
    return False


def _text_digest(value: str) -> dict[str, Any]:
    """Reduce a string content field to the same sha256/chars/bytes shape used
    for write bodies elsewhere in this module (see ``_redact_body_field``),
    dropping the original field the way that helper drops ``body_field``.
    """

    encoded = value.encode("utf-8")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "chars": len(value),
        "bytes": len(encoded),
    }


def _value_digest(value: Any) -> dict[str, Any]:
    """Digest an unknown field without retaining any nested raw value.

    MCP arguments are JSON values, but malformed requests are traced before
    Pydantic rejects them. Restricting the old helper to strings therefore let
    an unknown object/list survive verbatim in a durable validation-failure
    trace. Canonical JSON keeps a stable forensic fingerprint while failing
    closed for every JSON type.
    """

    if isinstance(value, str):
        return _text_digest(value)
    return _canonical_value_digest(value)


def _unknown_field_digest(field: Any, value: Any) -> dict[str, Any]:
    """Fingerprint an unrecognized pair without retaining its JSON key."""

    return {
        "key_digest": _canonical_value_digest(field),
        "value_digest": _canonical_value_digest(value),
    }


def _canonical_value_digest(value: Any) -> dict[str, Any]:
    """Hash the canonical JSON representation of an untrusted JSON value."""

    try:
        encoded_text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        # JSON-RPC cannot carry such a value, but direct in-process callers can.
        # Preserve fail-closed behavior without hashing repr(value), which may
        # itself contain the secret this boundary exists to remove.
        encoded_text = f"<non-json:{type(value).__module__}.{type(value).__qualname__}>"
    encoded = encoded_text.encode("utf-8")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "chars": len(encoded_text),
        "bytes": len(encoded),
    }


def redact_edit_receipt(receipt: Any) -> dict[str, Any] | None:
    """Allowlisted, masked copy of a context-edit response's ``edit_receipt``.

    The receipt is server-produced, but it quotes document text (the replaced
    regions and heading paths), so it crosses the same boundary as caller
    input before it is stored: known keys only, typed values only, excerpts
    re-cut and masked like any other preview. Of the regions only the first is
    kept, as the Chats ``first_change`` preview. Returns ``None`` for anything
    that is not a receipt. Idempotent.
    """

    if not isinstance(receipt, dict):
        return None
    edit_mode = receipt.get("edit_mode")
    if edit_mode not in _EDIT_RECEIPT_MODES:
        return None
    redacted: dict[str, Any] = {"edit_mode": edit_mode}
    for field in ("editing_revision_before", "editing_revision_after"):
        value = receipt.get(field)
        redacted[field] = value if _is_count(value) else None
    for field in _EDIT_RECEIPT_COUNT_FIELDS:
        value = receipt.get(field)
        redacted[field] = value if _is_count(value) else None
    sections = receipt.get("changed_sections")
    redacted["changed_sections"] = [
        path
        for path in (
            _receipt_heading_path(section)
            for section in _sequence(sections)[:_EDIT_RECEIPT_MAX_SECTIONS]
        )
        if path
    ]
    redacted["first_change"] = _receipt_first_change(
        receipt["first_change"]
        if "first_change" in receipt
        else _first_region(receipt.get("changes"))
    )
    operations = receipt.get("operations")
    redacted["operations"] = [
        operation
        for operation in (
            _receipt_operation(item)
            for item in _sequence(operations)[:_EDIT_RECEIPT_MAX_OPERATIONS]
        )
        if operation is not None
    ]
    blocks = receipt.get("changed_blocks")
    redacted["changed_blocks"] = [
        block
        for block in (
            _receipt_block(item)
            for item in _sequence(blocks)[:_EDIT_RECEIPT_MAX_BLOCK_IDS]
        )
        if block is not None
    ]
    return redacted


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _sequence(value: Any) -> list[Any]:
    # In-process payloads may still hold tuples (e.g. a line range) that JSON
    # would have turned into lists.
    return list(value) if isinstance(value, (list, tuple)) else []


def _receipt_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return text_preview(value, limit=limit)["text"]


def _receipt_heading_path(value: Any) -> list[str]:
    path: list[str] = []
    for part in _sequence(value)[:_EDIT_RECEIPT_MAX_HEADING_DEPTH]:
        text = _receipt_text(part, limit=_EDIT_RECEIPT_HEADING_MAX_CHARS)
        if text:
            path.append(text)
    return path


def _first_region(changes: Any) -> dict[str, Any] | None:
    """The first replaced region as a before/after preview pair.

    Regions are listed as a prefix in document order, so the first one listed
    is the edit's first change; with none listed there is no preview.
    """

    regions = _sequence(changes)
    if not regions or not isinstance(regions[0], dict):
        return None
    region = regions[0]
    parts = [
        region.get(key)
        for key in ("context_before", "removed", "added", "context_after")
    ]
    if not all(isinstance(part, str) for part in parts):
        return None
    before, removed, added, after = parts
    return {
        "line": region.get("line"),
        "before_preview": before + removed + after,
        "after_preview": before + added + after,
    }


def _receipt_first_change(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not _is_count(value.get("line")):
        return None
    return {
        "line": value["line"],
        "before_preview": _receipt_text(
            value.get("before_preview"), limit=PREVIEW_MAX_CHARS
        ),
        "after_preview": _receipt_text(
            value.get("after_preview"), limit=PREVIEW_MAX_CHARS
        ),
    }


def _receipt_operation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    index = value.get("index")
    op = value.get("op")
    if not _is_count(index) or op not in _PATCH_DRAFT_OPERATIONS:
        return None
    line_range = _sequence(value.get("line_range"))
    operation: dict[str, Any] = {
        "index": index,
        "op": op,
        "line_range": (
            line_range
            if len(line_range) == 2 and all(_is_count(bound) for bound in line_range)
            else None
        ),
        "heading_path": _receipt_heading_path(value.get("heading_path")) or None,
        "affected_block_ids": [
            block_id
            for block_id in _sequence(value.get("affected_block_ids"))[
                :_EDIT_RECEIPT_MAX_BLOCK_IDS
            ]
            if _is_block_id(block_id)
        ],
        "chars_added": (
            value["chars_added"] if _is_count(value.get("chars_added")) else None
        ),
        "chars_removed": (
            value["chars_removed"] if _is_count(value.get("chars_removed")) else None
        ),
    }
    # replace_text only: which matcher tier found the target, and for a
    # forgiven copy slip how close it was. Absent for every other operation.
    if value.get("match_kind") in {"exact", "normalized", "fuzzy"}:
        operation["match_kind"] = value["match_kind"]
    similarity = value.get("match_similarity")
    if type(similarity) is float and 0 <= similarity <= 1:
        operation["match_similarity"] = similarity
    matched = value.get("matched_sha256")
    if (
        isinstance(matched, str)
        and len(matched) == 64
        and all(char in _HEX_CHARS for char in matched)
    ):
        operation["matched_sha256"] = matched
    return operation


def _is_block_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _EDIT_RECEIPT_BLOCK_ID_MAX_CHARS
        and _BLOCK_ID_RE.fullmatch(value) is not None
    )


def _receipt_block(value: Any) -> dict[str, str] | None:
    # A changed block's new address (id, kind, content hash) lets a client
    # chain block patches without re-reading; it names no text.
    if not isinstance(value, dict):
        return None
    block_id, kind, block_hash = value.get("id"), value.get("kind"), value.get("hash")
    if not (
        _is_block_id(block_id)
        and isinstance(kind, str)
        and _BLOCK_KIND_RE.fullmatch(kind)
        and isinstance(block_hash, str)
        and len(block_hash) == 64
        and all(char in _HEX_CHARS for char in block_hash)
    ):
        return None
    return {"id": block_id, "kind": kind, "hash": block_hash}


def redact_error_details(details: Any) -> list[dict[str, Any]]:
    """Structural core of an error envelope's ``error_details``.

    Keeps each detail's code, its schema path when that is a plain path, the
    operation index (stated, or else named by the path), for
    ``operations_overlap`` the pair of operations with their line ranges, and
    for a refused text match its nearest candidates' positions. ``message``
    and ``fix`` are server prose about the caller's request and are dropped.
    """

    redacted: list[dict[str, Any]] = []
    for item in _sequence(details)[:_MAX_ERROR_DETAILS]:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if not isinstance(code, str) or not _ERROR_CODE_RE.fullmatch(code):
            continue
        field = item.get("field")
        if not isinstance(field, str) or not _ERROR_FIELD_RE.fullmatch(field):
            field = None
        operation = _OPERATION_FIELD_RE.match(field) if field else None
        stated_index = item.get("operation_index")
        entry: dict[str, Any] = {
            "code": code,
            "field": field,
            "operation_index": (
                stated_index
                if _is_count(stated_index)
                else (
                    int(operation.group(1) or operation.group(2))
                    if operation is not None
                    else None
                )
            ),
            "conflict": (
                _overlap_conflict(item.get("message"))
                if code == "operations_overlap"
                else None
            ),
        }
        candidates = [
            candidate
            for candidate in (
                _match_candidate(value)
                for value in _sequence(item.get("candidates"))[:_MAX_MATCH_CANDIDATES]
            )
            if candidate is not None
        ]
        if candidates:
            entry["candidates"] = candidates
        redacted.append(entry)
    return redacted


_MAX_MATCH_CANDIDATES = 3


def _match_candidate(value: Any) -> dict[str, Any] | None:
    # Where a refused replace_text nearly matched: lines, block id, a score.
    # Positions only -- the matcher never puts candidate text here.
    if not isinstance(value, dict):
        return None
    line_start, line_end = value.get("line_start"), value.get("line_end")
    similarity = value.get("similarity")
    kind = value.get("match_kind")
    if not (
        _is_count(line_start)
        and _is_count(line_end)
        and line_start >= 1
        and line_end >= line_start
        and type(similarity) in (int, float)
        and 0 <= similarity <= 1
        and kind in {"exact", "normalized", "fuzzy"}
    ):
        return None
    block_id = value.get("block_id")
    return {
        "line_start": line_start,
        "line_end": line_end,
        "block_id": block_id if _is_block_id(block_id) else None,
        "similarity": float(similarity),
        "match_kind": kind,
    }


def _overlap_conflict(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, str):
        return None
    match = _OVERLAP_MESSAGE_RE.search(message)
    if match is None:
        return None
    left, left_first, left_last, right, right_first, right_last, kind = (
        match.groups()
    )
    return {
        "left_op_index": int(left),
        "right_op_index": int(right),
        "left_lines": [int(left_first), int(left_last or left_first)],
        "right_lines": [int(right_first), int(right_last or right_first)],
        "overlap_kind": _OVERLAP_KINDS.get(kind.strip()),
    }


def null_previews(value: Any) -> tuple[Any, int]:
    """Copy of a stored summary/receipt with every preview excerpt nulled.

    Retention deletes the text but keeps the key, so a reader can tell "this
    call's preview was deleted" from "this call never had one". Digests,
    counts and heading paths stay: they are the audit record. Returns the
    copy and how many excerpts were cleared.
    """

    cleared = 0

    def visit(node: Any) -> Any:
        nonlocal cleared
        if isinstance(node, list):
            return [visit(item) for item in node]
        if not isinstance(node, dict):
            return node
        result: dict[str, Any] = {}
        for key, item in node.items():
            if _is_preview_key(key) and item is not None:
                cleared += 1
                result[key] = None
            else:
                result[key] = visit(item)
        return result

    return visit(value), cleared


def _is_preview_key(key: Any) -> bool:
    return isinstance(key, str) and key.endswith(PREVIEW_SUFFIX)


def redact_tool_capture(
    tool_name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Defense-in-depth redaction for trace, audit, and Chat capture paths."""

    redacted_arguments = redact_tool_arguments(
        tool_name,
        arguments,
        payload=payload,
    )
    if tool_name != UPLOAD_IMAGE_TOOL:
        return redacted_arguments, copy.deepcopy(payload)

    secret = arguments.get("content_base64")
    redacted_payload = _scrub(copy.deepcopy(payload), secret=secret)
    envelope = redacted_payload.get("envelope")
    if isinstance(envelope, dict):
        params_summary = envelope.get("params_summary")
        if isinstance(params_summary, dict):
            envelope["params_summary"] = _scrub(params_summary, secret=secret)
    return redacted_arguments, redacted_payload


def _scrub(value: Any, *, secret: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _scrub(item, secret=secret)
            for key, item in value.items()
            if key != "content_base64"
        }
    if isinstance(value, list):
        return [_scrub(item, secret=secret) for item in value]
    if isinstance(value, str):
        if isinstance(secret, str) and secret and secret in value:
            return value.replace(secret, "[redacted image bytes]")
        if value.lstrip().lower().startswith("data:image/"):
            return "[redacted data image]"
    return value
