from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, ValidationError

from backend.policy.auth import ActorContext
from backend.schemas.common import (
    EnvelopeWarning,
    MCPErrorDetail,
    MCPResultEnvelope,
)

# Cap the per-field list so one malformed payload cannot return hundreds of
# entries; the count of the remainder is reported instead.
MAX_ERROR_DETAILS = 10

# How many details the assembled message names before it starts counting.
NAMED_IN_MESSAGE = 3

# A field name is whatever JSON key the caller wrote, so for `extra_forbidden`
# it is untrusted text that reaches the response, the persisted trace and the
# Chats timeline. Three 300 KB keys produced a 900 KB `result_summary`.
MAX_FIELD_CHARS = 80
MAX_DETAIL_MESSAGE_CHARS = 200
MAX_ERROR_MESSAGE_CHARS = 600

# What to do next, per failure code. Mirrors ADVISORY_WARNING_MESSAGES: stable
# English written for the agent that has to recover, not for a log reader.
#
# None of these name an envelope field. Guidance is included in error_message
# even when a call site supplies no message or details. result_summary keeps
# the same cause and recovery text for the trace.
ERROR_CODE_GUIDANCE = {
    "validation_failed": (
        "Fix the fields named in this failure, then call the tool again."
    ),
    "idempotency_key_conflict": (
        "Use a new idempotency_key for different arguments; do not retry unchanged."
    ),
    # Never "resend with the revision this failure names": a revision that
    # moved means something else saved -- possibly the user, in the browser --
    # and a blind resend overwrites exactly that work.
    "stale_base_version": (
        "What you based this on changed since you read it, so nothing was "
        "saved. Re-read before retrying: your own open edit with "
        "read_structure_map or read_context_outline (version='my_edit'; "
        "error_details name its current version_id and editing_revision), "
        "otherwise the page or the latest_version_id this failure names. "
        "Rebuild your change on what you read and send it with the values "
        "from that read; do not resend the same content with only the "
        "revision or base swapped in."
    ),
    "base_not_found": (
        "Call read_structure_map to get the page's current root_hash/version_id "
        "and use that as base, then send a new request."
    ),
    "reconcile_target_invalid": (
        "reconciles must name the alternative_draft_id from an earlier diverged "
        "response on this page, and every operation must be "
        "block-addressed; fix the request and resend."
    ),
    "text_occurrence_mismatch": (
        "old_text matched a different number of times than expected_occurrences. "
        "error_details names the operation and the matching lines (and blocks); "
        "set expected_occurrences to that count, or scope the operation with "
        "at={by:'block', block, expected} from read_structure_map, then send a "
        "new request."
    ),
    # read_structure_map previews only a block's first line, so it cannot be
    # the place old_text is copied from.
    "text_match_not_found": (
        "old_text is not in the addressed text, even ignoring whitespace and "
        "Unicode differences. error_details lists the nearest lines and blocks "
        "(not applied). Read the text with read_context_outline "
        "(version='my_edit', its section selected), copy old_text from it, "
        "and address its block with at={by:'block', block, expected} (id and "
        "hash from read_structure_map); a small copy slip is then forgiven."
    ),
    "text_match_ambiguous": (
        "More than one place is about as close to old_text, so nothing was "
        "changed. Pick one of the blocks in error_details and resend with "
        "at={by:'block', block, expected}, or lengthen old_text until only one "
        "place matches."
    ),
    "text_match_low_confidence": (
        "The closest text differs in a way that is never guessed (a number, an "
        "identifier, code, a URL, a line or hard break, or too many "
        "characters); the message says which. Copy old_text exactly from the "
        "text in error_details (read_context_outline with version='my_edit' "
        "returns it), then send a new request."
    ),
    "section_not_found": (
        "The section address did not resolve. A heading_path lists every "
        "heading from the top of the page, the # title included. Call "
        "read_context_outline (version='my_edit' for your open edit) for "
        "current section ids and heading paths, then send a new request."
    ),
    "section_hash_mismatch": (
        "The section changed since you read it. Call read_context_outline for "
        "its current hash, then send a new request."
    ),
    "block_not_found": (
        "Call read_structure_map to get current block ids, then send a new request."
    ),
    "block_hash_mismatch": (
        "Call read_structure_map to get the block's current hash and content, then "
        "send a new request."
    ),
    "row_not_found": (
        "Call read_structure_map to get current row ids, then send a new request."
    ),
    "row_hash_mismatch": (
        "Call read_structure_map to get the row's current hash and content, then "
        "send a new request."
    ),
    "anchor_confidence_low": (
        "The anchor was found only far from char_offset_hint. Prefer a block "
        "address from read_structure_map; set accept_fuzzy only if you have "
        "checked that distant match."
    ),
    "anchor_not_found": (
        "The anchor text was not found. Address the operation by block from "
        "read_structure_map, or by section from read_context_outline, then "
        "send a new request."
    ),
    "section_position_invalid": (
        "Target a heading section for insert_section, then send a new request."
    ),
    "heading_level_invalid": (
        "Choose a parent above level six, or insert the section as a sibling."
    ),
    "operation_unsupported": (
        "This operation or combination is not supported as sent (for example "
        "block-addressed and text-addressed operations mixed in one call). "
        "Change the request as the message says, then send a new request."
    ),
    "operations_overlap": (
        "The operations named in error_details target overlapping text in the "
        "same snapshot. Merge those into one operation or send them as "
        "separate calls; the other operations can stay in the batch. A block "
        "replaced or deleted in a call cannot also be inserted after in it."
    ),
    "block_reflow": (
        "The new content changed how a block you did not target parses. Make "
        "it a complete block (close code fences and HTML blocks, end with a "
        "blank line) or include the neighbouring block in the replacement, "
        "then send a new request."
    ),
    "body_length_exceeded": (
        "Reduce the replacement or inserted content below the body limit, then send "
        "a new request."
    ),
    "stale_context_draft": (
        "Re-read Context history and publish only the latest linear draft after "
        "its required review is approved."
    ),
    "rejected_by_policy": (
        "Workspace policy blocks this call as sent. Do not retry unchanged."
    ),
    "page_name_occupied": (
        "Re-issue the call against a page_id you are allowed to edit, or ask "
        "an authorized editor for access. Do not retry unchanged."
    ),
    "disambiguation_required": (
        "Re-issue the call against a selected candidate page_id. Do not retry "
        "with the bare name."
    ),
    "not_found": (
        "The target does not exist in this workspace. Re-resolve it with search "
        "or get_index."
    ),
    "workspace_role_insufficient": (
        "This actor lacks the required role. Do not retry; ask a human to grant it."
    ),
    "mcp_connection_disabled": (
        "This connection was disabled from Settings > Access for this workspace. "
        "Do not retry; ask a human to re-enable it."
    ),
    "scope_denied": (
        "The token is missing a required scope. Do not retry; request a token "
        "with the scope."
    ),
    "mcp_client_disabled": (
        "A workspace owner must reactivate this MCP client before retrying."
    ),
    "mcp_client_deleted": (
        "This credential binding cannot be restored. Authorize a new credential."
    ),
    # Names the field rather than pointing at this response's copy: the token
    # minted for a rejected call was never persisted, so echoing it back is not
    # the recovery.
    "conversation_token_required": (
        "Send conversation_token: the token returned by an earlier successful "
        'call in this conversation, or the literal "new" to start one.'
    ),
    "revision_submission_required": (
        "Call submit_revision_for_review with review_item_id to revise the "
        "active change request."
    ),
    "embedded_image_requires_upload": (
        "Call upload_image first and store data.markdown or data.logical_ref. "
        "For a new page, create it (page-first) before uploading -- "
        "upload_image always requires an existing page_id and your own open "
        "editing_version_id, never a page-less upload. Never persist "
        "data.content_url or another /api/v1 transport URL, and never fall "
        "back to a data: URI."
    ),
    "media_content_mismatch": (
        "Provide a complete PNG, JPEG, GIF, or WebP whose bytes match its "
        "declared mime_type, then use a new marker_uuid."
    ),
    "media_install_in_progress": (
        "An identical media install is still active. Wait, then retry the same call."
    ),
    "legacy_media_cutover_conflict": (
        "This Workspace has completed legacy media cutover. Remove or migrate "
        "the historical upload reference before retrying a lifecycle change."
    ),
    "media_install_unavailable": (
        "Media storage is temporarily unavailable. Retry the same call later."
    ),
    "media_owner_mismatch": (
        "Use only a page owned by this actor, and use a new marker_uuid for a "
        "different target. Do not retry unchanged."
    ),
    "media_policy_changed": (
        "Refresh the current media policy and preflight guidance with guide, "
        "then upload under that policy with a new marker_uuid. Do not retry "
        "the stale request unchanged."
    ),
    "media_quota_exceeded": (
        "Free unused media or ask a workspace administrator to increase the "
        "quota before uploading again. Do not retry unchanged."
    ),
    "media_editing_not_open": (
        "The named editing_version_id is no longer open (already submitted, "
        "published, or discarded). Open or resume your own editing session on "
        "this page, then upload_image against its current editing_version_id "
        "with a new marker_uuid. Do not retry unchanged."
    ),
    "media_staging_bytes_exceeded": (
        "The active staging byte budget is busy. Wait for active uploads to "
        "finish, then retry the same call."
    ),
    "media_staging_limit_exceeded": (
        "The active staging item limit is busy. Wait for active uploads to "
        "finish, then retry the same call."
    ),
    "media_state_changed": (
        "The marker_uuid conflicts with durable media state. Re-read the target "
        "and use a new marker_uuid; do not retry unchanged."
    ),
    "media_too_large": (
        "Reduce the decoded image below the advertised byte limit and upload it "
        "with a new marker_uuid. Do not retry unchanged."
    ),
    "media_type_unsupported": (
        "Convert the image to PNG, JPEG, GIF, or WebP and upload it with a new "
        "marker_uuid. Do not retry unchanged."
    ),
    "media_unavailable": (
        "Context media is temporarily unavailable. Retry the same call later."
    ),
    "workspace_plane_unavailable": (
        "This workspace's own database is temporarily unreachable. Retry the "
        "same call later; other workspaces are unaffected."
    ),
    "grant_replica_missing": (
        "This credential's access to this workspace has not finished syncing "
        "yet (or was just revoked). Retry the same call shortly; other "
        "workspaces this credential can reach are unaffected."
    ),
}

