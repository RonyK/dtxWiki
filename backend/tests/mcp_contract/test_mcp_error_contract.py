"""A failed tool call must tell the caller how to repair it.

An MCP client used to receive only `{"error_code": "validation_failed"}` while a
REST caller got the exception's message for the very same failure. An agent
cannot fix a request it cannot see the problem in, so it either gives up or
retries the identical call forever.
"""

import pytest
from pydantic import ValidationError

from backend.mcp.envelope import (
    ERROR_CODE_GUIDANCE,
    MAX_ERROR_DETAILS,
    MAX_ERROR_MESSAGE_CHARS,
    MAX_FIELD_CHARS,
    RETRYABLE_BY_CODE,
    MCPErrorResponse,
    error_envelope,
    validation_error_envelope,
)
from backend.mcp.protocol.runner import _capture_trace_and_wire_payload
from backend.mcp.router import dispatch
from backend.policy.auth import ActorContext
from backend.schemas.common import ERROR_CODES
from backend.schemas.mcp.context_tools import (
    PatchContextEditInput,
    UpdateContextEditInput,
)
from backend.services.context_image_ingest import CONTEXT_IMAGE_INGEST_ERROR_CODES

_VALID_UPDATE_CONTEXT_EDIT_PAYLOAD = {
    "workspace_id": "00000000-0000-0000-0000-000000000001",
    "page_id": "00000000-0000-0000-0000-000000000002",
    "body_md": "body",
    "base_version_id": "00000000-0000-0000-0000-000000000003",
    "base_content_hash": "a" * 64,
    "expected_editing_revision": 1,
    "idempotency_key": "error-contract-key-1",
}


def _actor() -> ActorContext:
    return ActorContext(
        actor_kind="mcp_client",
        actor_id="error-contract",
        token_kind="mcp_client",
        workspace_role="editor",
        role="editor",
    )


def _validation_error(payload: dict, model=UpdateContextEditInput) -> ValidationError:
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate(payload)
    return excinfo.value


def test_missing_field_names_the_field_and_the_repair():
    exc = _validation_error({})

    envelope = validation_error_envelope(
        _actor(), exc, allowed_fields=UpdateContextEditInput.model_fields
    )

    assert envelope.error_code == "validation_failed"
    assert envelope.error_message
    fields = {detail.field for detail in envelope.error_details}
    assert "body_md" in fields
    # base_version_id/base_content_hash are optional for your own open edit;
    # the revision is the concurrency check that is always required.
    assert "expected_editing_revision" in fields
    # Resending the same arguments can never pass the same validator.
    assert envelope.retryable is False


def test_unknown_field_gets_the_accepted_field_list():
    """The most common agent mistake, and the most trivially self-correctable."""

    exc = _validation_error(
        {
            **_VALID_UPDATE_CONTEXT_EDIT_PAYLOAD,
            "bodyMd": "typo-cased duplicate",
        }
    )

    envelope = validation_error_envelope(
        _actor(), exc, allowed_fields=UpdateContextEditInput.model_fields
    )

    extra = [d for d in envelope.error_details if d.code == "extra_forbidden"]
    assert extra, envelope.error_details
    assert extra[0].fix and "body_md" in extra[0].fix


def test_rejected_value_is_never_reflected_back():
    """`body_md` may be a megabyte; echoing it wastes tokens and is an
    injection vector, and the string is also persisted in the MCP trace."""

    secret = "SENTINEL-DO-NOT-REFLECT-9f2c"
    exc = _validation_error(
        {
            **_VALID_UPDATE_CONTEXT_EDIT_PAYLOAD,
            "body_md": secret,
            "unexpected_field": secret,
        }
    )

    envelope = validation_error_envelope(
        _actor(), exc, allowed_fields=UpdateContextEditInput.model_fields
    )

    assert secret not in envelope.model_dump_json()


