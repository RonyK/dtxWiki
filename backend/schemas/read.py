from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.common import (
    ContextHistoryStatus,
    RESTResponseBase,
    RetrievalMode,
    SearchScope,
)
from backend.spi.types import (
    RankedResult as RankedResult,
)
from backend.spi.types import (
    SearchFilters as SearchFilters,
)
from backend.spi.types import (
    SearchMatchReason as SearchMatchReason,
)
from backend.spi.types import (
    SearchResultType as SearchResultType,
)

SEARCH_QUERY_MAX_LENGTH = 2048


class WorkspaceSummary(BaseModel):
    id: UUID
    slug: str
    title: str
    name: str
    goal: str
    default_language: str
    visibility: str
    join_policy: str = "invite_only"
    member_count: int = 0
    role: str | None = None
    # WS-09: every Personal workspace defaults to the same title
    # ("Personal Workspace"), and since 5130b614 a viewer can be a member of
    # someone else's shared Personal workspace, not only their own. Without
    # an owner name, two such rows in the switcher are pixel-identical.
    owner_display_name: str | None = None

    model_config = ConfigDict(extra="forbid")


class WorkspaceDetail(WorkspaceSummary):
    description: str | None = None
    library_policy: dict[str, Any]
    write_policy: dict[str, Any]
    citation_policy: dict[str, Any]
    agent_injection_policy: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class WorkspaceListData(BaseModel):
    workspaces: list[WorkspaceSummary]
    current_workspace_id: UUID | None = None
    default_workspace_id: UUID | None = None
    personal_workspace_id: UUID | None = None
    # D-01v2 (P10.3 T2, section 2.2): count of the actor's live
    # WorkspaceMembership rows that fall OUTSIDE a pat/oauth_jwt credential's
    # allowed set (no id/title -- the credential is not allowed to know
    # which workspaces those are, only that expanding its allowed set via
    # MCP Access would reach more). Always 0 for a session actor, which sees
    # its full membership roster unfiltered.
    other_membership_count: int = 0

    model_config = ConfigDict(extra="forbid")


class WorkspaceCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    goal: str | None = Field(default=None, min_length=1, max_length=512)
    default_language: str | None = Field(default=None, min_length=2, max_length=16)
    visibility: str | None = Field(default=None, min_length=1, max_length=32)

    model_config = ConfigDict(extra="forbid")


class WorkspaceDeleteData(BaseModel):
    deleted_workspace_id: UUID

    model_config = ConfigDict(extra="forbid")


class TreePathEntry(BaseModel):
    id: UUID
    section: str
    title: str
    slug: str | None = None
    node_kind: str

    model_config = ConfigDict(extra="forbid")


class TreeNodePayload(TreePathEntry):
    parent_id: UUID | None = None
    sort_key: str
    nav_summary: str | None = None
    # Render-time heuristic (design 12.1): the same text get_index shows
    # agents when nav_summary is NULL. Derived, never stored — the C-6 card
    # renders it grey with an "auto" badge.
    nav_summary_auto: str | None = None
    target_kind: str | None = None
    target_id: UUID | None = None
    target_meta: dict[str, str] = Field(default_factory=dict)
    is_system_node: bool
    can_have_children: bool
    primary_placement: bool
    visibility_status: str
    children_count: int = 0
    has_children: bool = False
    breadcrumb: list[TreePathEntry] = Field(default_factory=list)


class TreeSectionPolicyPayload(BaseModel):
    allow_user_folder_create: bool
    allow_page_as_parent: bool

    model_config = ConfigDict(extra="forbid")


class TreeListData(BaseModel):
    nodes: list[TreeNodePayload]
    policies: dict[str, TreeSectionPolicyPayload] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class HotConceptEntry(BaseModel):
    kind: str
    id: UUID
    title: str
    path: str
    score: int

    model_config = ConfigDict(extra="forbid")


class LinkSyntaxGuidance(BaseModel):
    link_syntax: Literal["wikilink", "context_url"]
    template: str
    target_source: str

    model_config = ConfigDict(extra="forbid")