# Codes where resending the identical request can never succeed. Everything
# else stays None (unknown) rather than claiming a retry is safe.
RETRYABLE_BY_CODE = {
    "validation_failed": False,
    "idempotency_key_conflict": False,
    "rejected_by_policy": False,
    "page_name_occupied": False,
    "disambiguation_required": False,
    "workspace_role_insufficient": False,
    "mcp_connection_disabled": False,
    "scope_denied": False,
    "mcp_client_disabled": False,
    "mcp_client_deleted": False,
    "not_found": False,
    # False despite being recoverable: an identical request still has no token.
    # Recovery adds an earlier envelope token or the "new" sentinel.
    "conversation_token_required": False,
    "revision_submission_required": False,
    "embedded_image_requires_upload": False,
    "media_content_mismatch": False,
    "media_install_in_progress": True,
    "media_install_unavailable": True,
    "media_owner_mismatch": False,
    "media_policy_changed": False,
    "media_quota_exceeded": False,
    "media_editing_not_open": False,
    "media_staging_bytes_exceeded": True,
    "media_staging_limit_exceeded": True,
    "media_state_changed": False,
    "media_too_large": False,
    "media_type_unsupported": False,
    "media_unavailable": True,
    "workspace_plane_unavailable": True,
    "grant_replica_missing": True,
    "legacy_media_cutover_conflict": False,
    # False despite being recoverable: the base version the caller sent is the
    # thing that is stale, so resending it loops forever. The guidance above
    # carries the recovery, which is a different request, not a retry.
    "stale_base_version": False,
    "base_not_found": False,
    "reconcile_target_invalid": False,
    "text_occurrence_mismatch": False,
    "text_match_not_found": False,
    "text_match_ambiguous": False,
    "text_match_low_confidence": False,
    "section_not_found": False,
    "section_hash_mismatch": False,
    "block_not_found": False,
    "block_hash_mismatch": False,
    "row_not_found": False,
    "row_hash_mismatch": False,
    "section_position_invalid": False,
    "heading_level_invalid": False,
    "anchor_confidence_low": False,
    "anchor_not_found": False,
    "operations_overlap": False,
    "operation_unsupported": False,
    "block_reflow": False,
    "body_length_exceeded": False,
    "stale_context_draft": False,
}