def test_detail_list_is_capped_and_reports_the_remainder():
    payload = dict(_VALID_UPDATE_CONTEXT_EDIT_PAYLOAD)
    payload.update({f"bogus_{index}": index for index in range(MAX_ERROR_DETAILS + 5)})
    exc = _validation_error(payload)

    envelope = validation_error_envelope(
        _actor(), exc, allowed_fields=UpdateContextEditInput.model_fields
    )

    assert len(envelope.error_details) == MAX_ERROR_DETAILS
    assert "more" in (envelope.error_message or "")


def test_service_message_and_recovery_guidance_reach_the_caller():
    envelope = error_envelope(
        _actor(),
        "rejected_by_policy",
        message="core claim missing citation under strict policy",
    )

    assert envelope.error_message == (
        "core claim missing citation under strict policy "
        + ERROR_CODE_GUIDANCE["rejected_by_policy"]
    )
    # A client that only reads result_summary still learns the cause and the fix.
    assert "core claim missing citation" in (envelope.result_summary or "")
    assert ERROR_CODE_GUIDANCE["rejected_by_policy"] in (envelope.result_summary or "")
    assert envelope.retryable is False


def test_stale_base_version_is_not_retryable_but_says_how_to_recover():
    """`retryable` is about resending the identical call, and this one loops.

    The base version the caller sent is the stale thing, so an agent that reads
    `retryable is True` here would resend it forever. Recovery goes through the
    guidance instead, which asks for a different request.
    """

    envelope = error_envelope(_actor(), "stale_base_version", "conflict")
    summary = envelope.result_summary or ""

    assert envelope.retryable is False
    assert "latest_version_id" in summary
    # The P8 edit tools name the caller's own open edit. The recovery is to
    # re-read it: a revision that moved means something else saved -- maybe
    # the same person in the browser -- and resending with the new revision
    # swapped in overwrote exactly that work (B-5).
    assert "Re-read before retrying" in summary
    assert "version='my_edit'" in summary
    assert "do not resend the same content" in summary
    assert "resend with it" not in summary


def test_stale_context_draft_defaults_to_non_retryable_conflict():
    envelope = error_envelope(_actor(), "stale_context_draft")

    assert envelope.result_status == "conflict"
    assert envelope.retryable is False
    assert "latest linear draft" in (envelope.result_summary or "")


def test_media_editing_not_open_is_a_nonretryable_conflict():
    envelope = error_envelope(_actor(), "media_editing_not_open")

    assert envelope.result_status == "conflict"
    assert envelope.retryable is False
    assert ERROR_CODE_GUIDANCE["media_editing_not_open"] in (
        envelope.result_summary or ""
    )
    # Recovery must survive the wire projection that omits result_summary.
    _, _, wire = _capture_trace_and_wire_payload(
        "upload_image", {}, MCPErrorResponse(envelope=envelope).model_dump(mode="json")
    )
    assert "result_summary" not in wire["envelope"]
    assert (
        wire["envelope"]["error_message"]
        == ERROR_CODE_GUIDANCE["media_editing_not_open"]
    )


def test_media_editing_not_open_guidance_names_the_explicit_recovery_field():
    guidance = ERROR_CODE_GUIDANCE["media_editing_not_open"]

    assert "editing_version_id" in guidance
    assert "upload_image" in guidance


def test_embedded_image_guidance_never_tells_agents_to_persist_transport_urls():
    guidance = ERROR_CODE_GUIDANCE["embedded_image_requires_upload"]

    assert "data.markdown" in guidance
    assert "data.logical_ref" in guidance
    assert "editing_version_id" in guidance
    assert "Never persist data.content_url" in guidance
    assert "relative URL" not in guidance


