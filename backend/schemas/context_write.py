from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.schemas.common import DraftReviewReason, RESTErrorBody, RESTResponseBase
from backend.schemas.input_policy import InputRequest
from backend.schemas.page import (
    BODY_MD_MAX_LENGTH,
    CONTENT_HASH_PATTERN,
    PageMetadataPatch,
)

CONTEXT_PAGE_TYPES = (
    "overview",
    "source",
    "entity",
    "concept",
    "synthesis",
    "comparison",
    "decision",
    "contradiction",
    "query",
    "guide",
)


class CitationInput(BaseModel):
    target_kind: Literal[
        "context_claim",
        "context_page_section",
        "context_page_whole",
    ] = "context_claim"
    target_locator: dict[str, Any] | None = None
    citation_type: Literal["raw_source", "context_page", "derived_artifact"]
    library_item_version_id: UUID | None = None
    library_artifact_id: UUID | None = None
    cited_context_page_version_id: UUID | None = None
    location: dict[str, Any] | None = None
    snippet: str | None = None
    created_via: Literal["cite_tool", "manual_input"] = "cite_tool"

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_citation_target(self):
        if self.citation_type == "raw_source":
            if (
                self.library_item_version_id is None
                or self.library_artifact_id is not None
                or self.cited_context_page_version_id is not None
            ):
                raise ValueError(
                    "raw_source citations require only library_item_version_id"
                )
        elif self.citation_type == "derived_artifact":
            if (
                self.library_artifact_id is None
                or self.library_item_version_id is not None
                or self.cited_context_page_version_id is not None
            ):
                raise ValueError(
                    "derived_artifact citations require only library_artifact_id"
                )
        elif (
            self.cited_context_page_version_id is None
            or self.library_item_version_id is not None
            or self.library_artifact_id is not None
        ):
            raise ValueError(
                "context_page citations require only cited_context_page_version_id"
            )
        return self


_NEEDS_REVIEW_DESCRIPTION = (
    "Reserve true for genuine exceptions that need human attention: changes that "
    "are uncertain, contradict existing content, or are high-stakes. Leave routine "
    "edits false (the default) so the workspace review policy can route them. The "
    "server independently forces review on objective grounds such as lint errors."
)


class WriteDraftRequest(InputRequest):
    title: str = Field(min_length=1)
    page_type: Literal[
        "overview",
        "source",
        "entity",
        "concept",
        "synthesis",
        "comparison",
        "decision",
        "contradiction",
        "query",
        "guide",
    ]
    body_markdown: str = Field(min_length=1, max_length=BODY_MD_MAX_LENGTH)
    citations: list[CitationInput] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    source_context: list[UUID] = Field(default_factory=list)
    risk_notes: str | None = None
    placement_parent_node_id: UUID | None = Field(
        default=None,
        description=(
            "Context tree parent node UUID. If omitted, a new page goes to "
            "Inbox. To choose a location, read guide placement_parent_candidates "
            "and pass its node_id; candidate paths are discovery hints, not "
            "write inputs. A failed placement rejects and rolls back the entire "
            "write."
        ),
    )
    needs_review: bool = Field(default=False, description=_NEEDS_REVIEW_DESCRIPTION)

    model_config = ConfigDict(extra="forbid")

    @field_validator("body_markdown")
    @classmethod
    def _body_not_blank(cls, value: str) -> str:
        # I13: min_length=1 still admits all-whitespace (" "); a new page needs
        # real content (and an all-whitespace body normalizes away under Auto-fix).
        if not value.strip():
            raise ValueError("body_markdown must not be only whitespace")
        return value

    @model_validator(mode="after")
    def _reject_unsupported_source_context(self):
        if self.source_context:
            raise ValueError(
                "source_context is not supported; use citations to record sources"
            )
        return self


class WriteDraftResult(BaseModel):
    page_id: UUID
    draft_version_id: UUID
    change_set_id: UUID
    review_item_id: UUID
    # Additive, nullable defaults keep persisted idempotency results from older
    # builds readable while new writes return a complete post-write snapshot.
    persisted_content_hash: str | None = Field(
        default=None,
        pattern=CONTENT_HASH_PATTERN,
    )
    persisted_character_count: int | None = Field(default=None, ge=0)
    outcome: (
        Literal[
            "created_draft",
            "updated_draft",
            "no_change",
            "review_only",
        ]
        | None
    ) = None
    body_changed: bool | None = None
    metadata_changed: bool | None = None
    version_status: (
        Literal[
            "draft",
            "published_snapshot",
            "superseded_snapshot",
        ]
        | None
    ) = None
    latest_draft_version_id: UUID | None = None
    current_published_version_id: UUID | None = None
    review_status: str | None = None
    # See UpdateDraftRequest.expected_editing_revision -- a caller chains this
    # value into its first update_draft call on the version this created.
    editing_revision: int | None = Field(default=None, ge=1)
    citation_warnings: list[str] = Field(default_factory=list)
    # `next_action="review_required"` used to be the whole story: the write
    # succeeded, a human now has to look at it, and the author was never told
    # what to change. A lint error here is a concrete, fixable defect (an
    # unclosed code fence, with its line number), so name it.
    review_reasons: list[DraftReviewReason] = Field(default_factory=list)
    next_action: Literal[
        "review_required",
        "more_citation_needed",
        "ready_for_review",
    ]

    model_config = ConfigDict(extra="forbid")


