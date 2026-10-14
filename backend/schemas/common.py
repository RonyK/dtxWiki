from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Envelope FAMILY markers. These name the shape of the result envelope a
# caller has to parse -- `llmwiki.mcp.v1` is pinned as a `Literal` on
# MCPResultEnvelope, is the `v1` in the checked-in `shared/schemas/mcp/v1/`
# path, and has a REST twin. They are a family label, not a change counter:
# folding 14 audit fields into `envelope.meta` changed the envelope and did
# not move this string, and deleting three tools moved the catalog without
# touching the envelope at all. Bumping it to `v2` is a whole-wire migration
# (every consumer, both schema directories, both Literals), which is why it
# cannot double as the marker that says "the tool contract moved".
MCP_SCHEMA_VERSION = "llmwiki.mcp.v1"
REST_SCHEMA_VERSION = "llmwiki.rest.v1"

# TOOL CATALOG contract revision -- a different axis from MCP_SCHEMA_VERSION,
# not a competing marker for the same one. It answers "which tools exist, what
# do they accept, and what shape do they return", where MCP_SCHEMA_VERSION
# answers "how do I parse the envelope those results are wrapped in". It is
# deliberately nested under the envelope family on the wire
# (`llmwiki.mcp.v1.tools.<N>`) so the two read as one lineage rather than as
# rival version numbers.
#
# Bump this whenever the tool contract changes in a way a client can observe:
# a tool added/removed/renamed, an input schema changed, a result projection
# changed (including a field that only ever appears inside `envelope.meta`),
# or the model-facing envelope allowlist widened. The digest half of the
# published version (see backend/mcp/protocol/tool_descriptors.py) is derived
# from the live catalog and moves on its own, so forgetting to bump this
# degrades the marker's readability, not its truthfulness.
#
# Revision history:
#   1 -- Epic-1 wire contract: outputSchema off `tools/list`, three legacy
#        page tools deleted, 14 envelope fields folded into `envelope.meta`,
#        and the two paginated read projections (read_context_outline,
#        read_structure_map) grown cursors, with `document.block_count`
#        respecified as the document total rather than `len(blocks)`.
#   2 -- P3 (ContextMediaSession removal): `upload_image` input drops
#        `media_session_id`, requires `page_id` and `editing_version_id` both
#        (no more page-less upload); its result drops `media_session_id` and
#        `content_url` now names a `pages/{page_id}/versions/{version_id}/
#        media/{asset_id}/content` path instead of a media-session path;
#        `write_draft` input drops `media_session_id`.
#   3 -- P6.2 (editing_revision conflict-check retrofit): `update_draft` (and
#        `submit_revision_for_review`, which shares the request model) gains
#        an optional `expected_editing_revision` input field -- base_content_
#        hash alone cannot catch a metadata-only change (title/tags/aliases)
#        on the same version row, so a stale caller whose body still matched
#        could silently clobber one. `write_draft` and `update_draft` results
#        both gain `editing_revision` so a caller can chain the value into
#        its next write.
#   4 -- P9.1 (canonical Context route family): `upload_image`'s result
#        `content_url` now names a
#        `workspaces/{workspace_id}/context/pages/{page_id}/versions/
#        {version_id}/media/{asset_id}/content` path (workspace_id-first,
#        under the new `/context/pages` route family) instead of the
#        deleted `pages/{page_id}/versions/{version_id}/media/{asset_id}/
#        content` path. No tool added/removed/renamed and no input schema
#        changed -- only this one result field's URL shape moved, because
#        the REST route it names moved.
#   5 -- P8.6 (Context-editing-state-machine MCP tools, partial): six tools
#        added -- create_context_page, begin_context_edit,
#        update_context_edit, submit_context_edit_for_review,
#        discard_context_edit, list_my_context_edits -- each a thin wrapper
#        over the PageService P8 command Browser REST already calls
#        (backend/api/context_pages.py), so the two surfaces share exactly
#        one implementation per command. The old write_draft/update_draft/
#        patch_draft/submit_revision_for_review surface is UNCHANGED and
#        stays live alongside the new one: two of those tools remain
#        load-bearing for reconciliation/changes-requested-review flows this
#        pass does not replace, and patch_context_edit (the seventh tool of
#        the eventual clean target) is deferred pending an open architecture
#        decision. Removing the old surface is a separate, later change.
#   6 -- P8.6 (Context-editing-state-machine MCP tools, complete): the
#        seventh and final tool, patch_context_edit, added. Applies the same
#        typed EditOperation union patch_draft accepts (minus
#        resolve_conflict, reconciliation-only) to the caller's own open
#        "editing" row in place via context_editing.apply_operations_with_
#        block_map, reusing autosave_page's in-place body-persistence fields
#        -- never a new version, never patch_draft/ContextWriteService/
#        reconcile_branch, which remain untouched and out of scope. No input
#        or output schema of any existing tool changed.
#   7 -- P8.6 (old Context-write MCP tools deregistered): write_draft,
#        update_draft, and patch_draft removed from the MCP tool catalog --
#        their wire-level registration, schemas, scopes, dispatch, and
#        redaction only. Their backing ContextWriteService.write_draft/
#        update_draft/patch_draft Python methods are unchanged and stay
#        load-bearing for two internal (non-MCP) callers: PageService.
#        reconcile_branch (patch_draft) and ReviewService.
#        submit_revision_for_review (update_draft). submit_revision_for_review
#        the MCP tool is unaffected and stays registered as-is. Three tools
#        removed, no input/output schema of any remaining tool changed.
#   8 -- P10.3 T2 (D-01v2 credential redesign): list_my_workspaces' output
#        schema (WorkspaceListData, shared with REST GET /workspaces) gained
#        one field, other_membership_count -- the count of the actor's live
#        WorkspaceMembership rows a pat/oauth_jwt credential's allowed
#        workspace set does not cover (no id/title, just a count; expanding
#        the allowed set to reach one is now an explicit MCP Access UI
#        action). No tool added/removed/renamed, no other schema changed.
#   9 -- MCP edit-failure fixes: every advertised inputSchema is projected to
#        the portable subset hosts convert ($ref inlined, unions flattened,
#        const -> enum; backend/mcp/protocol/schema_projection.py), so
#        patch_context_edit's operations are typed instead of Array<unknown>.
#        create/update/patch_context_edit data gained edit_receipt;
#        read_structure_map/read_context_outline accept version="my_edit" and
#        the structure-map document gained editing_revision,
#        resolved_version_source and map_repaired; update_context_edit's
#        base_version_id/base_content_hash became optional; page payloads
#        gained inconsistency_reasons. replace_text gained match_mode and a
#        block `at` address, accept_fuzzy moved off the operations that
#        cannot take an anchor, receipt operations gained match_kind/
#        match_similarity/match_policy/matched_sha256, and MCPErrorDetail gained
#        operation_index and candidates (new codes text_match_not_found/
#        _ambiguous/_low_confidence). No tool added/removed/renamed.
#  10 -- Descriptions only, from a test that had three models solve eleven
#        editing tasks from what tools/list and guide say: heading_path is
#        described as the full path from the page's top heading (every model
#        wrote just the last heading); patch_context_edit's operations and
#        every op's `content` say replace_block/insert_row take one block/row
#        and that replacing a block and inserting after it in one call
#        overlaps; the receipt wording says an edited block keeps its id;
#        match_mode lists what must still match exactly. No field, tool or
#        schema shape changed.
#  11 -- update_context_edit gained an optional version_id binding the save
#        to the editing row the caller read (a conflict when another row is
#        open, or none). Conflicts on the caller's own open edit name its
#        version_id and editing_revision in error_details, and the guidance
#        says to re-read before retrying instead of resending with the new
#        revision. Descriptions of update/patch_context_edit and the edit
#        receipt say when a block keeps its id and that update_context_edit
#        with no open edit stages a reviewed draft. No tool added/removed.
#  12 -- Context write tools and guide gained authored-link guidance; every
#        envelope gained the context_page_has_no_graph_visible_links advisory.
#        Existing edit receipts and version-bound saves remain supported.
MCP_TOOL_CONTRACT_REVISION = 12
ERROR_CODES = (
    "not_found",
    "stale_base_version",
    "stale_context_draft",
    # patch_draft push semantics (design doc section 5): `base` names
    # neither a version id nor a content_hash of this page.
    "base_not_found",
    # Omitted base has no durable conversation read ledger to resolve from.
    "base_required",
    # A legacy content-hash base names more than one version on this page;
    # callers must use read_structure_map.document.version_id instead.
    "base_ambiguous",
    # patch_draft section 6.2: `reconciles` does not name an active
    # diverged branch of this page, or its own operations are not
    # block-addressed.
    "reconcile_target_invalid",
    # Partial-edit refusals. These are the codes the edit pipeline actually
    # raises (backend/services/context_editing.py); the design draft used
    # different names for several of them, so keep this list derived from the
    # implementation, not from the prose.
    "text_occurrence_mismatch",
    # replace_text's tiered matcher (backend/services/text_matching.py) found
    # no usable target: nothing close enough, more than one place about as
    # close, or one close place whose differences it will not guess across.
    # Each carries the nearest candidates by line and block, never text.
    "text_match_not_found",
    "text_match_ambiguous",
    "text_match_low_confidence",
    "section_not_found",
    "section_hash_mismatch",
    # Block-addressed operations (design doc
    # doc/design/block-provenance-contract.md section 2).
    "block_not_found",
    "block_hash_mismatch",
    "row_not_found",
    "row_hash_mismatch",
    "section_position_invalid",
    "heading_level_invalid",
    "anchor_not_found",
    "anchor_confidence_low",
    "operations_overlap",
    "operation_unsupported",
    # An operation's content re-parsed a block it did not target (lazy
    # continuation, an unclosed fence). Recoverable by isolating the content.
    "block_reflow",
    "body_length_exceeded",
    "context_history_state_inconsistent",
    "validation_failed",
    "unauthorized",
    "unknown_tool",
    "internal_error",
    "legacy_media_cutover_conflict",
    "database_schema_mismatch",
    "bundle_expired",
    "inconsistent_identifier",
    "mcp_client_workspace_denied",
    "mcp_client_disabled",
    "mcp_client_deleted",
    "rejected_by_policy",
    "page_name_occupied",
    "disambiguation_required",
    "restricted_library_denied",
    "unsupported_retrieval_mode",
    "scope_denied",
    "token_workspace_denied",
    "too_large",
    "unsupported_mime",
    "unknown_actor_kind",
    "workspace_membership_missing",
    "workspace_role_insufficient",
    # D-01v3-e: the target workspace's own database plane could not be
    # reached/queried while the identity/control plane stayed reachable.
    # Distinct from every workspace_* denial above -- those are considered
    # decisions with a working answer; this one means no answer was
    # available, so the caller fails closed rather than allowing on the
    # control-plane check alone.
    "workspace_plane_unavailable",
    # D-01v3-b: the target workspace's own plane answered, but this
    # credential has no live (non-revoked) workspace_credential_grant
    # replica row there yet -- either the fan-out from an expansion has not
    # landed (or the reconciler has not retried it), or the replica was
    # tombstoned. Distinct from workspace_plane_unavailable (no answer at
    # all) and from every workspace_* denial above (a considered, stable
    # decision) -- this one is expected to resolve on its own shortly.
    "grant_replica_missing",
    "mcp_connection_disabled",
    "conversation_token_malformed",
    "conversation_token_required",
    "revision_submission_required",
    "conversation_token_unknown",
    "conversation_token_owner_mismatch",
    "human_session_required",
    "user_not_found",
    "chat_owner_unresolvable",
    "review_already_claimed",
    "page_has_no_published_version",
    "client_state_invalid",
    "client_state_expired",
    "idempotency_key_conflict",
    "embedded_image_requires_upload",
    "media_markdown_policy_invalid",
    "media_content_mismatch",
    "media_install_in_progress",
    "media_install_unavailable",
    "media_quota_exceeded",
    "media_policy_changed",
    "media_owner_mismatch",
    "media_editing_not_open",
    "media_staging_bytes_exceeded",
    "media_staging_limit_exceeded",
    "media_state_changed",
    "media_too_large",
    "media_type_unsupported",
    "media_unavailable",
    "request_too_large",
)

