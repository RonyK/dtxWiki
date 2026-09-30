"""Runner-side structuredContent validation.

Replaces the reference SDK's own post-call output validation
(``mcp/server/lowlevel/server.py``), which the server disables by not
advertising ``outputSchema`` on the wire (see ``tool_descriptors.py``) --
Anthropic's own tool-use API never reads it, so 75% of the advertised
``tools/list`` payload was dead weight for every client built on that SDK.
Dropping it from the wire also drops that SDK's own safety net, leaving this
module as the ONLY output gate, so it fails CLOSED: a violation raises and the
runner's sanitize barrier turns it into the fixed ``internal_error`` result.

Two distinct shapes are checked, because they are not the same object: the
FULL payload the handler returned, and the redacted + envelope-slimmed copy
actually put on the wire. Validating only the first would miss any defect the
redaction or slimming step introduces.

One class of served payload is deliberately shaped-but-not-checked: the fixed
``_tool_error_payload`` replacement built by the runner's three ``except``
handlers. It goes through the same redaction + slimming step, so what a client
receives is still the slimmed shape, but validating it would mean raising from
inside an ``except`` block, where no sanitize barrier remains. Its conformance
is pinned statically instead, by
``test_tool_error_payload_satisfies_every_tool_contract_in_both_phases``.

The schemas come from a live ``build_mcp_schemas()`` call, not from reading
``shared/schemas/mcp/v1/*.json`` -- the checked-in files are the export of
those same Pydantic-derived objects and ``tests/static/test_schema_drift.py``
is what keeps the two identical, so validating in-process adds no filesystem
or packaging dependency at runtime.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import jsonschema
from jsonschema.validators import validator_for

from backend.scripts.export_mcp_schemas import build_mcp_schemas

logger = logging.getLogger("dtxwiki.mcp")

# The 9 MCPResultEnvelope fields a model actually acts on: a status/error to
# branch on, warnings and error_details to self-correct from, and the two
# handles (conversation_token, review_item_id) a follow-up call needs. The
# other 14 fields (schema_version, request_id, workspace_id, actor*,
# audit_event_id, activity_ref, provider, model_id, model_version,
# params_summary, client_self_reported) are audit/telemetry nobody has ever
# branched a tool call on -- folding them into envelope.meta instead of
# repeating them in every tool's outputSchema is most of this envelope's
# 47% share of the wire tools/list payload. It lives here rather than in
# runner.py because ``_wire_schemas()`` below derives its pruned schema from
# it: the runner already imports this module (and aliases this name as
# ``_WIRE_ENVELOPE_FIELDS``), so defining it in runner.py would force this
# module to import the runner back. Only that second direction is impossible;
# the first is what actually happens.
WIRE_ENVELOPE_FIELDS = frozenset(
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

_SCHEMAS: dict[str, dict] | None = None
_WIRE_SCHEMAS: dict[str, dict] | None = None
# Compiled validator per (phase, tool). Caching these, not just the schemas,
# is the whole point: ``jsonschema.validate()`` re-runs ``check_schema`` --
# meta-schema validation of the tool's OWN schema -- on every call, and this
# module validates twice per tool call. Measured on these schemas that is
# 40 ms/call for search and 66 ms for patch_context_edit, against 0.24 ms with the
# validator built once; and the runner calls this from ``call_tool_async``
# directly, not through ``asyncio.to_thread``, so the cost lands on the MCP
# server's event loop. ``check_schema`` still runs -- once, at build time.
_VALIDATORS: dict[str, jsonschema.protocols.Validator] = {}
_WIRE_VALIDATORS: dict[str, jsonschema.protocols.Validator] = {}


class MCPOutputContractViolation(RuntimeError):
    """A tool result violated its own declared output contract.

    Carries only schema-space coordinates -- never the offending value --
    because ``str(exc)`` reaches the log through the runner's sanitize
    barrier, which logs the exception it caught.
    """

    def __init__(
        self,
        *,
        tool_name: str,
        phase: str,
        instance_path: str,
        schema_path: str,
        validator: str,
    ) -> None:
        super().__init__(
            f"mcp tool={tool_name} phase={phase} output contract violated "
            f"at {instance_path} (schema {schema_path}, keyword {validator})"
        )
        self.tool_name = tool_name
        self.phase = phase
        self.instance_path = instance_path
        self.schema_path = schema_path
        self.validator = validator


def _schemas() -> dict[str, dict]:
    global _SCHEMAS
    if _SCHEMAS is None:
        _SCHEMAS = build_mcp_schemas()
    return _SCHEMAS


def _wire_schemas() -> dict[str, dict]:
    """Per-tool schema for the SLIMMED wire shape.

    Derived by pruning ``MCPResultEnvelope``'s properties to
    ``WIRE_ENVELOPE_FIELDS`` + ``meta``. The envelope model declares
    ``additionalProperties: false``, so an audit-only field surviving onto the
    wire is a hard validation failure rather than a silent leak.

    What this can and cannot catch, precisely: the runner's slimmer filters by
    this same frozenset, so WIDENING ``WIRE_ENVELOPE_FIELDS`` widens the
    slimmer and this schema in lockstep and is invisible here. Nothing about
    the payload can catch that, because after the widening the payload is
    correct by construction -- the only defence is that the membership itself
    is pinned, by
    ``test_output_validation.py::test_the_model_facing_envelope_field_set_is_pinned``.
    What THIS schema catches is every way a payload can reach the wire NOT
    having been slimmed by that function: a new return path in
    ``call_tool_async`` that forgets the capture helper, a slimmer that stops
    filtering or folds into the wrong key, and redaction that adds or corrupts
    top-level/``data`` keys.
    """

    global _WIRE_SCHEMAS
    if _WIRE_SCHEMAS is None:
        allowed = WIRE_ENVELOPE_FIELDS | {"meta"}
        built: dict[str, dict] = {}
        for tool_name, schema in _schemas().items():
            output = copy.deepcopy(schema["output"])
            envelope_def = output.get("$defs", {}).get("MCPResultEnvelope")
            # Guarded rather than indexed: a tool whose output schema stops
            # inlining that $def degrades to the unpruned check instead of
            # taking the whole runner down on the first call.
            if isinstance(envelope_def, dict) and isinstance(
                envelope_def.get("properties"), dict
            ):
                envelope_def["properties"] = {
                    key: value
                    for key, value in envelope_def["properties"].items()
                    if key in allowed
                }
                if isinstance(envelope_def.get("required"), list):
                    envelope_def["required"] = [
                        key for key in envelope_def["required"] if key in allowed
                    ]
            built[tool_name] = {"output": output}
        _WIRE_SCHEMAS = built
    return _WIRE_SCHEMAS


def _validator_for_tool(
    tool_name: str, *, phase: str
) -> jsonschema.protocols.Validator | None:
    """The compiled validator for one tool+phase, built at most once.

    ``None`` for a tool with no declared schema, which ``validate_tool_output``
    treats as a no-op. ``check_schema`` is kept -- a malformed tool schema must
    still be caught loudly -- but it is paid here, once per tool, instead of on
    every tool call the way ``jsonschema.validate()`` would.
    """

    cache = _WIRE_VALIDATORS if phase == "wire" else _VALIDATORS
    cached = cache.get(tool_name)
    if cached is not None:
        return cached
    schemas = _wire_schemas() if phase == "wire" else _schemas()
    schema = schemas.get(tool_name)
    if schema is None:
        return None
    output_schema = schema["output"]
    validator_cls = validator_for(output_schema)
    validator_cls.check_schema(output_schema)
    validator = validator_cls(output_schema)
    cache[tool_name] = validator
    return validator


def validate_tool_output(
    tool_name: str, payload: dict[str, Any], *, phase: str = "handler"
) -> None:
    """Raise ``MCPOutputContractViolation`` when ``payload`` violates the
    tool's own declared output contract.

    ``phase="handler"`` checks the FULL payload the handler returned;
    ``phase="wire"`` checks the redacted + envelope-slimmed copy actually
    sent, against a schema whose envelope is pruned to the model-facing
    allowlist.

    This FAILS CLOSED. Dropping ``outputSchema`` from the wire also disabled
    the client SDK's own post-call check, so nothing downstream would notice;
    the caller's sanitize barrier turns the raise into the fixed
    ``internal_error`` result rather than serving a payload no one validated.

    Only schema-space coordinates are logged. ``jsonschema``'s
    ``ValidationError.message`` embeds the rejected instance verbatim (an enum
    failure prints the value), so ``exc.message``, ``exc.instance``,
    ``exc.validator_value`` and ``exc.context`` must never reach the log --
    for a ``result_summary`` or a citation snippet that would be document
    content written into application logs.
    """

    validator = _validator_for_tool(tool_name, phase=phase)
    if validator is None:
        return
    try:
        validator.validate(payload)
    except jsonschema.ValidationError as exc:
        instance_path = exc.json_path
        schema_path = "/".join(str(part) for part in exc.absolute_schema_path)
        # NOT `validator`: that name holds the compiled validator above, and
        # rebinding it here makes the branch read as if it mutates the cache.
        keyword = str(exc.validator)
        logger.error(
            "mcp tool=%s phase=%s returned structuredContent violating its "
            "own output contract at %s (schema %s, keyword %s)",
            tool_name,
            phase,
            instance_path,
            schema_path,
            keyword,
        )
        # `from None` is load-bearing: a chained ValidationError would be
        # printed, instance value included, by the runner's logger.exception
        # traceback -- re-introducing the leak this branch just avoided.
        raise MCPOutputContractViolation(
            tool_name=tool_name,
            phase=phase,
            instance_path=instance_path,
            schema_path=schema_path,
            validator=keyword,
        ) from None
