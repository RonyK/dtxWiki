import logging
import traceback
from uuid import uuid4

import pytest

from backend.mcp.envelope import error_envelope
from backend.mcp.protocol import output_validation
from backend.mcp.protocol import runner as runner_module
from backend.mcp.protocol.output_validation import (
    MCPOutputContractViolation,
    validate_tool_output,
)
from backend.policy.auth import ActorContext
from backend.scripts.export_mcp_schemas import build_mcp_schemas
from backend.tests.conftest import captured_log_messages

pytestmark = pytest.mark.unit


def test_validate_tool_output_accepts_a_conforming_error_envelope():
    # Every real handler's failure path already conforms to its own tool's
    # required=["envelope"] (or ["envelope", "pages"] for list_pages) --
    # this is what _tool_error_payload exists to guarantee. Confirm the
    # validator does not spuriously flag it.
    payload = {
        "envelope": {
            "schema_version": "llmwiki.mcp.v1",
            "request_id": "11111111-1111-1111-1111-111111111111",
            "result_status": "failed",
            "error_code": "validation_failed",
            "warnings": [],
        }
    }
    validate_tool_output("guide", payload)  # must not raise


def test_validate_tool_output_raises_on_a_violation():
    # Fail CLOSED. Dropping outputSchema from the wire also disabled the
    # client SDK's post-call check, so returning None here would serve a
    # payload no one validated -- as a success, since isError comes from the
    # handler's own result_status.
    payload = {"envelope": {"result_status": "succeeded"}, "unexpected": "extra-key"}

    with pytest.raises(MCPOutputContractViolation) as excinfo:
        validate_tool_output("guide", payload)

    assert excinfo.value.tool_name == "guide"
    assert excinfo.value.phase == "handler"
    assert excinfo.value.validator == "additionalProperties"
    assert "extra-key" not in str(excinfo.value)


def test_validate_tool_output_never_logs_the_rejected_value():
    # jsonschema's ValidationError.message embeds the rejected instance, so an
    # enum failure used to print the value verbatim into the application log.
    # For a result_summary or a citation snippet that is document content.
    secret = "SECRET-STATUS-VALUE"
    payload = {"envelope": {"result_status": secret}}

    with captured_log_messages("dtxwiki.mcp", logging.ERROR) as messages:
        with pytest.raises(MCPOutputContractViolation) as excinfo:
            validate_tool_output("guide", payload)

    assert not any(secret in message for message in messages)
    assert secret not in str(excinfo.value)
    # `raise ... from None` in the validator: the runner's sanitize barrier
    # logs this exception with logger.exception, and a chained ValidationError
    # would print the rejected value into that traceback instead.
    assert secret not in "".join(traceback.format_exception(excinfo.value))
    assert any(
        "tool=guide" in message and "phase=handler" in message for message in messages
    )
    # The field is still located precisely -- only its value is gone.
    assert any(
        "$.envelope.result_status" in message and "enum" in message
        for message in messages
    )


@pytest.mark.parametrize("audit_field", ("provider", "request_id"))
def test_wire_phase_rejects_an_audit_only_envelope_field_the_handler_phase_allows(
    audit_field,
):
    # These are two of the 14 audit/telemetry fields _slim_envelope_for_wire
    # folds into envelope.meta. Their presence on the wire copy is a slimming
    # regression that the handler-phase schema cannot see, because the full
    # envelope legitimately carries them.
    payload = {
        "envelope": {
            "result_status": "succeeded",
            audit_field: "11111111-1111-1111-1111-111111111111",
            "meta": {},
        }
    }

    validate_tool_output("guide", payload)  # the handler shape allows it

    with pytest.raises(MCPOutputContractViolation) as excinfo:
        validate_tool_output("guide", payload, phase="wire")

    assert excinfo.value.phase == "wire"
    assert excinfo.value.validator == "additionalProperties"


@pytest.mark.parametrize("tool_name", sorted(build_mcp_schemas()))
def test_tool_error_payload_satisfies_every_tool_contract_in_both_phases(tool_name):
    """The standing proof that ``validate_contract=False`` in the runner's
    three ``except`` handlers (``MCPClientRuntimeDenied``,
    ``_ConversationTokenCarrierConflict``, catch-all ``Exception``) is sound:
    their replacement payload must itself be
    schema-clean, because a raise from inside an except block escapes the
    sanitize barrier entirely. This fails the day someone adds a required
    field to a tool's output model or narrows the envelope -- i.e. the day the
    fail-closed fallback would start needing a fallback of its own.
    """
    actor = ActorContext(
        actor_kind="mcp_client",
        actor_id="contract-guard",
        scopes=("*",),
        workspace_id=uuid4(),
        workspace_role="mcp_client",
        role="mcp_client",
        token_kind="mcp_client",
    )
    envelope = error_envelope(actor, "internal_error", result_status="failed")
    envelope.result_summary = (
        "Tool call failed due to an unexpected server error. "
        "Details were logged on the server."
    )
    payload = runner_module._tool_error_payload(envelope)

    validate_tool_output(tool_name, payload)
    validate_tool_output(
        tool_name, runner_module._slim_envelope_for_wire(payload), phase="wire"
    )


def test_validate_tool_output_is_a_no_op_for_an_unknown_tool_name():
    validate_tool_output("does_not_exist", {"anything": True})  # must not raise