ContextHistoryStatus = Literal[
    "state_inconsistent",
    "current_editing",
    "current_draft",
    "published_snapshot",
    "alternative_draft",
    "discarded_draft",
    "superseded_snapshot",
    "superseded_draft",
]
WARNING_CODES = (
    "stale",
    "permission_limited",
    "citation_missing",
    "citation_weak",
    "citation_stale",
    "token_budget",
    "workflow_sequence_incomplete",
    "draft_not_injectable",
    "expired_context",
    "archived",
    "unsupported_retrieval_mode_fallback",
    "deprecated_tool",
    "conversation_token_not_supplied",
    "unresolved_diverged_branch",
    "conversation_read_stale",
    # The write succeeded but the body failed the objective format floor, so it
    # is held as a draft rather than published. Carries the defect and its line.
    "lint_publish_blocked",
    # The caller explicitly published despite objective format findings. Carries
    # the findings in editor coordinates so the UI can warn without guessing.
    "published_with_findings",
    # The returned version is an unpublished draft. This wording is safe on
    # reads as well as writes and does not imply that a save failed.
    "draft_unpublished",
    # A patch was refused because old_text did not match uniquely. The
    # caller does not hold the document, so these carry the actual match
    # count and the headings the matches fall under -- enough to retry
    # without re-reading the page, which is the cost patching removes.
    "text_occurrence_mismatch",
    "text_not_found",
    # Search advisory codes (design §8.2). unsupported_filter_*,
    # disambiguation_required, and explicit semantic/hybrid fallback fire
    # today; coverage_gap_recorded is emitted by Ask/REST/MCP zero-hit paths.
    "unsupported_filter_tag",
    "unsupported_filter_date",
    "disambiguation_required",
    "semantic_fallback_used",
    "keyword_lane_unavailable",
    "coverage_gap_recorded",
    "evidence_not_citable",
    "mrtr_preview_unavailable",
    "borrowed_rerank_applied",
    "borrowed_query_rewrite_applied",
    # Ask answer-pipeline degradation. The answer is still served from the
    # deterministic evidence pack; these say why it is not the composed one.
    "llm_provider_unavailable",
    "llm_composer_failed",
    # Epic-2 Phase 6a fan-out (doc/design/workspace-database-separation-plan.md
    # §4): one or more workspace shards could not be reached while building a
    # cross-workspace admin job listing/summary, so the result is a partial
    # one -- the reachable workspaces' data, not every workspace's.
    "job_fanout_incomplete",
    "context_page_has_no_graph_visible_links",
)