class UpdateDraftRequest(InputRequest):
    page_id: UUID
    base_version_id: UUID
    base_content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    # base_content_hash alone only covers the markdown body, so a save that
    # changes only title/tags/aliases/metadata does not move it -- a second,
    # stale caller whose body still matches would pass the hash check and
    # silently overwrite the metadata change. A caller that states the
    # editing_revision value it last saw closes that gap (see
    # ContextPageVersion.editing_revision's docstring); omitting it skips only
    # this narrower check, the base_version_id/base_content_hash check above
    # still applies.
    expected_editing_revision: int | None = Field(default=None, ge=1)
    on_stale: Literal["conflict"] = "conflict"
    update_mode: Literal[
        "append_section",
        "full_rewrite",
        "citation_update",
        "metadata_update",
    ]
    body_markdown: str | None = Field(
        default=None,
        min_length=1,
        max_length=BODY_MD_MAX_LENGTH,
    )
    patch_description: str | None = None
    citations: list[CitationInput] = Field(default_factory=list)
    metadata_patch: PageMetadataPatch | None = None
    reason: str = Field(min_length=1)
    feedback_id: UUID | None = None
    needs_review: bool = Field(default=False, description=_NEEDS_REVIEW_DESCRIPTION)

    model_config = ConfigDict(extra="forbid")

    @field_validator("update_mode", mode="before")
    @classmethod
    def _reject_replace_section(cls, value):
        # D-1: `replace_section` claimed to replace one section but
        # `_body_for_update` only special-cases `append_section` -- every other
        # mode, including this one, returns `body_markdown` verbatim, so it
        # silently wiped the whole document. It has zero production and test
        # callers, so it is removed outright rather than deprecated. Intercept
        # the literal value here (before the Literal check) so removal reads
        # as an actionable message instead of a generic enum-mismatch error.
        if value == "replace_section":
            raise ValueError(
                "replace_section was removed: it silently replaced the entire "
                "document instead of a section. Use full_rewrite for a "
                "whole-document replacement; section-scoped editing will "
                "arrive later as a separate tool."
            )
        return value

    @model_validator(mode="after")
    def validate_update_mode_payload(self):
        if self.update_mode in {
            "append_section",
            "full_rewrite",
        } and not (self.body_markdown or self.patch_description):
            raise ValueError("body_markdown or patch_description is required")
        # I13: an all-whitespace body for a body-writing mode is a degenerate
        # edit (it normalizes away under Auto-fix and would auto-approve as a
        # "format-only" no-op); reject it at the boundary.
        if (
            self.update_mode in {"append_section", "full_rewrite"}
            and self.body_markdown is not None
            and not self.body_markdown.strip()
        ):
            raise ValueError("body_markdown must not be only whitespace")
        if self.update_mode == "citation_update" and not self.citations:
            raise ValueError("citations are required for citation_update")
        if self.update_mode == "metadata_update" and not self.metadata_patch:
            raise ValueError("metadata_patch is required for metadata_update")
        if (
            self.update_mode in {"citation_update", "metadata_update"}
            and self.body_markdown is not None
        ):
            # Constant message on purpose: the static MCP validation contract
            # (tests/static/test_mcp_validation_contract.py) forbids building a
            # validator message from the rejected input, because that message
            # travels to the client and into the durable trace. Both modes are
            # named literally rather than interpolated.
            raise ValueError(
                "body_markdown is not supported for citation_update or "
                "metadata_update; neither mode writes the page body"
            )
        return self


