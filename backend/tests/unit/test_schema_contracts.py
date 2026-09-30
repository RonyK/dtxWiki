import ast
import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.schemas.auth import OAuthConsentData, OAuthConsentWorkspaceData
from backend.schemas.common import (
    ERROR_CODES,
    MCP_SCHEMA_VERSION,
    WARNING_CODES,
    EnvelopeWarning,
    MCPActor,
    MCPResultEnvelope,
)
from backend.schemas.context_write import (
    CitationInput,
    UpdateDraftRequest,
    WriteDraftRequest,
)
from backend.schemas.page import (
    BODY_MD_MAX_LENGTH,
    PageCreateRequest,
    PageMetadataPatch,
    PageUpdateRequest,
)
from backend.schemas.read import RankedResult


def test_error_codes_match_shared_contract_artifact():
    shared_error_codes = json.loads(Path("shared/error_codes.json").read_text())["p0_0"]

    assert ERROR_CODES == tuple(shared_error_codes)


def test_policy_error_codes_are_registered():
    from backend.mcp.envelope import POLICY_ERROR_CODES

    assert POLICY_ERROR_CODES.issubset(set(ERROR_CODES))


def test_compound_error_codes_have_registered_base_codes():
    registered = _registered_error_codes()

    for emitted in (
        "workspace_role_insufficient:sensitive_agent_page",
        "workspace_role_insufficient:sensitive_resource",
    ):
        base, _, detail = emitted.partition(":")
        assert detail
        assert base in registered


def _registered_error_codes() -> set[str]:
    registry = json.loads(Path("shared/error_codes.json").read_text())
    return set(registry["p0_0"]) | set(registry["rest"])


def test_rest_error_code_registry_section_is_sorted_unique_and_disjoint():
    registry = json.loads(Path("shared/error_codes.json").read_text())
    rest_codes = registry["rest"]

    assert rest_codes == sorted(rest_codes)
    assert len(rest_codes) == len(set(rest_codes))
    assert set(rest_codes).isdisjoint(set(registry["p0_0"]))


def test_high_fix_error_codes_are_registered():
    registered = _registered_error_codes()

    assert "signup_disabled" in registered
    assert "restricted_library_denied" in registered


def test_mcp_access_lifecycle_rest_error_codes_are_registered():
    registered = _registered_error_codes()

    assert {
        "invalid_access_key_cursor",
        "mcp_client_already_deleted",
        "mcp_client_cursor_invalid",
        "mcp_client_must_be_disabled",
        "mcp_client_system_managed",
    } <= registered


def test_context_media_rest_error_codes_are_registered():
    registered = _registered_error_codes()

    assert {
        "legacy_media_cutover_conflict",
        "media_content_mismatch",
        "media_in_use",
        "media_install_in_progress",
        "media_install_unavailable",
        "media_markdown_policy_invalid",
        "media_policy_changed",
        "media_editing_not_open",
        "media_quota_exceeded",
        "media_range_invalid",
        "media_reference_invalid",
        "media_staging_bytes_exceeded",
        "media_staging_limit_exceeded",
        "media_state_changed",
        "media_too_large",
        "media_type_unsupported",
        "media_usage_token_stale",
        "media_used_in_history",
        "request_too_large",
    } <= registered