CONFLICT_ERROR_CODES = {
    "legacy_media_cutover_conflict",
    "media_editing_not_open",
    "media_install_in_progress",
    "media_policy_changed",
    "media_state_changed",
    "stale_context_draft",
}

POLICY_ERROR_CODES = {
    "inconsistent_identifier",
    "mcp_client_workspace_denied",
    "mcp_client_disabled",
    "mcp_client_deleted",
    "rejected_by_policy",
    "page_name_occupied",
    "scope_denied",
    "restricted_library_denied",
    "token_workspace_denied",
    "workspace_membership_missing",
    "workspace_role_insufficient",
    "mcp_connection_disabled",
    "unsupported_retrieval_mode",
    "embedded_image_requires_upload",
    "media_owner_mismatch",
    "media_quota_exceeded",
    "media_markdown_policy_invalid",
}


class MCPErrorResponse(BaseModel):
    envelope: MCPResultEnvelope

    model_config = ConfigDict(extra="forbid")


def ok_envelope(actor: ActorContext) -> MCPResultEnvelope:
    return MCPResultEnvelope(
        result_status="succeeded",
        workspace_id=actor.workspace_id,
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        result_summary="Tool call succeeded.",
    )


def error_envelope(
    actor: ActorContext,
    error_code: str,
    result_status: str | None = None,
    *,
    message: str | None = None,
    details: list[MCPErrorDetail] | None = None,
    retryable: bool | None = None,
    review_item_id=None,
) -> MCPResultEnvelope:
    """Build a failure envelope that says how to recover, not just that it failed.

    The keyword arguments are optional so the existing call sites keep working
    unchanged; anything that has a real message should pass it.
    """

    root = _code_root(error_code)
    guidance = ERROR_CODE_GUIDANCE.get(root)
    if retryable is None:
        retryable = RETRYABLE_BY_CODE.get(root)
    summary = f"Tool call failed: {error_code}."
    # Fold the message into result_summary as well: a client that only reads the
    # summary still learns the cause, and mcp_trace_capture stores this string,
    # so the reason shows up in the Chats timeline with no schema change there.
    if message:
        summary = f"{summary} {message}"
    if guidance:
        summary = f"{summary} {guidance}"
    error_message = message
    if guidance:
        # Validation errors have a message budget; reserve room for the full
        # repair instruction. Other failures keep their complete cause, which
        # can include the version, hash and revision needed to recover.
        if root == "validation_failed" and message:
            error_message = _clamp(
                message, MAX_ERROR_MESSAGE_CHARS - len(guidance) - 1
            )
        error_message = f"{error_message} {guidance}" if error_message else guidance
    return MCPResultEnvelope(
        result_status=result_status or result_status_for_error(error_code),
        workspace_id=actor.workspace_id,
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        result_summary=summary,
        error_code=error_code,
        error_message=error_message,
        error_details=list(details or []),
        retryable=retryable,
        review_item_id=review_item_id,
    )