# Single source for retrieval capability + search scope (REST, MCP, services).
RetrievalMode = Literal["keyword", "semantic", "hybrid"]
SearchScope = Literal[
    "context_page",
    "context_claim",
    "library_item",
    "library_artifact",
    "feedback",
    "change",
    "agent_page",
    "all",
]


class TextMatchCandidate(BaseModel):
    """Where replace_text's old_text nearly matched, by position only.

    Evidence for a refused match, so the caller can re-address one block
    instead of re-reading the page. It deliberately carries no text: the
    candidate is page content, and echoing it would put authored text into
    server guidance and the durable trace.
    """

    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    # The read_structure_map block holding the candidate, when the block map
    # is available and the candidate sits inside one block.
    block_id: str | None = None
    similarity: float = Field(ge=0, le=1)
    match_kind: Literal["exact", "normalized", "fuzzy"]

    model_config = ConfigDict(extra="forbid")


class MCPErrorDetail(BaseModel):
    """One actionable defect in a rejected tool call.

    Shaped for a caller that has to repair its own request: `field` locates the
    problem, `message` states it, and `fix` says what to send instead. Never
    echo the offending value here — a rejected `body_markdown` can be a
    megabyte, and reflecting client input back into a response (and into the
    stored trace) is both wasteful and an injection vector.
    """

    field: str | None = None
    code: str
    message: str
    fix: str | None = None
    # The failing entry of a patch_context_edit batch (0-based), so a caller
    # repairs that one operation instead of bisecting the batch.
    operation_index: int | None = Field(default=None, ge=0)
    candidates: list[TextMatchCandidate] | None = None

    model_config = ConfigDict(extra="forbid")