def test_every_canonical_ingest_failure_has_a_pinned_mcp_contract():
    expected_status = {
        "validation_failed": "failed",
        "media_type_unsupported": "failed",
        "media_content_mismatch": "failed",
        "media_too_large": "failed",
        "not_found": "failed",
        "media_policy_changed": "conflict",
        "media_owner_mismatch": "rejected_by_policy",
        "media_editing_not_open": "conflict",
        "media_unavailable": "failed",
        "media_quota_exceeded": "rejected_by_policy",
        "media_staging_limit_exceeded": "failed",
        "media_staging_bytes_exceeded": "failed",
        "media_install_in_progress": "conflict",
        "media_install_unavailable": "failed",
        "media_state_changed": "conflict",
    }

    assert CONTEXT_IMAGE_INGEST_ERROR_CODES == set(expected_status)
    assert CONTEXT_IMAGE_INGEST_ERROR_CODES <= set(ERROR_CODES)
    assert CONTEXT_IMAGE_INGEST_ERROR_CODES <= ERROR_CODE_GUIDANCE.keys()
    assert CONTEXT_IMAGE_INGEST_ERROR_CODES <= RETRYABLE_BY_CODE.keys()
    for error_code, result_status in expected_status.items():
        assert error_envelope(_actor(), error_code).result_status == result_status


def test_media_policy_change_recovery_requires_refresh_and_a_new_marker():
    envelope = error_envelope(_actor(), "media_policy_changed")

    assert envelope.result_status == "conflict"
    assert envelope.retryable is False
    assert "current media policy" in (envelope.result_summary or "")
    assert "preflight" in (envelope.result_summary or "")
    assert "new marker_uuid" in (envelope.result_summary or "")


def test_existing_call_sites_keep_working_without_the_new_arguments():
    """The keyword arguments are optional; ~77 call sites pass none of them."""

    envelope = error_envelope(_actor(), "not_found")

    assert envelope.error_code == "not_found"
    assert envelope.error_message == ERROR_CODE_GUIDANCE["not_found"]
    assert envelope.error_details == []


def test_a_read_tool_also_names_the_field_not_just_the_write_tools(
    mcp_default_actor, page_service
):
    """The detail path reaches the whole tool surface, not only the write tools.

    `search` stands in for the fifteen read/review/agent/maintenance handlers
    that used to answer `validation_failed` with nothing attached.
    """

    actor = mcp_default_actor(scopes=("search",))

    response = dispatch(
        "search",
        {"workspace_id": "not-a-uuid", "queryy": "typo in the field name"},
        actor=actor,
        page_service=page_service,
    )

    envelope = response.envelope
    assert envelope.error_code == "validation_failed"
    fields = {detail.field for detail in envelope.error_details}
    assert "workspace_id" in fields
    assert "queryy" in fields
    extra = next(
        detail for detail in envelope.error_details if detail.field == "queryy"
    )
    assert extra.fix and "query" in extra.fix
    assert envelope.retryable is False


def test_an_invented_field_name_cannot_flood_the_response_or_the_trace():
    """`include_input=False` keeps the rejected *value* out, but for
    `extra_forbidden` the location IS caller-authored text -- the JSON key it
    made up. It reaches the response, `mcp_tool_calls.result_summary` and the
    Chats timeline, so three 300 KB keys produced a ~900 KB record."""

    exc = _validation_error(
        {
            **_VALID_UPDATE_CONTEXT_EDIT_PAYLOAD,
            **{f"{'k' * 300_000}{index}": 1 for index in range(3)},
        }
    )

    envelope = validation_error_envelope(
        _actor(), exc, allowed_fields=UpdateContextEditInput.model_fields
    )

    assert len(envelope.error_message or "") <= MAX_ERROR_MESSAGE_CHARS + 1
    assert envelope.error_message.endswith(ERROR_CODE_GUIDANCE["validation_failed"])
    assert len(envelope.result_summary or "") < 2000
    for detail in envelope.error_details:
        assert len(detail.field or "") <= MAX_FIELD_CHARS + 1


def test_a_nested_rejection_does_not_offer_the_outer_models_field_list():
    """`operations[0].confidence` is rejected by the operation's own typed
    model, whose fields are nothing like PatchContextEditInput's. Listing the
    outer names sent the agent to fix the wrong object."""

    exc = _validation_error(
        {
            "workspace_id": "00000000-0000-0000-0000-000000000001",
            "page_id": "00000000-0000-0000-0000-000000000002",
            "version_id": "00000000-0000-0000-0000-000000000003",
            "operations": [
                {
                    "op": "replace_text",
                    "old_text": "before",
                    "new_text": "after",
                    "confidence": 0.9,
                }
            ],
            "expected_editing_revision": 1,
            "idempotency_key": "error-contract-key-2",
        },
        model=PatchContextEditInput,
    )

    envelope = validation_error_envelope(
        _actor(), exc, allowed_fields=PatchContextEditInput.model_fields
    )

    nested = [
        detail
        for detail in envelope.error_details
        if detail.code == "extra_forbidden"
        and (detail.field or "").startswith("operations")
    ]
    assert nested, [detail.model_dump() for detail in envelope.error_details]
    for detail in nested:
        assert detail.fix == "Remove the unknown field."