@pytest.mark.parametrize(
    ("folded_field", "bad_value"),
    (("params_summary", "not-a-dict"), ("client_self_reported", "yes")),
)
def test_handler_phase_catches_a_folded_field_the_wire_phase_cannot(
    folded_field, bad_value
):
    """The mirror image of the audit-field test above, and the reason BOTH
    phases have to run. ``_slim_envelope_for_wire`` folds these into
    ``envelope.meta``, typed ``dict[str, Any] | None``, so on the wire copy
    any JSON is legal there. For the 13 folded envelope fields the handler
    phase is the ONLY gate; the wire phase is structurally blind.
    """
    payload = {
        "envelope": {
            "schema_version": "llmwiki.mcp.v1",
            "request_id": "88888888-8888-8888-8888-888888888888",
            "result_status": "succeeded",
            folded_field: bad_value,
            "warnings": [],
        }
    }

    with pytest.raises(MCPOutputContractViolation) as excinfo:
        validate_tool_output("search", payload)

    assert excinfo.value.phase == "handler"
    assert excinfo.value.instance_path == f"$.envelope.{folded_field}"

    # And the wire phase does not raise on the slimmed copy of that same
    # payload -- so nothing but the handler-phase call can catch this.
    validate_tool_output(
        "search", runner_module._slim_envelope_for_wire(payload), phase="wire"
    )


@pytest.mark.parametrize("phase", ("handler", "wire"))
def test_validator_is_compiled_once_per_tool_not_once_per_call(monkeypatch, phase):
    """``jsonschema.validate()`` re-runs ``check_schema`` -- meta-schema
    validation of the tool's own schema -- on every call, and this module
    validates twice per tool call. Measured on these schemas that was 40 ms of
    event-loop time per search call and 66 ms per patch_draft call, versus
    0.24 ms with the validator built once. ``_capture_trace_and_wire_payload``
    runs on the event loop directly, not via ``asyncio.to_thread``, so this is
    not amortisable. Counted, not timed, so it cannot flake.
    """
    monkeypatch.setattr(output_validation, "_VALIDATORS", {})
    monkeypatch.setattr(output_validation, "_WIRE_VALIDATORS", {})
    builds: list[str] = []
    real_validator_for = output_validation.validator_for

    def counting_validator_for(schema, *args, **kwargs):
        builds.append(phase)
        return real_validator_for(schema, *args, **kwargs)

    monkeypatch.setattr(output_validation, "validator_for", counting_validator_for)

    handler_payload = {
        "envelope": {
            "schema_version": "llmwiki.mcp.v1",
            "request_id": "99999999-9999-9999-9999-999999999999",
            "result_status": "succeeded",
            "warnings": [],
        }
    }
    payload = (
        runner_module._slim_envelope_for_wire(handler_payload)
        if phase == "wire"
        else handler_payload
    )
    for _ in range(5):
        validate_tool_output("guide", payload, phase=phase)

    assert builds == [phase]
    # A second tool compiles its own validator; the cache is per tool.
    for _ in range(5):
        validate_tool_output("search", payload, phase=phase)

    assert builds == [phase, phase]


def test_the_model_facing_envelope_field_set_is_pinned():
    """Widening WIRE_ENVELOPE_FIELDS must be a deliberate, reviewed act.

    The runner's slimmer and the wire-phase schema are both derived from this
    one frozenset, so adding a name to it widens BOTH in lockstep: the audit
    field then reaches the client and every validation still passes, because
    after the widening the payload is correct by construction. No payload-shaped
    assertion can see that. The membership is the only place it can be caught,
    so it is asserted here rather than described in a comment.

    These are the 9 fields a model actually acts on -- a status/error to branch
    on, warnings and error_details to self-correct from, and the two handles a
    follow-up call needs. The other 13 are audit/telemetry, and folding them
    into envelope.meta is most of the wire saving the 260901 design bought.
    """

    assert output_validation.WIRE_ENVELOPE_FIELDS == frozenset(
        {
            "result_status",
            "result_summary",
            "error_code",
            "error_message",
            "error_details",
            "retryable",
            "warnings",
            "conversation_token",
            "review_item_id",
        }
    )


def test_the_folded_field_count_matches_the_envelope():
    """The comments say 14; make that a measurement, not a claim.

    They said 13 for the whole of Epic-1, at nine sites across production code
    and tests. The real count is 14, and the fourteenth is `meta` ITSELF: the
    slimmer folds every non-model-facing field into a fresh `meta` dict, and
    `meta` is not in the model-facing set, so a fresh envelope produces
    `envelope.meta.meta = null` on the wire. That artifact is recorded but not
    yet fixed -- `_slim_envelope_for_wire` lives in runner.py, which another
    change owns at the time of writing.

    Pinning the count here means the next person to touch the envelope has to
    update the number rather than discovering later that nine comments drifted.
    """

    from backend.schemas.common import MCPResultEnvelope

    folded = set(MCPResultEnvelope.model_fields) - set(
        output_validation.WIRE_ENVELOPE_FIELDS
    )

    assert len(folded) == 14, (
        "the number in the comments across tool_descriptors.py, "
        "schemas/common.py and output_validation.py must match this set: "
        f"{sorted(folded)}"
    )
    assert "meta" in folded, (
        "`meta` folding into itself is what makes the count 14 rather than 13, "
        "and is why a fresh envelope carries `envelope.meta.meta = null`"
    )