def validation_error_envelope(
    actor: ActorContext,
    exc: ValidationError,
    *,
    allowed_fields: Iterable[str] | None = None,
) -> MCPResultEnvelope:
    """Translate a pydantic failure into per-field repair instructions."""

    # include_input=False is a hard requirement, not a preference: pydantic
    # otherwise embeds the rejected value, so a 1 MB body_markdown would be
    # reflected into the response and into the persisted trace.
    raw = exc.errors(include_url=False, include_input=False, include_context=False)
    details: list[MCPErrorDetail] = []
    for entry in raw[:MAX_ERROR_DETAILS]:
        loc = tuple(entry.get("loc", ()))
        field = _clamp(".".join(str(part) for part in loc), MAX_FIELD_CHARS) or None
        code = str(entry.get("type", "validation_error"))
        fix = None
        if code == "extra_forbidden":
            # The most common agent mistake is inventing a field name, and it is
            # the one that is trivially self-correctable given the real list --
            # but only when the top-level model is the one that rejected it. A
            # nested model has its own field list, so naming the outer one sends
            # the agent to fix the wrong object.
            if allowed_fields and len(loc) == 1:
                fix = "Remove the unknown field. Accepted fields: " + ", ".join(
                    sorted(allowed_fields)
                )
            else:
                fix = "Remove the unknown field."
        elif code.startswith("missing"):
            fix = f"Add {field}." if field else "Add the missing field."
        details.append(
            MCPErrorDetail(
                field=field,
                code=code,
                message=_clamp(
                    str(entry.get("msg", "invalid value")), MAX_DETAIL_MESSAGE_CHARS
                ),
                fix=fix,
            )
        )
    named = details[:NAMED_IN_MESSAGE]
    message = "; ".join(
        f"{detail.field or 'request'}: {detail.message}" for detail in named
    )
    # Count against what the message actually named. Counting against
    # MAX_ERROR_DETAILS made the marker wrong for any payload with more than
    # three errors and absent entirely between four and ten -- the record then
    # claimed three problems when there were five.
    unlisted = len(raw) - len(named)
    if unlisted > 0:
        message = f"{message} (and {unlisted} more)"
    message = _clamp(message, MAX_ERROR_MESSAGE_CHARS)
    return error_envelope(
        actor,
        "validation_failed",
        message=message or None,
        details=details,
        # Resending the same arguments cannot pass the same validator.
        retryable=False,
    )