class LinkingGuidance(BaseModel):
    syntaxes: list[LinkSyntaxGuidance]
    graph_visibility: Literal["published_head_only"]
    note: str
    missing_links_warning: Literal["context_page_has_no_graph_visible_links"]

    model_config = ConfigDict(extra="forbid")


class GuidePayload(BaseModel):
    workspace_summary: dict[str, Any]
    actor_context: dict[str, Any]
    library_policy_summary: dict[str, Any]
    context_write_policy: dict[str, Any]
    agent_injection_policy: dict[str, Any]
    citation_policy: dict[str, Any]
    context_page_taxonomy: list[dict[str, str]]
    agent_page_taxonomy: list[dict[str, str]]
    available_tools: list[str]
    available_prompts: list[str] = Field(default_factory=list)
    available_resources: list[str] = Field(default_factory=list)
    active_agent_pages: list[dict[str, Any]] = Field(default_factory=list)
    hot_concepts: list[HotConceptEntry] = Field(default_factory=list)
    placement_parent_candidates: list[dict[str, Any]] = Field(default_factory=list)
    recommended_agent_guidance_flow: list[str] = Field(default_factory=list)
    required_workflow: list[str]
    linking: LinkingGuidance

    model_config = ConfigDict(extra="forbid")


class VersionInfo(BaseModel):
    version_id: UUID
    version_no: int
    version_status: str
    review_disposition: Literal["rejected", "pending", "approved", "none"] = "none"
    history_status: ContextHistoryStatus | None = None
    graph_status: str | None = None
    parent_version_id: UUID | None = None
    content_hash: str | None = None
    created_at: datetime | None = None
    is_visible_head: bool = False
    is_published_head: bool = False
    is_draft_head: bool = False
    is_alternative: bool = False
    is_discarded: bool = False

    model_config = ConfigDict(extra="forbid")


class PublishedVersionInfo(BaseModel):
    """Human-facing summary of the snapshot anonymous readers receive.

    The authenticated Context reader needs the stable version number without
    eagerly walking History cursors.  It deliberately does not change the
    anonymous public-page payload.
    """

    version_id: UUID
    version_no: int
    published_at: datetime

    model_config = ConfigDict(extra="forbid")


class LibraryConversionStatePayload(BaseModel):
    status: str
    reason: (
        Literal[
            "document_conversion_failed",
            "document_conversion_timed_out",
            "document_conversion_unavailable",
        ]
        | None
    ) = None
    retryable: bool = False

    model_config = ConfigDict(extra="forbid")


class LibraryItemVersionPayload(BaseModel):
    id: UUID
    library_item_id: UUID
    version_no: int
    content_hash: str
    processing_status: str
    conversion: LibraryConversionStatePayload
    original_filename: str | None = None
    declared_media_type: str | None = None
    detected_media_type: str | None = None
    format_id: str | None = None
    byte_size: int | None = None
    detection: dict[str, Any] | None = None
    selected_conversion_attempt_id: UUID | None = None
    selected_preview_attempt_id: UUID | None = None
    preview_status: str
    original_url: str | None = None
    # Doc §3-D: server-authoritative for History's "download this version"
    # action -- only True when the original blob was actually verified
    # retrievable (not merely that `source_blob_id` is set). False on the
    # current/ready-version payloads, which don't compute this check; the
    # frontend never consults it there.
    original_download_available: bool = False
    poll_url: str | None = None
    extraction_summary: str | None = None
    previous_version_id: UUID | None = None
    created_at: datetime
    fetched_at: datetime

    model_config = ConfigDict(extra="forbid")


class LibraryArtifactPayload(BaseModel):
    id: UUID
    library_item_version_id: UUID
    stage: str
    artifact_type: str
    role: str | None = None
    media_type: str | None = None
    byte_size: int | None = None
    ordinal: int | None = None
    is_complete: bool = True
    sanitizer_version: str | None = None
    extraction_method: str | None = None
    index_text: str | None = None
    location_model: dict[str, Any]
    quality_score: float | None = None
    content_url: str | None = None
    created_at: datetime

    model_config = ConfigDict(extra="forbid")