# Consumed by the MCP receipt adapter, which wraps page-authored text
# (resolved_heading_path, anchor_preview) as untrusted before it reaches the
# response. Deliberately plain str/dict rather than the MCP layer's
# UntrustedPageText/ResolvedLineRange: the MCP schema module already imports
# FROM here, so the reverse import would cycle.
class PatchOperationResolution(BaseModel):
    """What the server actually resolved for one ``patch_draft`` operation.

    A null field means that field genuinely does not apply to this
    operation's kind (e.g. ``old_section_hash`` for ``insert_section``, which
    has nothing to have replaced).
    """

    op: str
    resolved_heading_path: list[str] | None = None
    resolved_line_range: dict[str, int] | None = None
    anchor_status: str | None = None
    anchor_preview: str | None = None
    old_section_hash: str | None = None
    new_section_hash: str | None = None
    chars_delta: int | None = None
    # Optional defaults preserve durable UpdateDraftResult rows written before
    # operation outcomes were added. Current patch_draft writes populate both.
    outcome: Literal["applied", "already_landed", "no_effect"] | None = Field(
        default=None,
        description=(
            "Whether this execution applied the operation or proved its effect "
            "was already present (a content-identical replace_text/replace_section/ "
            "delete_section). 'no_effect' means the caller authored no delta "
            "relative to the effective base, so a moved head is retained "
            "without falsely claiming the requested content already landed. "
            "Every insertion now applies -- retries are replayed from the "
            "idempotency ledger before reaching this engine. Null only when "
            "replaying a legacy stored result."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_landing_match(cls, value):
        """Consume, but do not re-emit, pre-ledger natural-retry evidence.

        Durable idempotency rows remain replayable for 24 hours across the
        contract replacement. The engine no longer produces or infers this
        field, and the new public receipt intentionally has no such property.
        """

        if isinstance(value, dict) and "landing_match" in value:
            value = dict(value)
            value.pop("landing_match", None)
        return value

    model_config = ConfigDict(extra="forbid")


class BlockMapDeltaNode(BaseModel):
    """One content-bearing identity in a patch receipt's structure-map delta."""

    id: str = Field(min_length=1)
    hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    node_type: Literal["block", "row"]
    kind: str | None = None
    parent_block_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class BlockMapDeltaEntry(BaseModel):
    """An identity/hash transition caused by one completed patch."""

    change: Literal["added", "replaced", "removed"]
    old: BlockMapDeltaNode | None = None
    new: BlockMapDeltaNode | None = None

    @model_validator(mode="after")
    def _validate_sides(self):
        if self.change == "added" and (self.old is not None or self.new is None):
            raise ValueError("added block-map delta requires only new")
        if self.change == "removed" and (self.old is None or self.new is not None):
            raise ValueError("removed block-map delta requires only old")
        if self.change == "replaced" and (self.old is None or self.new is None):
            raise ValueError("replaced block-map delta requires old and new")
        return self

    model_config = ConfigDict(extra="forbid")


class UpdateDraftResult(BaseModel):
    draft_version_id: UUID | None = None
    change_set_id: UUID | None = None
    review_item_id: UUID | None = None
    diff_summary: str
    # Optional defaults are required for replaying idempotency rows written by
    # an older server. Successful current writes populate every field; conflict
    # payloads leave state that cannot be proven from the exception as null.
    persisted_content_hash: str | None = Field(
        default=None,
        pattern=CONTENT_HASH_PATTERN,
    )
    persisted_character_count: int | None = Field(default=None, ge=0)
    outcome: (
        Literal[
            "created_draft",
            "updated_draft",
            "no_change",
            "review_only",
        ]
        | None
    ) = None
    body_changed: bool | None = None
    metadata_changed: bool | None = None
    version_status: (
        Literal[
            "draft",
            "published_snapshot",
            "superseded_snapshot",
        ]
        | None
    ) = None
    latest_draft_version_id: UUID | None = None
    current_published_version_id: UUID | None = None
    review_status: str | None = None
    latest_version_id: UUID | None = None
    latest_content_hash: str | None = None
    # See PageUpdateRequest.expected_editing_revision. Null for the same
    # reason persisted_content_hash is null on a conflict payload: state that
    # cannot be proven from the exception.
    editing_revision: int | None = Field(default=None, ge=1)
    alternative_draft_id: UUID | None = None
    # Mirrors WriteDraftResult. `update_draft` runs the same auto-approval gate
    # and computes the same findings, and dropping them meant an agent learned
    # why its *first* write needed review and never again for any edit after it.
    review_reasons: list[DraftReviewReason] = Field(default_factory=list)
    # The three below back patch_draft's receipt (PatchDraftReceipt). They are
    # null, not a legacy default, for a caller that supplies no per-operation
    # target ranges (update_draft) or never reaches classification (a plain
    # no-op) -- see ContextWriteService._persist_context_update.
    normalized: bool | None = None
    outside_target_changed: bool | None = None
    risk: str | None = None
    operation_receipts: list[PatchOperationResolution] | None = None
    # Internal handoff for the REST reconciliation preview. Excluded from the
    # generic update/MCP wire model so page-authored Markdown is exposed only
    # by the purpose-built, authorization-checked reconciliation endpoint.
    preview_body_markdown: str | None = Field(default=None, exclude=True)
    block_map_delta: list[BlockMapDeltaEntry] | None = Field(
        default=None,
        description=(
            "patch_draft only: server-derived added/replaced/removed block or "
            "table-row identities, expressed as old-to-new id/hash pairs. An "
            "empty list proves that this result changed no structure-map "
            "identity or content. Null is reserved for update_draft and legacy "
            "stored results."
        ),
    )
    # patch_draft dry_run returns a predicted structure map using the same
    # serialize_blocks() shape
    # persisted on ContextPageVersion.block_map
    # (kind/hash/lines/gap/sub/rows/id),
    # computed against the not-yet-persisted body a real write would produce.
    # Null on every non-dry_run result and on legacy replayed rows.
    predicted_block_map: list[dict] | None = None
    # patch_draft's push-outcome against its effective base: either the
    # caller-supplied base or the conversation read ledger's version. A
    # patch_draft call with neither fails with base_required instead of
    # applying directly at head. Null remains valid for update_draft and
    # legacy replay rows that predate push classification. "diverged" reuses
    # the alternative-draft branch mechanism; `alternative_draft_id` carries
    # that branch version id.
    push_outcome: Literal["committed", "rebased", "diverged", "replayed"] | None = (
        Field(
            default=None,
            description=(
                "patch_draft only: outcome against the explicit or read-ledger "
                "effective base. A patch without either base fails with "
                "base_required. Null for update_draft and legacy replay rows "
                "without push classification. 'replayed' means the durable "
                "idempotency ledger returned an earlier result without executing "
                "another write."
            ),
        )
    )
    # The ids (block or table row) whose content differs between `base` and
    # head at push time -- every changed id for "rebased" (informational:
    # "your base was behind by these clean changes"), and the ids that
    # actually conflicted with this request's own targets for "diverged".
    changed_since_base_ids: list[str] | None = None
    # design doc section 6.1's richer diverged payload -- set only when
    # push_outcome is "diverged". `conflicts` mirrors changed_since_base_ids
    # but with text/actor detail per id (base/head/yours + a git-style
    # marker block for content-bearing ops); `clean_changes` covers every OTHER
    # id changed_since_base_ids also names, informational only (already part
    # of head, nothing to resolve). Reconciliation uses the common
    # conflicts[*].head.version_id as base and `alternative_draft_id` in
    # reconciles; the top-level result version id is the branch and is not a
    # valid reconciliation base. `resolution_instruction` is fixed guidance
    # text, not computed per-request.
    conflicts: list[dict] | None = None
    clean_changes: list[dict] | None = None
    resolution_instruction: str | None = None

    model_config = ConfigDict(extra="forbid")


class PublishContextPageRequest(InputRequest):
    # Deliberately carries no floor override. This model is bound by the
    # workspace publish route, which any `draft-write` bearer token reaches, so
    # a field here is a field every non-interactive caller can send. Overriding
    # the objective floor is a decision only an author looking at the warning in
    # the editor may take, so it travels as a keyword argument to
    # ContextWriteService.publish_draft instead -- see PageService.publish_draft.
    version_id: UUID
    reason: str = Field(min_length=1)
    # The content the caller staged and means to publish. A draft row is
    # mutable, so without this a concurrent session of the same author can
    # change the body between staging and publishing. In-process callers
    # (review approval, PageService) publish a version they already hold
    # under the write lock and pass None; see PublishContextPageRouteRequest
    # for the externally bound body, where this becomes mandatory.
    expected_content_hash: str | None = Field(
        default=None, pattern=CONTENT_HASH_PATTERN
    )

    model_config = ConfigDict(extra="forbid")


class PublishContextPageResult(BaseModel):
    page_id: UUID
    published_version_id: UUID
    previous_published_version_id: UUID | None = None
    change_set_id: UUID
    audit_event_id: UUID
    findings: list[DraftReviewReason] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class CiteRequest(BaseModel):
    claim_text: str = Field(min_length=1)
    target_page_id: UUID | None = None
    source_scope: list[UUID] | None = None
    desired_citation_type: Literal["raw_source", "context_page", "both"] = "both"
    limit: int = Field(default=10, ge=1, le=50)

    model_config = ConfigDict(extra="forbid")


class CitationCandidate(BaseModel):
    citation_type: Literal["raw_source", "context_page", "derived_artifact"]
    library_item_version_id: UUID | None = None
    library_artifact_id: UUID | None = None
    cited_context_page_version_id: UUID | None = None
    location: dict[str, Any] = Field(default_factory=dict)
    snippet: str
    support_level: Literal["strong", "partial", "weak", "contradicts", "none"]
    freshness: Literal["current", "stale", "archived"]
    recommendation: Literal["use", "do_not_use", "needs_review"]
    write_citation: CitationInput | None = Field(
        default=None,
        description=("Write-ready CitationInput."),
    )

    model_config = ConfigDict(extra="forbid")


class SuggestedCitationVerification(BaseModel):
    candidate_id: UUID
    supported: Literal["yes", "no", "partial"]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=1000)

    model_config = ConfigDict(extra="forbid")


class CiteResult(BaseModel):
    citation_candidates: list[CitationCandidate] = Field(default_factory=list)
    suggested_verification: SuggestedCitationVerification | None = Field(
        default=None,
        description=(
            "Client advisory only; it never mutates persisted citation "
            "verification_status without a later reviewed write."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class CiteResponse(RESTResponseBase):
    data: CiteResult

    model_config = ConfigDict(extra="forbid")


class SubmitFeedbackRequest(InputRequest):
    target_type: Literal[
        "agent_page",
        "context_page",
        "section",
        "context_claim",
        "context_citation",
        "answer",
        "source",
        "library_artifact",
    ]
    target_id: UUID
    body: str = Field(min_length=1)
    severity: Literal["info", "minor", "major", "critical"] = "minor"
    suggested_action: str | None = None

    model_config = ConfigDict(extra="forbid")


class SubmitFeedbackResult(BaseModel):
    feedback_id: UUID
    status: Literal["open", "triaged"]
    next_action: Literal[
        "agent_resolution_queued",
        "needs_user_review",
        "none",
    ]

    model_config = ConfigDict(extra="forbid")


class SubmitFeedbackResponse(RESTResponseBase):
    data: SubmitFeedbackResult

    model_config = ConfigDict(extra="forbid")


class FeedbackEntry(BaseModel):
    id: UUID
    target_type: str
    target_id: UUID
    body: str
    severity: str
    status: str
    created_at: datetime

    model_config = ConfigDict(extra="forbid")


class FeedbackListResponse(RESTResponseBase):
    data: list[FeedbackEntry]

    model_config = ConfigDict(extra="forbid")


class ReviewDecisionRequest(InputRequest):
    # Reuse this ID when retrying an uncertain send. It becomes the durable
    # decision ledger ID, so a retry cannot add another request_changes round.
    decision_id: UUID | None = None
    decision_type: Literal["approve", "reject", "request_changes"]
    decision_reason: str | None = None
    # Optional routing target, only meaningful on request_changes (ⓐ/ⓒ).
    # chat_owner = the change's originator + their AI; requester = the actor
    # making the request (self/their AI); workspace_editors = broadcast to all
    # editors. Omitting it on request_changes defaults to the editor broadcast
    # (NULL audience, treated as kind-3 by the visibility filter — D2), so the
    # current single "Request changes" button keeps working until the routing
    # picker ships in the T15 frontend.
    audience_kind: Literal["chat_owner", "requester", "workspace_editors"] | None = None
    # Reject only: the impact_fingerprint of the previewed rejection. When the
    # impact changed before the decision, the server refuses with 409
    # review_decision_impact_changed instead of rejecting more than was shown.
    expected_impact_fingerprint: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def require_request_changes_reason(self) -> "ReviewDecisionRequest":
        reason = self.decision_reason.strip() if self.decision_reason else ""
        self.decision_reason = reason or None
        if self.decision_type == "request_changes" and self.decision_reason is None:
            raise ValueError(
                "decision_reason is required when requesting review changes"
            )
        # audience_kind is optional for request_changes (None -> broadcast) but
        # not allowed on approve/reject.
        if self.decision_type != "request_changes" and self.audience_kind is not None:
            raise ValueError("audience_kind is only allowed when requesting changes")
        if (
            self.decision_type != "reject"
            and self.expected_impact_fingerprint is not None
        ):
            raise ValueError("expected_impact_fingerprint is only allowed on reject")
        return self

    model_config = ConfigDict(extra="forbid")


class ReviewDecisionPayload(BaseModel):
    decision_id: UUID
    decision_type: str
    actor_type: str
    actor_id: UUID | None
    actor_display_name: str | None
    decision_reason: str | None
    audience_kind: Literal["chat_owner", "requester", "workspace_editors"] | None
    audience_user_id: UUID | None
    resulting_review_status: str
    resulting_change_set_status: str
    created_at: datetime

    model_config = ConfigDict(extra="forbid")


class ReviewDocumentTarget(BaseModel):
    """Actor-visible document identity, independent of an operation's raw ID."""

    document_kind: Literal["context", "agent", "library"]
    document_id: UUID | None = None
    title: str | None = None
    slug: str | None = None
    reader_href: str | None = None
    can_open: bool = False
    unavailable_reason: str | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewContentTarget(BaseModel):
    """The exact reviewed version and its independently authorized baseline."""

    mode: Literal["context_version", "metadata_only", "unsupported", "unavailable"]
    candidate_version_id: UUID | None = None
    candidate_version_no: int | None = None
    baseline_kind: Literal["published", "parent", "empty", "unavailable"] = (
        "unavailable"
    )
    baseline_version_id: UUID | None = None
    baseline_version_no: int | None = None
    can_read: bool = False
    can_diff: bool = False
    unavailable_reason: str | None = None
    is_current_candidate: bool | None = None
    # Set when the candidate was written on an earlier, still unpublished
    # draft: comparing against it shows this draft's own change, while the
    # baseline above shows what publishing it would change.
    draft_parent_version_id: UUID | None = None
    draft_parent_version_no: int | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewActionAvailability(BaseModel):
    allowed: bool
    reason_code: str | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewActionCapabilities(BaseModel):
    approve: ReviewActionAvailability
    reject: ReviewActionAvailability
    request_changes: ReviewActionAvailability
    edit_directly: ReviewActionAvailability

    model_config = ConfigDict(extra="forbid")


class ReviewVersionRef(BaseModel):
    version_id: UUID
    version_no: int

    model_config = ConfigDict(extra="forbid")


class ReviewRejectEffect(BaseModel):
    effect: Literal[
        "restore_published",
        "hide_unpublished",
        "no_head_change",
        "restore_previous_draft",
    ]
    document_id: UUID | None = None
    candidate_version_id: UUID | None = None
    candidate_version_no: int | None = None
    published_version_id: UUID | None = None
    published_version_no: int | None = None
    visible_sibling_review_ids: list[UUID] = Field(default_factory=list)
    visible_sibling_count: int = Field(default=0, ge=0)
    # Later drafts written on top of the candidate, oldest first. Rejecting the
    # candidate rejects them too; their open reviews are settled with it.
    cascaded_versions: list[ReviewVersionRef] = Field(default_factory=list)
    visible_cascaded_review_ids: list[UUID] = Field(default_factory=list)
    cascaded_review_count: int = Field(default=0, ge=0)
    # restore_previous_draft: the earlier draft that becomes the edit head.
    resulting_draft_version_id: UUID | None = None
    resulting_draft_version_no: int | None = None
    # That draft has no open review left, so rejecting opens a new one for it.
    restores_review: bool = False
    # Before a first publication, the earlier draft's page metadata could not
    # be restored (it predates per-draft metadata), so the current values stay.
    metadata_kept: bool = False
    # Set when the rejection cannot run, e.g. a private edit sits on top.
    blocked_reason: Literal["private_edit_in_progress"] | None = None
    # Binds a confirmation to exactly this impact (see ReviewDecisionRequest).
    impact_fingerprint: str | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewSettlement(BaseModel):
    """Why a superseded review was closed, and by which decided review."""

    reason_code: str
    by_review_item_id: UUID | None = None
    by_version_no: int | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewDecisionUndo(BaseModel):
    """Undo of the review's latest decision, for an actor who may undo it.

    The decider or an owner may undo a decision; any editor may undo an
    automatic approval. Absent for actors without that permission. Decisions
    without a recorded effect return available=False with not_recorded;
    recorded effects that cannot be reversed use unsupported_effect.
    """

    decision_id: UUID
    decision_type: str
    available: bool
    # later_changes: something changed after the decision, so undoing it would
    # discard that work. not_recorded and unsupported_effect explain why a
    # decision cannot be reversed even when no later change occurred.
    reason_code: Literal[
        "later_changes", "not_recorded", "unsupported_effect"
    ] | None = None
    # While unavailable: the version whose content a new draft can start from
    # instead (the version an approval replaced, or the rejected draft).
    restore_version_id: UUID | None = None
    restore_version_no: int | None = None
    page_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewEvidenceOperation(BaseModel):
    operation_id: UUID
    target_type: str
    target_id: UUID
    target_title: str | None = None
    target_slug: str | None = None
    target_version_no: int | None = None
    operation: str
    diff_summary: str | None = None
    diff_ref: str | None = None
    document: ReviewDocumentTarget | None = None
    content: ReviewContentTarget | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewEvidencePayload(BaseModel):
    change_set_status: str
    trigger_type: str
    reason: str | None
    cite_call_skipped: bool
    operations: list[ReviewEvidenceOperation]

    model_config = ConfigDict(extra="forbid")


class ReviewItemDetail(BaseModel):
    id: UUID
    workspace_id: UUID
    change_set_id: UUID
    review_type: str
    status: str
    risk_level: str
    risk_policy_version: str | None = None
    risk_resolution_summary: str | None = None
    risk_signals: dict[str, Any] | None = None
    risk_policy_snapshot: dict[str, Any] | None = None
    decision_summary: str | None
    decision_summary_code: str | None
    # Current routing audience = audience_kind of the latest request_changes
    # decision while status == 'changes_requested' (derived, not stored).
    current_audience_kind: (
        Literal["chat_owner", "requester", "workspace_editors", "workspace_owners"]
        | None
    )
    # True iff this change has a resolvable chat owner (origin chat session with
    # a known owner_user_id), so the routing dialog can enable "chat owner".
    # Optional in the contract (frontend treats undefined as enabled, T15).
    chat_owner_available: bool = False
    # Soft-claim (D1): who is currently working this item, and since when.
    claimed_by: UUID | None
    claimed_at: datetime | None
    can_decide: bool
    can_claim: bool
    actions: ReviewActionCapabilities | None = None
    reject_effect: ReviewRejectEffect | None = None
    created_at: datetime
    updated_at: datetime
    evidence: ReviewEvidencePayload | None
    decisions: list[ReviewDecisionPayload]
    published_version_id: UUID | None = None
    superseded_review_count: int = 0
    settlement: ReviewSettlement | None = None
    undo: ReviewDecisionUndo | None = None
    reconciliation_available: bool = False
    reconciliation_page_id: UUID | None = None
    reconciliation_version_id: UUID | None = None
    reconciliation_base_version_no: int | None = Field(default=None, ge=1)
    reconciliation_head_advance_count: int | None = Field(default=None, ge=0)
    reconciliation_conflict_count: int | None = Field(default=None, ge=0)
    reconciliation_branch_actor_kind: str | None = None
    reconciliation_branch_actor_display_name: str | None = None
    reconciliation_chat_session_id: UUID | None = None
    can_discard_reconciliation: bool = False

    model_config = ConfigDict(extra="forbid")


class ReviewItemListPayload(BaseModel):
    items: list[ReviewItemDetail]
    next_cursor: str | None
    deferred_reconciliation_count: int = Field(default=0, ge=0)
    # Only populated when the query is narrowed to one page. Unfiltered the
    # queue is unbounded and a COUNT on every infinite-scroll request would be
    # paid by everyone; narrowed, the caller genuinely needs to say "3 drafts"
    # rather than "3 shown", and the count is over a handful of rows.
    total: int | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewItemListResponse(RESTResponseBase):
    data: ReviewItemListPayload

    model_config = ConfigDict(extra="forbid")


class ReviewStatusRiskBreakdown(BaseModel):
    high: int = 0
    medium: int = 0
    low: int = 0

    model_config = ConfigDict(extra="forbid")


class ReviewStatusByRiskBreakdown(BaseModel):
    # The status x risk matrix has a fixed set of lifecycle buckets, so it is a
    # typed object (not a free dict) — keeps the contract precise enough for the
    # frontend type to be derived from the schema rather than hand-maintained.
    pending: ReviewStatusRiskBreakdown
    changes_requested: ReviewStatusRiskBreakdown
    resolved: ReviewStatusRiskBreakdown

    model_config = ConfigDict(extra="forbid")


class ReviewCountsPayload(BaseModel):
    pending: int = 0
    changes_requested: int = 0
    resolved: int = 0
    # Per-risk counts are always {high, medium, low}; type them as a fixed model
    # (required) instead of a free dict so the schema is accurate.
    risk_counts: ReviewStatusRiskBreakdown
    # RO-1: per-status risk breakdown so the queue cards can show High/Medium/Low
    # chips inside each lifecycle card. Additive — old clients ignore it.
    status_by_risk: ReviewStatusByRiskBreakdown
    to_me: int = 0
    from_me: int = 0
    auto_approve_candidate_count: int = 0
    auto_approve_candidate_counts_by_risk: ReviewStatusRiskBreakdown

    model_config = ConfigDict(extra="forbid")


class ReviewCountsResponse(RESTResponseBase):
    data: ReviewCountsPayload

    model_config = ConfigDict(extra="forbid")


class ReviewDecisionRecentItem(BaseModel):
    # RD-1: a real review decision row (actor + type + reason), as opposed to the
    # resolved review items the overview previously reused.
    decision_id: UUID
    review_item_id: UUID
    # Compatibility field retained for existing clients. It now has exactly one
    # meaning: the actor-visible review target title.
    review_target_title: str | None = None
    # C5: target identity is separate from the generic review outcome summary.
    # These names match ReviewEvidenceOperation/context-target metadata so
    # clients do not need another target vocabulary for recent decisions.
    target_title: str | None = None
    target_slug: str | None = None
    target_version_no: int | None = None
    decision_summary: str | None = None
    decision_summary_code: str | None = None
    decision_type: str
    actor_display_name: str | None = None
    decision_reason: str | None = None
    resulting_review_status: str | None = None
    risk_level: str | None = None
    created_at: datetime

    model_config = ConfigDict(extra="forbid")


class ReviewDecisionRecentListResponse(RESTResponseBase):
    data: list[ReviewDecisionRecentItem]

    model_config = ConfigDict(extra="forbid")


class ReviewBulkDecisionFilters(BaseModel):
    status: str | None = None
    risk_level: str | None = None
    scope: Literal["to_me", "from_me", "all"] = "all"
    target_page_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewBulkDecisionRequest(BaseModel):
    review_item_ids: list[UUID] | None = Field(default=None, max_length=500)
    filters: ReviewBulkDecisionFilters | None = None
    selection_token: str | None = Field(default=None, max_length=1_500_000)
    batch_limit: int = Field(default=500, ge=1, le=500)
    decision_type: Literal["approve"]
    dry_run: bool = False

    @model_validator(mode="after")
    def require_target(self) -> "ReviewBulkDecisionRequest":
        if self.selection_token is not None:
            if self.review_item_ids is not None or self.filters is not None:
                raise ValueError(
                    "selection_token cannot be combined with another target"
                )
        elif self.review_item_ids is None and self.filters is None:
            raise ValueError("review_item_ids, filters or selection_token is required")
        return self

    model_config = ConfigDict(extra="forbid")


class ReviewBulkSkippedItem(BaseModel):
    id: UUID
    reason: str
    reason_code: str | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewBulkItemOutcome(BaseModel):
    id: UUID
    outcome: Literal["approved", "auto_settled", "excluded", "failed"]
    reason_code: str | None = None
    reason: str | None = None
    target_title: str | None = None
    target_version_no: int | None = None
    # approved and undoable now: the decision a per-document undo names.
    decision_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewBulkDecisionPayload(BaseModel):
    approved: list[UUID]
    skipped: list[ReviewBulkSkippedItem]
    auto_settled_review_item_ids: list[UUID] = Field(default_factory=list)
    outcomes: list[ReviewBulkItemOutcome] = Field(default_factory=list)
    next_selection_token: str | None = None
    selection_complete: bool = True
    processed_count: int = 0
    remaining_count: int = 0
    total: int = 0
    requires_reconfirmation: bool = False

    model_config = ConfigDict(extra="forbid")


class ReviewBulkDecisionResponse(RESTResponseBase):
    data: ReviewBulkDecisionPayload

    model_config = ConfigDict(extra="forbid")


class ReviewSelectionRequest(BaseModel):
    # The service reports an operational-limit refusal with total + limit;
    # rejecting here would lose that actionable error body.
    review_item_ids: list[UUID] | None = Field(
        default=None, json_schema_extra={"maxItems": 5000}
    )
    filters: ReviewBulkDecisionFilters | None = None

    @model_validator(mode="after")
    def require_one_target(self) -> "ReviewSelectionRequest":
        if (self.review_item_ids is None) == (self.filters is None):
            raise ValueError("exactly one of review_item_ids or filters is required")
        return self

    model_config = ConfigDict(extra="forbid")


class ReviewSelectionItem(BaseModel):
    id: UUID
    eligible: bool
    will_auto_settle: bool = False
    # Only an eligible representative exposes its actor-visible same-candidate
    # effect. The global impact list is the union of these per-item subsets.
    auto_settled_review_item_ids: list[UUID] = Field(default_factory=list)
    reason_code: str | None = None
    reason: str | None = None
    target_title: str | None = None
    target_version_no: int | None = None

    model_config = ConfigDict(extra="forbid")


class ReviewSelectionPayload(BaseModel):
    selection_token: str
    expires_at: datetime
    total: int
    limit: int = 5000
    review_item_ids: list[UUID]
    eligible_count: int
    items: list[ReviewSelectionItem]
    auto_settled_review_item_ids: list[UUID] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ReviewSelectionResponse(RESTResponseBase):
    data: ReviewSelectionPayload

    model_config = ConfigDict(extra="forbid")


class ReviewSelectionLimitDetails(BaseModel):
    total: int
    limit: int

    model_config = ConfigDict(extra="forbid")


class ReviewSelectionLimitErrorBody(RESTErrorBody):
    details: ReviewSelectionLimitDetails


class ReviewAutoApprovalSweepPayload(BaseModel):
    approved: list[UUID] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ReviewAutoApprovalSweepResponse(RESTResponseBase):
    data: ReviewAutoApprovalSweepPayload

    model_config = ConfigDict(extra="forbid")


class ReviewItemResponse(RESTResponseBase):
    data: ReviewItemDetail

    model_config = ConfigDict(extra="forbid")