class PageNameDisambiguationCandidate(BaseModel):
    page_id: UUID
    slug: str
    title: str
    placement_parent_node_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")


class DraftReviewReason(BaseModel):
    """One reason a write did not simply publish.

    Lives here rather than beside either write surface because both use it: the
    MCP result names why a draft went to review, and the REST page payload names
    why a save was held from publication.
    """

    code: str
    message: str

    model_config = ConfigDict(extra="forbid")


class EnvelopeWarning(BaseModel):
    """Structured warning attached to MCP/REST envelope responses.

    Hotfix-2 renamed this model to avoid shadowing Python's builtin warning
    base class. Wire shape (``code`` + ``message``) is unchanged; only the
    Python class name and JSON Schema title move.
    """

    code: str = Field(
        ...,
        json_schema_extra={"enum": list(WARNING_CODES)},
        description="Stable warning code; must be drawn from WARNING_CODES.",
    )
    message: str

    model_config = ConfigDict(extra="forbid")

    @field_validator("code")
    @classmethod
    def _code_in_registry(cls, v: str) -> str:
        if v not in WARNING_CODES:
            raise ValueError(f"unknown warning code: {v!r}")
        return v


ResultStatus = Literal[
    "accepted",
    "succeeded",
    "failed",
    "partial",
    "rejected_by_policy",
    "needs_review",
    "conflict",
]
ActorKind = Literal["human", "mcp_client", "agent", "system"]