class LibraryItemSummary(BaseModel):
    id: UUID
    uri: str
    title: str | None = None
    connector_type: str
    format_hint: str
    trust_level: str
    lifecycle_status: str
    current_version_id: UUID | None = None
    current_ready_version_id: UUID | None = None
    current_preview_version_id: UUID | None = None
    processing_status: str | None = None
    updated_at: datetime

    model_config = ConfigDict(extra="forbid")


class LibraryItemDetail(LibraryItemSummary):
    metadata: dict[str, Any]
    current_version: LibraryItemVersionPayload | None = None
    current_ready_version: LibraryItemVersionPayload | None = None
    versions: list[LibraryItemVersionPayload] = Field(default_factory=list)
    artifacts: list[LibraryArtifactPayload] = Field(default_factory=list)


class AgentCategoryEntry(BaseModel):
    category: str
    description: str
    default_injection_policy: str

    model_config = ConfigDict(extra="forbid")


class AgentPageSummary(BaseModel):
    id: UUID
    category: str
    title: str
    slug: str
    lifecycle_status: str
    injection_policy: str
    priority: int
    sensitivity: str
    current_active_version_id: UUID | None = None
    latest_draft_version_id: UUID | None = None
    updated_at: datetime

    model_config = ConfigDict(extra="forbid")


class AgentPageDetail(AgentPageSummary):
    body_markdown: str | None = None
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    version_info: VersionInfo | None = None
    tree_path: list[TreePathEntry] = Field(default_factory=list)
    parent_node_id: UUID | None = None
    children_count: int = 0
    metadata_provenance: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)


class ContextPageSummary(BaseModel):
    id: UUID
    slug: str
    title: str
    page_type: str
    lifecycle_status: str
    aliases: list[str] = Field(default_factory=list)
    visible_version_id: UUID | None = None
    current_version_id: UUID | None = None
    current_published_version_id: UUID | None = None
    latest_draft_version_id: UUID | None = None
    is_public: bool = False
    updated_at: datetime

    model_config = ConfigDict(extra="forbid")


class PendingReviewInfo(BaseModel):
    """The open review awaiting a decision for a page's pending draft (F1).

    Surfaced on the Context page so a reviewer can act inline, without first
    finding the item in the Review rail.
    """

    review_item_id: UUID
    status: str  # "pending" | "changes_requested"
    risk_level: str

    model_config = ConfigDict(extra="forbid")


class WikilinkTarget(BaseModel):
    target_slug: str
    target_page_id: UUID | None = None
    current_slug: str | None = None

    model_config = ConfigDict(extra="forbid")


class ContextPageDetail(ContextPageSummary):
    body_markdown: str | None = None
    version_info: VersionInfo | None = None
    published_version_info: PublishedVersionInfo | None = None
    tree_path: list[TreePathEntry] = Field(default_factory=list)
    parent_node_id: UUID | None = None
    children_count: int = 0
    backlink_count: int = 0
    wikilink_targets: list[WikilinkTarget] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # F1: a draft awaiting review + the open review item, so the Context surface
    # can show "pending draft" + an inline approve/request-changes affordance.
    # pending_draft_warnings lints the DRAFT body (not the displayed/published
    # one), so a reviewer sees the draft's format issues before approving even
    # when the page's published head is clean (Sweep-2).
    pending_draft_version_id: UUID | None = None
    pending_draft_warnings: list[str] = Field(default_factory=list)
    pending_review: PendingReviewInfo | None = None
    reconciliation_pending_count: int = Field(default=0, ge=0)
    reconciliation_pending_count_truncated: bool = False
    next_reconciliation_version_id: UUID | None = None
    reconciliation_available: bool = False


class LibraryItemListPage(BaseModel):
    items: list[LibraryItemSummary] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int = 0
    has_more: bool = False

    model_config = ConfigDict(extra="forbid")


class ContextPageListPage(BaseModel):
    items: list[ContextPageSummary] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int = 0
    has_more: bool = False

    model_config = ConfigDict(extra="forbid")


class AgentPageListPage(BaseModel):
    items: list[AgentPageSummary] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int = 0
    has_more: bool = False

    model_config = ConfigDict(extra="forbid")