def test_the_and_n_more_marker_counts_what_the_message_left_out():
    """The cap on details and the cap on names in the message are independent,
    so counting against the former claimed three problems when there were five
    and printed nothing at all between four and ten."""

    exc = _validation_error({})
    raw_count = len(exc.errors())
    assert raw_count > 3, raw_count

    envelope = validation_error_envelope(_actor(), exc)

    assert f"(and {raw_count - 3} more)" in (envelope.error_message or "")


def test_a_composite_denial_code_still_finds_its_guidance_and_retryability():
    """read_agent_page denies a sensitive page with
    `workspace_role_insufficient:sensitive_agent_page`. Every lookup here is an
    exact-match `.get`, so the composite form matched none of them and the same
    permanent denial answered with `retryable: None` and no guidance."""

    envelope = error_envelope(
        _actor(),
        "workspace_role_insufficient:sensitive_agent_page",
        result_status="rejected_by_policy",
    )

    assert envelope.retryable is False
    assert ERROR_CODE_GUIDANCE["workspace_role_insufficient"] in (
        envelope.result_summary or ""
    )
    # The composite code itself is preserved; only the lookup is normalized.
    assert envelope.error_code == "workspace_role_insufficient:sensitive_agent_page"


def test_guidance_never_points_at_an_envelope_field_that_may_be_empty():
    for code, guidance in ERROR_CODE_GUIDANCE.items():
        assert "envelope." not in guidance, code


def test_recovery_maps_are_keyed_by_registered_error_codes():
    """Guidance keyed by a code nothing raises is guidance nobody ever sees.

    The partial-edit codes were first written from the design prose, so the
    maps carried names like `text_not_unique` and `anchor_orphan` while the
    pipeline raised `text_occurrence_mismatch` and `anchor_not_found`. Every
    entry looked healthy in isolation and no test compared the two, so the
    recovery text for a refused edit was unreachable.
    """
    from backend.mcp.envelope import CONFLICT_ERROR_CODES

    registered = set(ERROR_CODES)
    for name, keys in (
        ("ERROR_CODE_GUIDANCE", set(ERROR_CODE_GUIDANCE)),
        ("RETRYABLE_BY_CODE", set(RETRYABLE_BY_CODE)),
        ("CONFLICT_ERROR_CODES", set(CONFLICT_ERROR_CODES)),
    ):
        assert keys <= registered, (
            f"{name} is keyed by unregistered error codes: {sorted(keys - registered)}"
        )


def test_dispatch_unknown_tool_returns_unknown_tool_envelope(
    page_service, mcp_default_actor
):
    """An unregistered tool name is an error envelope, not a raised exception.

    This lived in test_page_tools.py, beside the legacy Pages tools, until
    those were deregistered and deleted. It never tested them -- it tests the
    router's fallthrough -- so it moved here rather than going with them.
    """

    actor = mcp_default_actor(scopes=("read", "draft-write"))

    response = dispatch(
        "does_not_exist",
        {},
        actor=actor,
        page_service=page_service,
    )

    assert isinstance(response, MCPErrorResponse)
    assert response.envelope.result_status == "failed"
    assert response.envelope.workspace_id == actor.workspace_id
    assert response.envelope.actor.kind == actor.actor_kind
    assert response.envelope.actor.id == actor.actor_id
    assert response.envelope.result_summary == "Tool call failed: unknown_tool."
    assert response.envelope.error_code == "unknown_tool"
    assert set(response.model_dump().keys()) == {"envelope"}