class MCPActor(BaseModel):
    kind: ActorKind
    id: str

    model_config = ConfigDict(extra="forbid")


class MCPResultEnvelope(BaseModel):
    schema_version: Literal["llmwiki.mcp.v1"] = MCP_SCHEMA_VERSION
    request_id: UUID = Field(default_factory=uuid4)
    result_status: ResultStatus
    workspace_id: UUID | None = None
    # Optional on the v1 wire: actor_kind/actor_id remain the canonical legacy
    # fields, so an old v1 consumer that predates the structured `actor` object is
    # not rejected for a missing required field (Codex #8). The before-validator
    # still synthesizes `actor` from the legacy fields for current consumers.
    actor: MCPActor | None = None
    actor_kind: ActorKind | None = None
    actor_id: str | None = None
    result_summary: str | None = None
    audit_event_id: UUID | None = None
    activity_ref: UUID | None = None
    provider: str = "unknown"
    model_id: str = "unknown"
    model_version: str | None = None
    params_summary: dict[str, object] = Field(default_factory=dict)
    client_self_reported: bool = False
    error_code: str | None = None
    # A code alone is not actionable. REST consumers already receive
    # `RESTErrorBody.message` for the very same service exception, so an MCP
    # client was the only caller left guessing. These fields close that gap:
    # `error_message` says what went wrong, `error_details` says which field,
    # and `retryable` says whether resending the identical call can ever work
    # (an agent that cannot tell will loop).
    error_message: str | None = None
    error_details: list[MCPErrorDetail] = Field(default_factory=list)
    retryable: bool | None = None
    review_item_id: UUID | None = None
    warnings: list[EnvelopeWarning] = Field(default_factory=list)
    conversation_token: str | None = Field(
        default=None,
        description=(
            "Opaque MCP conversation token. Echo this value on the next tool "
            "call to append to the same dtxWiki MCP chat."
        ),
    )
    # The wire-slimming pass (runner._slim_envelope_for_wire) folds the 13
    # audit/telemetry fields not in its model-facing allowlist into this
    # bucket instead of repeating them in every tool's outputSchema. A real,
    # typed field here -- instead of an ad-hoc injected key -- is what keeps
    # the wire shape EXPRESSIBLE in the checked-in
    # shared/schemas/mcp/v1/*.json contract; the model itself never sets this,
    # only the wire-trim step does.
    #
    # It does not make the checked-in contract DESCRIBE the wire shape, and
    # this comment used to say that it did. The exported schema still declares
    # all 22 envelope properties, because that is the internal model; the
    # server enforces a stricter 9-fields-plus-meta shape on the wire, derived
    # in memory by output_validation._wire_schemas() and never exported. A
    # client validating against the checked-in JSON is therefore checking a
    # SUPERSET of what it will actually receive -- which is sound (every wire
    # payload satisfies it) but is not the same statement.
    meta: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _populate_actor_from_legacy_fields(cls, data: object) -> object:
        if not isinstance(data, dict) or data.get("actor") is not None:
            return data
        actor_kind = data.get("actor_kind")
        actor_id = data.get("actor_id")
        if actor_kind is None or actor_id is None:
            return data
        return {**data, "actor": {"kind": actor_kind, "id": actor_id}}

    @model_validator(mode="after")
    def _populate_legacy_actor_fields(self) -> "MCPResultEnvelope":
        if self.actor is not None:
            if self.actor_kind is None:
                self.actor_kind = self.actor.kind
            if self.actor_id is None:
                self.actor_id = self.actor.id
        return self