class VersionEntry(BaseModel):
    version_id: UUID
    version_no: int
    version_status: str
    review_disposition: Literal["rejected", "pending", "approved", "none"] = "none"
    history_status: ContextHistoryStatus | None = None
    graph_status: str | None = None
    actor_type: str
    actor_display_name: str | None = None
    created_at: datetime | None = None
    parent_version_id: UUID | None = None
    prior_version_id: UUID | None = None
    prior_relation: Literal["root", "visible", "hidden", "missing"] = "root"
    # The reconcile merge edge (design doc section 7.1): set only on a
    # diverged branch that landed cleanly, naming the linear version its
    # conflicts were folded into. A history graph draws this as a SECOND
    # incoming edge into that version, alongside its own parent_version_id --
    # the git-merge shape, not a lone dangling branch line.
    merged_into_version_id: UUID | None = None
    actor_id: UUID | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    is_current: bool = False
    is_current_published: bool = False
    is_latest_draft: bool = False
    is_visible_head: bool = False
    review_status: str | None = None
    review_item_id: UUID | None = None
    is_published_head: bool = False
    is_draft_head: bool = False
    is_alternative: bool = False
    is_discarded: bool = False
    reconciliation_available: bool = False

    model_config = ConfigDict(extra="forbid")


class ChangeSetEntry(BaseModel):
    change_set_id: UUID
    actor_type: str
    actor_id: UUID | None = None
    reason: str | None = None
    status: str
    risk_level: str
    created_at: datetime

    model_config = ConfigDict(extra="forbid")


class ReviewDecisionEntry(BaseModel):
    decision_id: UUID
    decision_type: str
    actor_type: str
    decision_reason: str | None = None
    created_at: datetime

    model_config = ConfigDict(extra="forbid")


class ListChangesPayload(BaseModel):
    versions: list[VersionEntry] = Field(default_factory=list)
    changesets: list[ChangeSetEntry] = Field(default_factory=list)
    review_decisions: list[ReviewDecisionEntry] = Field(default_factory=list)
    diffs: list[dict[str, Any]] | None = None
    next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class DisambiguationCandidate(BaseModel):
    id: UUID
    title: str
    concept_path: str | None = None
    match_reason: str

    model_config = ConfigDict(extra="forbid")


class DisambiguationPayload(BaseModel):
    status: Literal["ambiguous"] = "ambiguous"
    candidates: list[DisambiguationCandidate]

    model_config = ConfigDict(extra="forbid")


class SearchPayload(BaseModel):
    # Every field is always present in a search response, so the REST/MCP
    # contract declares them required (the frontend `operations.ts` SearchPayload
    # is derived from the generated schema, and the in-memory defaults below were
    # leaking out as `optional` on the wire). total_count/has_more keep a default
    # only for construction ergonomics; they still serialize on every response.
    ranked_results: list[RankedResult]
    supported_retrieval_modes: list[str]
    permission_note: str | None
    total_count: int = 0
    has_more: bool = False
    # Opaque offset cursor for the next page of ranked results; null when the
    # ranked set is exhausted. Echo it back as SearchRequest.cursor to page on.
    next_cursor: str | None
    warnings: list[str] = Field(default_factory=list)
    tried_lanes: list[str] = Field(default_factory=list)
    raw_query: str = ""
    normalized_query: str = ""
    disambiguation: DisambiguationPayload | None = None
    # Actor-visible, low-confidence entity hints retained for a true miss.
    # These are advisory inputs for coverage-gap/Q-4 review, not search hits.
    nearest_candidates: list[dict[str, str]] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class WorkspaceListResponse(RESTResponseBase):
    data: WorkspaceListData

    model_config = ConfigDict(extra="forbid")


class WorkspaceDetailResponse(RESTResponseBase):
    data: WorkspaceDetail

    model_config = ConfigDict(extra="forbid")


class WorkspaceCreateResponse(RESTResponseBase):
    data: WorkspaceSummary

    model_config = ConfigDict(extra="forbid")


class WorkspaceDeleteResponse(RESTResponseBase):
    data: WorkspaceDeleteData

    model_config = ConfigDict(extra="forbid")