def _code_root(error_code: str) -> str:
    """Strip a `:subtype` suffix before any table lookup.

    Some denials carry one — `workspace_role_insufficient:sensitive_agent_page`
    — and every table here is keyed on the bare code with an exact-match `.get`.
    The composite form matched none of them, so the same permanent denial
    answered with guidance and `retryable: False` on one page and with neither
    on another, leaving the agent no signal to stop retrying.
    """

    return error_code.split(":", 1)[0]


def _clamp(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def result_status_for_error(error_code: str) -> str:
    root = _code_root(error_code)
    if root in CONFLICT_ERROR_CODES:
        return "conflict"
    if root in POLICY_ERROR_CODES:
        return "rejected_by_policy"
    return "failed"


# Single payload-advisory -> envelope mapper (design B-11/§8.2). Search (and
# later lanes) put registered warning codes in their payload; MCP handlers
# call this once so trace capture and clients see the same structured
# warnings. Messages are stable English descriptors; clients localize by code.
ADVISORY_WARNING_MESSAGES = {
    "permission_limited": "Restricted Library items and artifacts were excluded.",
    "unsupported_filter_tag": "The tag filter is not applied by any retriever yet.",
    "unsupported_filter_date": (
        "The date_from/date_to filters are not applied by any retriever yet."
    ),
    "semantic_fallback_used": "Results came from the semantic fallback lane.",
    "keyword_lane_unavailable": (
        "Tier-B FTS5 symbolic lanes are unavailable for this database; "
        "exact/direct lanes remain active."
    ),
    "coverage_gap_recorded": "No evidence found; a coverage gap was recorded.",
    "evidence_not_citable": (
        "Evidence was found, but no stable citation target is available yet."
    ),
    "mrtr_preview_unavailable": (
        "The MRTR preview wire is disabled; the stable maintenance response was used."
    ),
    "borrowed_rerank_applied": "The calling client supplied the result reranking.",
    "llm_provider_unavailable": (
        "The configured answer model is not usable, so this answer is the "
        "deterministic evidence summary. Ask a workspace admin to check the "
        "server's LLM provider settings."
    ),
    "llm_composer_failed": (
        "The answer model did not respond, so this answer is the deterministic "
        "evidence summary. Retrying may produce a composed answer."
    ),
    "job_fanout_incomplete": (
        "One or more workspaces could not be reached, so this result is "
        "missing their jobs and any totals shown may undercount."
    ),
}


def advisory_warnings(codes: Iterable[str]) -> list[EnvelopeWarning]:
    warnings: list[EnvelopeWarning] = []
    seen: set[str] = set()
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        warnings.append(
            EnvelopeWarning(
                code=code,
                message=ADVISORY_WARNING_MESSAGES.get(code, code),
            )
        )
    return warnings


def apply_payload_warnings(
    envelope: MCPResultEnvelope, codes: list[str]
) -> MCPResultEnvelope:
    for warning in advisory_warnings(codes):
        code = warning.code
        if any(existing.code == code for existing in envelope.warnings):
            continue
        envelope.warnings.append(warning)
    return envelope