class MCPConversationInput(BaseModel):
    conversation_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        description=(
            "Opaque MCP conversation token. Omit or pass 'new' to start a "
            "new dtxWiki MCP chat; echo the returned envelope value to append."
        ),
    )


class RESTResultEnvelope(BaseModel):
    schema_version: Literal["llmwiki.rest.v1"] = REST_SCHEMA_VERSION
    request_id: UUID = Field(default_factory=uuid4)
    result_status: ResultStatus = "succeeded"
    actor_kind: ActorKind | None = None
    actor_id: str | None = None
    audit_event_id: UUID | None = None
    error_code: str | None = None
    warnings: list[EnvelopeWarning] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


def rest_success_envelope() -> RESTResultEnvelope:
    return RESTResultEnvelope(result_status="succeeded")


class RESTResponseBase(BaseModel):
    envelope: RESTResultEnvelope = Field(default_factory=rest_success_envelope)

    model_config = ConfigDict(extra="forbid")


class RESTErrorBody(BaseModel):
    envelope: RESTResultEnvelope
    error_code: str
    message: str
    request_id: UUID = Field(default_factory=uuid4)
    warnings: list[EnvelopeWarning] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class RESTBlocker(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    count: int = Field(ge=1)

    model_config = ConfigDict(extra="forbid")


class RESTConflictErrorBody(RESTErrorBody):
    server_version_id: UUID | None = None
    server_content_hash: str | None = None
    limit_bytes: int | None = None
    settings_link: str | None = None
    candidates: list[PageNameDisambiguationCandidate] | None = None
    blockers: list[RESTBlocker] | None = None
    page_id: UUID | None = None
    # PURGE_AUTHZ_PLAN S-7: only present on a workspace-delete 409 that carries
    # blockers (a purge-recoverable refusal). extra="forbid" on this model
    # means build_rest_error_response(extra=...) cannot inject these unless
    # they are declared fields.
    purge_grant: str | None = None
    purge_grant_expires_at: datetime | None = None