class TreeListResponse(RESTResponseBase):
    data: TreeListData

    model_config = ConfigDict(extra="forbid")


class TreeNodeResponse(RESTResponseBase):
    data: TreeNodePayload

    model_config = ConfigDict(extra="forbid")


class TreeNodeIndexPreviewPayload(BaseModel):
    markdown: str

    model_config = ConfigDict(extra="forbid")


class TreeNodeIndexPreviewResponse(RESTResponseBase):
    data: TreeNodeIndexPreviewPayload

    model_config = ConfigDict(extra="forbid")


class LibraryItemListResponse(RESTResponseBase):
    data: list[LibraryItemSummary]
    next_cursor: str | None = None
    total: int = 0
    has_more: bool = False

    model_config = ConfigDict(extra="forbid")


class LibraryItemDetailResponse(RESTResponseBase):
    data: LibraryItemDetail

    model_config = ConfigDict(extra="forbid")


class LibraryFormatCapabilityPayload(BaseModel):
    format_id: str
    display_name: str
    category: Literal["text", "document", "image", "audio", "video", "data"]
    extensions: list[str]
    default_extensions: list[str]
    extensions_overridden: bool
    media_types: list[str]
    search_aliases: list[str]
    provider_ids: list[str]
    upload: bool
    policy_enabled: bool
    deployment_available: bool
    converter_available: bool
    admission_state: Literal["ready", "validator_unavailable", "denied"]
    storage_state: Literal["available", "available_after_validation", "denied"]
    conversion_state: Literal["available", "stored_unconverted", "unavailable"]
    preview_state: Literal["ready", "degraded", "unavailable"]
    render_state: Literal["available", "unsupported"]
    reason: str | None = None
    conversion: Literal["available", "stored_unconverted", "denied"]
    preview_kind: Literal[
        "markdown", "pdf", "image", "audio", "video", "table", "metadata"
    ]
    renderer_contract: str
    max_bytes: int
    max_upload_bytes: int
    max_convertible_bytes: int | None = None
    upload_limit_sources: dict[
        Literal["workspace", "family", "format", "instance"], int
    ]
    upload_limiting_source: Literal[
        "workspace", "family", "format", "instance"
    ]

    model_config = ConfigDict(extra="forbid")


class LibraryFormatFamilyPresetBaselinePayload(BaseModel):
    preset_id: str
    preset_revision: int = Field(ge=1)
    format_ids: list[str]
    format_extensions: dict[str, list[str]]

    model_config = ConfigDict(extra="forbid")


class LibraryFormatFamilyCapabilityPayload(BaseModel):
    family_id: str
    name: str
    origin: Literal["preset", "custom"]
    format_ids: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    preset_id: str | None = None
    preset_revision: int | None = Field(default=None, ge=1)
    preset_baseline: LibraryFormatFamilyPresetBaselinePayload | None = None
    uploadable_count: int = Field(ge=0)
    preview_ready_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    max_upload_bytes: int | None = Field(default=None, gt=0)
    effective_max_upload_bytes: int = Field(gt=0)
    upload_limiting_source: Literal[
        "workspace", "family", "instance"
    ]
    blockers: list[str]

    model_config = ConfigDict(extra="forbid")


class LibraryFormatCapabilitySummaryPayload(BaseModel):
    total: int = Field(ge=0)
    uploadable: int = Field(ge=0)
    preview_ready: int = Field(ge=0)
    download_only: int = Field(ge=0)
    unavailable: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class LibraryFormatFamilyPresetPayload(BaseModel):
    preset_id: str
    revision: int = Field(ge=1)
    name: str
    description: str
    format_ids: list[str]
    extensions: list[str]
    format_extensions: dict[str, list[str]]

    model_config = ConfigDict(extra="forbid")


class LibraryCapabilitiesPayload(BaseModel):
    catalog_revision: str
    policy_revision: str
    capability_revision: str
    policy_schema_version: Literal[5]
    max_files_per_batch: int
    family_presets: list[LibraryFormatFamilyPresetPayload]
    families: list[LibraryFormatFamilyCapabilityPayload]
    summary: LibraryFormatCapabilitySummaryPayload
    formats: list[LibraryFormatCapabilityPayload]

    model_config = ConfigDict(extra="forbid")