def _error_code_literals(tree: ast.AST) -> set[str]:
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (
                    keyword.arg == "error_code"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    emitted.add(keyword.value.value)
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "error_code"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    emitted.add(value.value)
    return emitted


def test_read_api_emits_only_registered_error_codes():
    """Every error_code literal in backend/api/read.py must be registered.

    shared/error_codes.json is the declared error-code contract; route
    handlers minting unregistered codes silently widen the wire surface.
    Literal scan covers error_code= keywords and {"error_code": ...} dicts.
    The dynamic PermissionError branch in require_workspace_access derives
    its code from registered policy codes with a literal fallback, so the
    fallback is asserted explicitly.
    """
    source = Path("backend/api/read.py").read_text(encoding="utf-8")
    emitted = _error_code_literals(ast.parse(source))

    assert emitted, "expected backend/api/read.py to emit error_code literals"
    registered = _registered_error_codes()
    assert emitted - registered == set()
    assert "workspace_access_denied" in registered


def test_warning_codes_match_shared_contract_artifact():
    shared_warning_codes = json.loads(Path("shared/error_codes.json").read_text())[
        "warnings"
    ]

    assert WARNING_CODES == tuple(shared_warning_codes)


def test_ranked_result_type_is_schema_enum():
    result_type = RankedResult.model_json_schema()["properties"]["type"]

    assert set(result_type["enum"]) == {
        "context_page",
        "context_claim",
        "library_item",
        "library_artifact",
        "feedback",
        "change_set",
        "agent_page",
        "source",
    }


def test_mcp_envelope_uses_p0_0_schema_version_and_status_subset():
    envelope = MCPResultEnvelope(
        result_status="succeeded",
        workspace_id=UUID("00000000-0000-0000-0000-000000000123"),
        actor_kind="mcp_client",
        actor_id="dev-local-mcp",
    )

    assert envelope.schema_version == MCP_SCHEMA_VERSION
    assert envelope.result_status == "succeeded"
    assert envelope.workspace_id == UUID("00000000-0000-0000-0000-000000000123")
    assert envelope.actor == MCPActor(kind="mcp_client", id="dev-local-mcp")
    assert envelope.actor_kind == "mcp_client"
    assert envelope.actor_id == "dev-local-mcp"
    assert envelope.result_summary is None
    assert envelope.activity_ref is None
    assert envelope.provider == "unknown"
    assert envelope.model_id == "unknown"
    assert envelope.model_version is None
    assert envelope.params_summary == {}
    assert envelope.client_self_reported is False
    assert envelope.warnings == []
    assert envelope.error_code is None


def test_mcp_envelope_rejects_legacy_result_status():
    with pytest.raises(ValidationError):
        MCPResultEnvelope(
            result_status="ok",
            workspace_id=UUID("00000000-0000-0000-0000-000000000123"),
            actor_kind="mcp_client",
            actor_id="dev-local-mcp",
        )


def test_mcp_envelope_allows_workspace_less_results_and_optional_actor():
    # Codex #8: actor is OPTIONAL on the v1 wire (actor_kind/actor_id remain the
    # canonical legacy fields), so an old v1 consumer is not rejected for a
    # missing required field. A workspace-less result is also allowed.
    envelope = MCPResultEnvelope(
        result_status="succeeded",
        actor=MCPActor(kind="mcp_client", id="dev-local-mcp"),
    )
    assert envelope.workspace_id is None

    # No actor and no legacy fields: now valid (actor stays None).
    bare = MCPResultEnvelope(
        result_status="succeeded",
        workspace_id=UUID("00000000-0000-0000-0000-000000000123"),
    )
    assert bare.actor is None

    # Legacy actor_kind/actor_id synthesize the structured actor object.
    legacy = MCPResultEnvelope(
        result_status="succeeded", actor_kind="mcp_client", actor_id="dev-local-mcp"
    )
    assert legacy.actor is not None and legacy.actor.id == "dev-local-mcp"

    schema = MCPResultEnvelope.model_json_schema()
    assert "actor" not in set(schema["required"])
    assert "result_status" in set(schema["required"])
    assert "workspace_id" not in set(schema["required"])


def test_warning_validator_rejects_unknown_code():
    with pytest.raises(ValidationError):
        EnvelopeWarning(code="bogus", message="x")


def test_envelope_warning_code_enum_matches_registry_across_all_schemas():
    """Every exported EnvelopeWarning enum must match the Python registry.

    MCP schemas store EnvelopeWarning under output.$defs; REST OpenAPI stores
    it under components.schemas. Schemas without EnvelopeWarning are skipped
    for forward compatibility. Do not tighten that skip into a presence
    assertion without auditing the REST and MCP schema exporters first.
    """
    repo_root = Path(__file__).resolve().parents[3]
    mcp_dir = repo_root / "shared/schemas/mcp/v1"
    rest_openapi = repo_root / "shared/schemas/rest/v1/openapi.json"
    expected = set(WARNING_CODES)
    offenders: list[str] = []

    for schema_path in sorted(mcp_dir.glob("*.json")):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        # MCP schemas split into {input, output}; warnings live on output.$defs.
        warning_def = schema.get("output", {}).get("$defs", {}).get("EnvelopeWarning")
        if warning_def is None:
            continue
        code_schema = warning_def["properties"]["code"]
        if set(code_schema.get("enum", [])) != expected:
            offenders.append(str(schema_path.relative_to(repo_root)))

    rest = json.loads(rest_openapi.read_text(encoding="utf-8"))
    rest_warning = rest.get("components", {}).get("schemas", {}).get("EnvelopeWarning")
    if rest_warning is not None:
        rest_code = rest_warning["properties"]["code"]
        if set(rest_code.get("enum", [])) != expected:
            offenders.append(str(rest_openapi.relative_to(repo_root)))

    assert offenders == [], (
        f"Warning code enum drift detected in {len(offenders)} schema file(s): "
        f"{offenders}. Run `.venv/bin/python -m backend.scripts.export_openapi` "
        f"and `.venv/bin/python -m backend.scripts.export_mcp_schemas` to "
        f"regenerate. Partial regeneration is the most common cause."
    )


def test_page_write_contract_requires_base_for_updates_only():
    create_request = PageCreateRequest(
        workspace_slug="default",
        slug="hello",
        title="Hello",
        body_md="# Hello",
    )
    assert create_request.workspace_slug == "default"

    base_version_id = UUID("00000000-0000-0000-0000-000000000001")
    update_request = PageUpdateRequest(
        title="Hello v2",
        body_md="# Hello v2",
        base_version_id=base_version_id,
        base_content_hash="a" * 64,
        destination_node_id=UUID("00000000-0000-0000-0000-000000000002"),
    )
    assert update_request.base_version_id == base_version_id
    assert update_request.destination_node_id == UUID(
        "00000000-0000-0000-0000-000000000002"
    )


def test_page_write_contract_limits_tags():
    valid_tags = [f"tag-{index}" for index in range(32)]
    create_request = PageCreateRequest(
        workspace_slug="default",
        slug="hello",
        title="Hello",
        body_md="# Hello",
        tags=valid_tags,
    )
    assert create_request.tags == valid_tags

    with pytest.raises(ValidationError):
        PageCreateRequest(
            workspace_slug="default",
            slug="hello",
            title="Hello",
            body_md="# Hello",
            tags=[f"tag-{index}" for index in range(33)],
        )

    with pytest.raises(ValidationError):
        PageUpdateRequest(
            body_md="# Hello",
            base_version_id=UUID("00000000-0000-0000-0000-000000000001"),
            base_content_hash="a" * 64,
            tags=["x" * 49],
        )


def test_page_create_request_caps_body_md_length():
    # At the cap is accepted, one over is rejected. Guards against unbounded
    # body_md DoS surface (anchor resolver distant fuzzy scan, version diff).
    at_limit = "x" * BODY_MD_MAX_LENGTH
    PageCreateRequest(
        workspace_slug="default",
        slug="hello",
        title="Hello",
        body_md=at_limit,
    )

    with pytest.raises(ValidationError):
        PageCreateRequest(
            workspace_slug="default",
            slug="hello",
            title="Hello",
            body_md=at_limit + "x",
        )


def test_page_write_requests_reject_empty_body_md():
    base_version_id = UUID("00000000-0000-0000-0000-000000000001")

    with pytest.raises(ValidationError):
        PageCreateRequest(
            workspace_slug="default",
            slug="hello",
            title="Hello",
            body_md="",
        )

    with pytest.raises(ValidationError):
        PageUpdateRequest(
            body_md="",
            base_version_id=base_version_id,
            base_content_hash="a" * 64,
        )


def test_page_update_request_caps_body_md_length():
    base_version_id = UUID("00000000-0000-0000-0000-000000000001")
    with pytest.raises(ValidationError):
        PageUpdateRequest(
            body_md="x" * (BODY_MD_MAX_LENGTH + 1),
            base_version_id=base_version_id,
            base_content_hash="a" * 64,
        )


def test_context_write_requests_cap_body_markdown_length():
    at_limit = "x" * BODY_MD_MAX_LENGTH
    base_version_id = UUID("00000000-0000-0000-0000-000000000001")

    WriteDraftRequest(
        title="Hello",
        page_type="overview",
        body_markdown=at_limit,
        reason="test cap",
    )
    UpdateDraftRequest(
        page_id=UUID("00000000-0000-0000-0000-000000000002"),
        base_version_id=base_version_id,
        base_content_hash="a" * 64,
        update_mode="full_rewrite",
        body_markdown=at_limit,
        reason="test cap",
    )

    with pytest.raises(ValidationError):
        WriteDraftRequest(
            title="Hello",
            page_type="overview",
            body_markdown=at_limit + "x",
            reason="test cap",
        )

    with pytest.raises(ValidationError):
        UpdateDraftRequest(
            page_id=UUID("00000000-0000-0000-0000-000000000002"),
            base_version_id=base_version_id,
            base_content_hash="a" * 64,
            update_mode="full_rewrite",
            body_markdown=at_limit + "x",
            reason="test cap",
        )

    with pytest.raises(ValidationError):
        UpdateDraftRequest(
            page_id=UUID("00000000-0000-0000-0000-000000000002"),
            base_version_id=base_version_id,
            base_content_hash="a" * 64,
            update_mode="full_rewrite",
            body_markdown="",
            reason="test cap",
        )


def test_page_summary_allows_null_current_version_id():
    # PageSummary.current_version_id mirrors ContextPage.current_version_id,
    # which is nullable between page-row insert and the first version insert.
    # Comprehensive review finding D-C2.
    from datetime import datetime

    from backend.schemas.page import PageSummary

    summary = PageSummary(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        slug="hello",
        title="Hello",
        current_version_id=None,
        updated_at=datetime(2026, 5, 22, 0, 0, 0),
        content_hash="a" * 64,
    )
    assert summary.current_version_id is None
    assert summary.current_published_version_id is None
    assert summary.version_status is None


def test_oauth_consent_data_rejects_zero_workspaces():
    """Fix-6 Task 2.2 (F5-5), restated for the (user, client) GROUP shape
    P10.3 T3 introduced: ``OAuthConsentData.workspaces`` is non-empty.

    Fix-5 removed the `None` branch from
    `OAuthService.list_consents_for_user` after F4-5 hardened the consent
    invariant — every persisted consent row named a workspace. D-01v2/T3
    changed the row unit from one workspace to a whole allowed-workspace
    SET (``workspaces: list[OAuthConsentWorkspaceData]``), but the same
    invariant still holds at the new granularity: a consent group with zero
    workspaces cannot exist through the service layer (
    issue_authorization_code/update_consent_settings both require a
    non-empty set; revoke_consent deletes the whole group rather than
    leaving it empty). Without this schema-level test, a future relax to an
    optionally-empty list would silently re-introduce the original F4-5
    attack surface (a consent whose target workspace(s) were not enforced
    at issuance).
    """
    valid = OAuthConsentData(
        client_id="client-1",
        client_name="MCP Client",
        scope="mcp:read",
        workspaces=[
            OAuthConsentWorkspaceData(
                id=UUID("00000000-0000-0000-0000-000000000001"),
                title="Default Workspace",
                slug="default",
                membership_role="owner",
            )
        ],
        granted_at="2026-05-16T00:00:00Z",
        connection_state="active",
        refresh_ttl_seconds=None,
        effective_refresh_ttl_seconds=15_552_000,
        sliding_enabled=True,
        effective_sliding_enabled=True,
        instance_max_refresh_ttl_seconds=15_552_000,
    )
    assert len(valid.workspaces) == 1
    assert isinstance(valid.workspaces[0].id, UUID)

    with pytest.raises(ValidationError):
        OAuthConsentData(
            client_id="client-1",
            client_name="MCP Client",
            scope="mcp:read",
            workspaces=[],
            granted_at="2026-05-16T00:00:00Z",
            connection_state="active",
            refresh_ttl_seconds=None,
            effective_refresh_ttl_seconds=15_552_000,
            sliding_enabled=True,
            effective_sliding_enabled=True,
            instance_max_refresh_ttl_seconds=15_552_000,
        )


def _update_draft_kwargs(**overrides):
    base = {
        "page_id": UUID("00000000-0000-0000-0000-000000000002"),
        "base_version_id": UUID("00000000-0000-0000-0000-000000000001"),
        "base_content_hash": "a" * 64,
        "reason": "test",
    }
    base.update(overrides)
    return base


def test_replace_section_update_mode_is_rejected_with_actionable_message():
    """D-1: `replace_section` never scoped to a section.

    `_body_for_update` special-cases only `append_section`; every other mode
    (including `replace_section`) returned `body_markdown` verbatim, so this
    "section" mode silently replaced the ENTIRE document body. It had zero
    production and zero test callers before this fix -- there was previously
    NO coverage of `replace_section` semantics at all, which is exactly how a
    whole-document-wipe path shipped unnoticed. This is the first test to
    assert its removal, and the message must redirect the caller to the
    correct mode rather than surface a bare enum-mismatch error.
    """
    with pytest.raises(ValidationError, match="full_rewrite"):
        UpdateDraftRequest(
            **_update_draft_kwargs(
                update_mode="replace_section",
                body_markdown="# Replacement",
            )
        )


def test_update_draft_request_rejects_target_location():
    """D-2: `target_location` was accepted but never read on the update path.

    The only real per-citation location field is `CitationInput.target_locator`
    (a different field on a different model); the update body itself has
    nothing that consumes `target_location`. The model is `extra="forbid"`, so
    a caller sending it now gets a clear unknown-field rejection instead of a
    silently-ignored parameter. No prior test covered this field's absence of
    effect, so this is the first regression test pinning its removal.
    """
    with pytest.raises(ValidationError, match="target_location"):
        UpdateDraftRequest(
            **_update_draft_kwargs(
                update_mode="full_rewrite",
                body_markdown="# Body",
                target_location={"section": "body"},
            )
        )


@pytest.mark.parametrize("update_mode", ["citation_update", "metadata_update"])
def test_body_markdown_is_rejected_on_non_body_update_modes(update_mode):
    """`citation_update` / `metadata_update` never write the page body.

    Before this fix nothing stopped a caller from sending `body_markdown`
    alongside these modes -- it was silently ignored by `_body_for_update`,
    which only inspects `body_markdown` for `append_section`/`full_rewrite`
    (falling through to `patch_description` or the unchanged base body
    otherwise). Reject it explicitly at the schema boundary so a caller's
    mistaken belief that it did something is caught immediately rather than
    discovered later as a "why didn't my edit apply" bug report. First
    coverage of this rejection.
    """
    kwargs = _update_draft_kwargs(update_mode=update_mode, body_markdown="# Body")
    if update_mode == "citation_update":
        kwargs["citations"] = [
            CitationInput(
                citation_type="context_page",
                cited_context_page_version_id=UUID(
                    "00000000-0000-0000-0000-000000000003"
                ),
            )
        ]
    else:
        kwargs["metadata_patch"] = PageMetadataPatch(title="New title")
    with pytest.raises(ValidationError, match="body_markdown"):
        UpdateDraftRequest(**kwargs)


def test_update_draft_request_supported_modes_still_work():
    """Guard the four surviving `update_mode` values against regressions

    from the `replace_section` / `target_location` removal and the new
    `body_markdown` rejection on `citation_update` / `metadata_update`.
    """
    UpdateDraftRequest(
        **_update_draft_kwargs(update_mode="full_rewrite", body_markdown="# Body")
    )
    UpdateDraftRequest(
        **_update_draft_kwargs(
            update_mode="append_section", body_markdown="More content"
        )
    )
    UpdateDraftRequest(
        **_update_draft_kwargs(
            update_mode="citation_update",
            citations=[
                CitationInput(
                    citation_type="context_page",
                    cited_context_page_version_id=UUID(
                        "00000000-0000-0000-0000-000000000003"
                    ),
                )
            ],
        )
    )
    UpdateDraftRequest(
        **_update_draft_kwargs(
            update_mode="metadata_update",
            metadata_patch=PageMetadataPatch(title="New title"),
        )
    )