class LibraryCapabilitiesResponse(RESTResponseBase):
    data: LibraryCapabilitiesPayload

    model_config = ConfigDict(extra="forbid")


class LibraryItemVersionResponse(RESTResponseBase):
    data: LibraryItemVersionPayload

    model_config = ConfigDict(extra="forbid")


class LibraryItemVersionListResponse(RESTResponseBase):
    data: list[LibraryItemVersionPayload]

    model_config = ConfigDict(extra="forbid")


class LibraryArtifactResponse(RESTResponseBase):
    data: LibraryArtifactPayload

    model_config = ConfigDict(extra="forbid")


class LibraryItemReferencePayload(BaseModel):
    id: UUID
    title: str
    slug: str

    model_config = ConfigDict(extra="forbid")


class LibraryItemReferenceListResponse(RESTResponseBase):
    data: list[LibraryItemReferencePayload]

    model_config = ConfigDict(extra="forbid")


class LibraryItemTombstonePayload(BaseModel):
    library_item_id: UUID
    uri: str
    title: str | None = None
    last_version_no: int | None = None
    deleted_at: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class LibraryItemTombstoneListResponse(RESTResponseBase):
    data: list[LibraryItemTombstonePayload]

    model_config = ConfigDict(extra="forbid")


class AgentCategoryListResponse(RESTResponseBase):
    data: list[AgentCategoryEntry]

    model_config = ConfigDict(extra="forbid")


class AgentPageListResponse(RESTResponseBase):
    data: list[AgentPageSummary]
    next_cursor: str | None = None
    total: int = 0
    has_more: bool = False

    model_config = ConfigDict(extra="forbid")


class AgentPageDetailResponse(RESTResponseBase):
    data: AgentPageDetail

    model_config = ConfigDict(extra="forbid")


class ContextPageListResponse(RESTResponseBase):
    data: list[ContextPageSummary]
    next_cursor: str | None = None
    total: int = 0
    has_more: bool = False

    model_config = ConfigDict(extra="forbid")


class ContextPageDetailResponse(RESTResponseBase):
    data: ContextPageDetail

    model_config = ConfigDict(extra="forbid")


class GraphNeighborEntry(BaseModel):
    kind: str
    id: UUID
    title: str
    path: str | None = None
    edge_kind: str
    direction: Literal["in", "out"]

    model_config = ConfigDict(extra="forbid")


class GraphNeighborsPayload(BaseModel):
    backlinks: list[GraphNeighborEntry] = Field(default_factory=list)
    outbound: list[GraphNeighborEntry] = Field(default_factory=list)
    citations: list[GraphNeighborEntry] = Field(default_factory=list)
    co_access: list[GraphNeighborEntry] = Field(default_factory=list)
    mermaid: str | None = None
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class GraphNeighborsResponse(RESTResponseBase):
    data: GraphNeighborsPayload

    model_config = ConfigDict(extra="forbid")


class ListChangesResponse(RESTResponseBase):
    data: ListChangesPayload

    model_config = ConfigDict(extra="forbid")


class SearchRequest(BaseModel):
    query: str = Field(default="", max_length=SEARCH_QUERY_MAX_LENGTH)
    scope: SearchScope = "all"
    filters: SearchFilters = Field(default_factory=SearchFilters)
    retrieval_mode: RetrievalMode = "keyword"
    limit: int = Field(default=20, ge=1, le=100)
    # Opaque cursor from a prior SearchPayload.next_cursor; omitted for page 1.
    cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class SearchResponse(RESTResponseBase):
    data: SearchPayload

    model_config = ConfigDict(extra="forbid")


class SearchCapabilities(BaseModel):
    supported_retrieval_modes: list[RetrievalMode]

    model_config = ConfigDict(extra="forbid")


class SearchCapabilitiesResponse(RESTResponseBase):
    data: SearchCapabilities

    model_config = ConfigDict(extra="forbid")
