from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from backend.schemas.common import (
    MCPConversationInput,
    MCPResultEnvelope,
    RetrievalMode,
    SearchScope,
)
from backend.schemas.context_edit import SectionAddress
from backend.schemas.read import (
    SEARCH_QUERY_MAX_LENGTH,
    AgentCategoryEntry,
    AgentPageDetail,
    DisambiguationCandidate,
    GuidePayload,
    ListChangesPayload,
    SearchFilters,
    SearchPayload,
    WorkspaceListData,
)


class ListMyWorkspacesInput(BaseModel):
    # Intentionally conversation-less: list_my_workspaces is the workspace-less
    # discovery call. The runner strips any echoed conversation_token before
    # validation (it is still used for chat grouping), so echoing a token here
    # no longer raises validation_failed (C1) while keeping this schema clean.
    model_config = ConfigDict(extra="forbid")


class ListMyWorkspacesOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: WorkspaceListData | None = None

    model_config = ConfigDict(extra="forbid")


class GuideInput(MCPConversationInput):
    workspace_id: UUID
    detail_level: Literal["brief", "normal", "detailed", "full"] = Field(
        default="brief",
        description=(
            "'brief' (default): what reading, editing and exploring need. "
            "'normal' adds taxonomies, prompt/resource entry points, AgentPage "
            "policy, hot concepts and your full tool list. 'detailed' adds the "
            "full workspace summary and field explanations. 'full' is the old "
            "name of 'detailed'."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class GuideOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: GuidePayload | None = None

    model_config = ConfigDict(extra="forbid")


class SearchInput(MCPConversationInput):
    workspace_id: UUID
    query: str = Field(max_length=SEARCH_QUERY_MAX_LENGTH)
    scope: SearchScope = "all"
    filters: SearchFilters = Field(default_factory=SearchFilters)
    retrieval_mode: RetrievalMode = "keyword"
    limit: int = Field(default=20, ge=1, le=100)
    # Echo a prior SearchOutput.data.next_cursor here to page on (A5); omit for
    # page 1. Without this the output exposes next_cursor an agent cannot use.
    cursor: str | None = None
    wire: Literal["stable", "mrtr_preview"] = "stable"
    resume_token: str | None = Field(default=None, max_length=4096)
    input_responses: dict[str, dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _validate_resume_pair(self):
        if (self.resume_token is None) != (self.input_responses is None):
            raise ValueError(
                "resume_token and input_responses must be provided together"
            )
        return self

    model_config = ConfigDict(extra="forbid")


class SearchOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: SearchPayload | None = None
    input_required: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


FetchResultType = Literal[
    "context_page",
    "context_claim",
    "library_item",
    "library_artifact",
    "agent_page",
    "feedback",
    "change_set",
]


class FetchInput(MCPConversationInput):
    workspace_id: UUID
    id: UUID
    type: FetchResultType

    model_config = ConfigDict(extra="forbid")


class FetchOutput(BaseModel):
    envelope: MCPResultEnvelope
    id: UUID | None = None
    title: str | None = None
    text: str | None = None
    url: str | None = None
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class ReadInput(MCPConversationInput):
    workspace_id: UUID
    target_type: Literal[
        "context_page",
        "context_page_version",
        "library_item_version",
        "library_artifact",
        "context_claim",
        "context_citation",
        "feedback",
        "change_set",
    ]
    target_id: UUID
    # D-2: the former ``range``/``include`` fields were accepted but silently
    # ignored — removed rather than deprecated (extra="forbid" now rejects
    # them loudly). Versions/diff -> list_changes; backlinks/citations ->
    # read_concept neighbors; metadata is always in the payload.

    model_config = ConfigDict(extra="forbid")


class ReadOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class GetIndexInput(MCPConversationInput):
    # OKF navigational catalog (Tier-1 retrieval): returns an index.md the agent
    # surveys before READing concepts. Per-actor filtered (restricted/drafts).
    # Small workspaces render one flat catalog; larger ones render routing
    # nodes — pass ``node`` (a "node:" link target from a prior response:
    # slash path like "/context/payments", or a node UUID) to descend.
    workspace_id: UUID
    sections: list[Literal["context", "library"]] = Field(
        default_factory=lambda: ["context", "library"]
    )
    node: str | None = Field(default=None, max_length=512)
    format: Literal["markdown", "nodes"] = Field(
        default="markdown",
        description=(
            "'nodes' returns one tree node and its visible children with "
            "node_id, so you can walk from /context to a placement_parent_node_id."
        ),
    )
    limit: int = Field(default=20, ge=1, le=200, description="format='nodes' only.")
    cursor: str | None = Field(
        default=None,
        max_length=512,
        description="format='nodes' only: next_cursor from the previous page.",
    )

    model_config = ConfigDict(extra="forbid")


class IndexNodeRef(BaseModel):
    node_id: UUID
    title: str
    path: str
    can_have_children: bool

    model_config = ConfigDict(extra="forbid")


class IndexNodeChild(IndexNodeRef):
    node_kind: str
    has_children: bool

    model_config = ConfigDict(extra="forbid")


class IndexNodesPayload(BaseModel):
    node: IndexNodeRef
    children: list[IndexNodeChild] = Field(default_factory=list)
    children_total: int = 0
    next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class GetIndexOutput(BaseModel):
    envelope: MCPResultEnvelope
    index: str | None = None
    nodes: IndexNodesPayload | None = None

    model_config = ConfigDict(extra="forbid")


class ReadConceptInput(MCPConversationInput):
    # Read a single OKF concept by its path (e.g. "/context/my-page.md"),
    # returning frontmatter + Markdown body of the actor-visible version.
    # include_neighbors adds the 1-hop block (design 5.5); set false to save
    # tokens on pure content reads.
    workspace_id: UUID
    path: str = Field(min_length=1, max_length=512)
    include_neighbors: bool = True
    # Point-in-time read (design 17.2-B): resolve the version PUBLISHED at
    # this instant (superseded snapshots included). Names resolve at present;
    # neighbors are omitted (a historical graph would be dishonest).
    # CONTEXT concepts only — /library/<id>.md reads are always current and
    # echo as_of=null so a point-in-time caller can detect the downgrade.
    as_of: datetime | None = None
    format: Literal["structured", "mermaid"] = "structured"

    model_config = ConfigDict(extra="forbid")


class NeighborEntry(BaseModel):
    kind: str
    id: UUID
    title: str
    path: str | None = None
    edge_kind: str
    direction: Literal["in", "out"]

    model_config = ConfigDict(extra="forbid")


class ConceptNeighbors(BaseModel):
    # 1-hop graph expansion (design 5.5): title+path one-liners only, hard
    # caps per class, all entries actor-visible.
    outbound: list[NeighborEntry] = Field(default_factory=list, max_length=10)
    backlinks: list[NeighborEntry] = Field(default_factory=list, max_length=10)
    siblings: list[NeighborEntry] = Field(default_factory=list, max_length=10)
    citations: list[NeighborEntry] = Field(default_factory=list, max_length=5)
    co_access: list[NeighborEntry] = Field(default_factory=list, max_length=10)
    parent_folder: NeighborEntry | None = None

    model_config = ConfigDict(extra="forbid")


class ReadConceptOutput(BaseModel):
    envelope: MCPResultEnvelope
    status: Literal["found", "redirected", "ambiguous", "not_found"] | None = None
    path: str | None = None
    canonical_path: str | None = None
    redirect_target_path: str | None = None
    # Concept identity across sections (A-8): context pages fill
    # target_kind="context_page" (target_id == page_id); library sources
    # fill target_kind="library_item".
    target_kind: Literal["context_page", "library_item"] | None = None
    target_id: UUID | None = None
    page_id: UUID | None = None
    version_id: UUID | None = None
    version_status: str | None = None
    content: str | None = None
    frontmatter: dict[str, Any] | None = None
    candidates: list[DisambiguationCandidate] = Field(default_factory=list)
    neighbors: ConceptNeighbors | None = None
    as_of: datetime | None = None
    mermaid: str | None = None
    alternative_drafts: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Active alternatives ordered by version_no descending (newest first)."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class ContextOutlineTarget(BaseModel):
    path: str | None = Field(default=None, min_length=1, max_length=512)
    page_id: UUID | None = None
    version_id: UUID | None = None

    @model_validator(mode="after")
    def _require_one_target(self):
        locators = (self.path, self.page_id, self.version_id)
        if sum(value is not None for value in locators) != 1:
            raise ValueError("target must contain exactly one locator")
        return self

    model_config = ConfigDict(extra="forbid")


# Which stored version a context read resolves to. "my_edit" is the caller's
# own open editing row -- the body patch_context_edit applies to -- read with no
# side effects (no row is opened when there is none).
ContextReadVersion = Literal["visible", "draft", "published", "my_edit"]
_CONTEXT_READ_VERSION_DESCRIPTION = (
    "Which version to read: 'visible' (default; what this caller sees), "
    "'draft', 'published', or 'my_edit' -- your own open editing row, the "
    "exact body patch_context_edit edits (not_found when you have none; it "
    "never opens one). An explicit target.version_id overrides this."
)


class ReadContextOutlineInput(MCPConversationInput):
    workspace_id: UUID
    target: ContextOutlineTarget
    version: ContextReadVersion = Field(
        default="visible", description=_CONTEXT_READ_VERSION_DESCRIPTION
    )
    select: SectionAddress | None = None
    max_chars: int = Field(default=8000, ge=1, le=40000)
    cursor: str | None = None
    max_depth: int = Field(default=6, ge=1, le=6)
    max_sections: int = Field(
        default=200,
        ge=1,
        le=2000,
        description=(
            "Maximum outline entries per page. The heading list is bounded by "
            "this and by a wire-byte budget, whichever binds first."
        ),
    )
    outline_cursor: str | None = Field(
        default=None,
        description=(
            "Resume token for the heading list. Independent of `cursor`, which "
            "pages the selected section's body and requires `select`."
        ),
    )

    @model_validator(mode="after")
    def _cursor_requires_selection(self):
        # Deliberately constrains `cursor` only: outline_cursor pages the
        # heading list, which is returned with or without a selection.
        if self.cursor is not None and self.select is None:
            raise ValueError("cursor requires select")
        return self

    model_config = ConfigDict(extra="forbid")


class ContextOutlineDocument(BaseModel):
    page_id: UUID
    version_id: UUID
    version_status: str
    content_hash: str = Field(
        description=(
            "Hash of canonicalized Markdown. This normalized hash is not an "
            "offset-cache key; use body_raw_sha256 for raw outline offsets."
        )
    )
    body_raw_sha256: str = Field(
        description="SHA-256 of the exact raw Markdown indexed by outline offsets."
    )
    char_count: int
    section_count: int

    model_config = ConfigDict(extra="forbid")


class ContextOutlineEntry(BaseModel):
    section_id: str
    level: int
    title: str
    heading_path: list[str]
    occurrence: int
    line_start: int
    line_end: int
    char_count: int
    section_hash: str = Field(
        description=(
            "Normalized content_hash of the exact raw Markdown slice for this "
            "section subtree."
        )
    )

    model_config = ConfigDict(extra="forbid")


class ContextOutlineSection(BaseModel):
    content: str
    section_hash: str = Field(
        description=(
            "Normalized content_hash of the complete raw selected-section slice, "
            "including content beyond this page when truncated."
        )
    )
    anchor_status: Literal["exact", "fuzzy", "fuzzy_distant"]
    truncated: bool
    next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class ReadContextOutlineOutput(BaseModel):
    envelope: MCPResultEnvelope
    document: ContextOutlineDocument | None = None
    outline: list[ContextOutlineEntry] = Field(
        default_factory=list,
        max_length=2000,
        description=(
            "One page of the heading list, in document order. Never the whole "
            "document unless outline_next_cursor is null."
        ),
    )
    outline_total: int = Field(
        default=0,
        description=(
            "Headings passing max_depth across the WHOLE document -- the full "
            "population of this projection. document.section_count is a "
            "different number: every heading at every level."
        ),
    )
    outline_omitted_count: int = Field(
        default=0, description="outline_total minus the entries on this page."
    )
    outline_next_cursor: str | None = None
    section: ContextOutlineSection | None = None
    content_trust: Literal["untrusted_user_content"] = "untrusted_user_content"

    @model_serializer(mode="wrap")
    def _omit_absent_section(self, handler):
        payload = handler(self)
        if self.section is None:
            payload.pop("section", None)
        return payload

    model_config = ConfigDict(extra="forbid")


class ReadStructureMapInput(MCPConversationInput):
    workspace_id: UUID
    target: ContextOutlineTarget
    version: ContextReadVersion = Field(
        default="visible", description=_CONTEXT_READ_VERSION_DESCRIPTION
    )
    max_blocks: int = Field(default=200, ge=1, le=2000)
    max_rows_per_block: int = Field(
        default=200,
        ge=1,
        le=2000,
        description=(
            "Independent of max_blocks on purpose: a one-megabyte table is a "
            "SINGLE block, so a block cap alone bounds nothing for it."
        ),
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Resume token. Resumes at a block boundary, or mid-table at a row."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class StructureMapRow(BaseModel):
    id: str
    hash: str

    model_config = ConfigDict(extra="forbid")


class StructureMapBlock(BaseModel):
    id: str
    kind: str
    hash: str
    line_span: tuple[int, int] = Field(
        description=(
            "[start, end) 1-indexed line range, computed fresh at serve time -- "
            "not a persisted field (design doc section 2.1)."
        )
    )
    preview: str = Field(
        description="The block's first raw line, truncated -- untrusted content."
    )
    rows: list[StructureMapRow] = Field(default_factory=list, max_length=2000)
    row_total: int = Field(
        default=0, description="This block's true row count, not the page's."
    )
    rows_omitted_count: int = Field(
        default=0, description="Rows of this block not on this page."
    )
    row_offset: int = Field(
        default=0,
        description=(
            "Index of the first returned row within the block, so a "
            "row-continuation page is unambiguous."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class StructureMapDocument(BaseModel):
    page_id: UUID
    version_id: UUID
    version_status: str
    root_hash: str = Field(
        description=(
            "The version's content_hash. It identifies the body these block "
            "ids and hashes describe; patch_context_edit's concurrency check "
            "is editing_revision, not this hash."
        )
    )
    editing_revision: int | None = Field(
        default=None,
        description=(
            "For an 'editing' row: the revision to send as "
            "patch_context_edit.expected_editing_revision. null otherwise."
        ),
    )
    resolved_version_source: Literal[
        "visible", "draft", "published", "my_edit", "exact_version"
    ] = Field(
        default="visible",
        description=(
            "How version_id was chosen: the requested `version` mode, or "
            "exact_version when target.version_id was given."
        ),
    )
    map_repaired: bool = Field(
        default=False,
        description=(
            "True when the stored block map described an older body and was "
            "rebuilt for this read. The ids returned are the ones "
            "patch_context_edit accepts for this row."
        ),
    )
    block_count: int = Field(
        description=(
            "The document's TOTAL block count -- NOT the number of blocks "
            "returned. Use len(blocks) for that, and follow next_cursor for "
            "the rest."
        )
    )

    model_config = ConfigDict(extra="forbid")


class ReadStructureMapOutput(BaseModel):
    envelope: MCPResultEnvelope
    document: StructureMapDocument | None = None
    blocks: list[StructureMapBlock] = Field(default_factory=list, max_length=2000)
    blocks_omitted_count: int = Field(
        default=0,
        description="Blocks after this page's last one, within the document.",
    )
    next_cursor: str | None = None
    content_trust: Literal["untrusted_user_content"] = "untrusted_user_content"

    model_config = ConfigDict(extra="forbid")


class WhatChangedInput(MCPConversationInput):
    # Published-change delta since a timestamp (design 17.2-B): the killer
    # tool for re-visiting agents — read the delta instead of re-crawling.
    # Also the CAG bundle cache-invalidation signal. Point-in-time content
    # reads: read_concept with as_of. search has no as_of until N3 (live
    # -head indexes only).
    workspace_id: UUID
    since: datetime
    scope: Literal["context"] = "context"
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class WhatChangedEntryModel(BaseModel):
    # One move of a page's published version, like a ref update (before ->
    # after). Why it moved is the reason, never an extra change_kind.
    concept_path: str
    title: str
    change_kind: Literal["created", "updated", "archived", "unpublished"] = Field(
        description=(
            "created: the page entered the published set, including publishing "
            "again after it was unpublished or archived. updated: the published "
            "version changed. unpublished: an undone first publication left "
            "nothing published. archived: the page was archived."
        )
    )
    page_id: UUID
    version_id: UUID | None = Field(
        default=None,
        description=(
            "The version published from this entry on; null for unpublished. "
            "For archived, the page's head when it was archived."
        ),
    )
    previous_version_id: UUID | None = Field(
        default=None,
        description=(
            "The version published right before this entry, not the version's "
            "parent; null when nothing was published."
        ),
    )
    changed_at: datetime
    reason: str | None = Field(
        default=None,
        description=(
            "Why the published version moved, e.g. approved through Context "
            "review, review decision undone, page restored from archive."
        ),
    )
    change_set_id: UUID

    model_config = ConfigDict(extra="forbid")


class WhatChangedOutput(BaseModel):
    envelope: MCPResultEnvelope
    entries: list[WhatChangedEntryModel] = Field(default_factory=list)
    since: datetime | None = None
    next_cursor: str | None = None
    has_more: bool = False

    model_config = ConfigDict(extra="forbid")


class GrepWikiInput(MCPConversationInput):
    # Literal-only by contract: user-supplied regex compiled with Python's
    # backtracking `re` has no execution bound (catastrophic backtracking =
    # CPU DoS from any viewer). A regex mode may return behind a
    # timeout-capable engine; until then `literal` is not an accepted field
    # (extra="forbid" turns it into validation_failed).
    workspace_id: UUID
    pattern: str = Field(min_length=1, max_length=512)
    scope: Literal["context"] = "context"
    context_lines: int = Field(default=2, ge=0, le=10)
    max_matches: int = Field(default=50, ge=1, le=200)

    model_config = ConfigDict(extra="forbid")


class GrepWikiOutput(BaseModel):
    envelope: MCPResultEnvelope
    matches: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ListChangesInput(MCPConversationInput):
    workspace_id: UUID
    target_type: Literal[
        "agent_page",
        "context_page",
        "library_item",
        "feedback",
        "workspace",
        "changeset",
    ]
    target_id: UUID
    include_diff: bool = False
    limit: int = Field(default=50, ge=1, le=200)

    model_config = ConfigDict(extra="forbid")


class ListChangesOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: ListChangesPayload | None = None

    model_config = ConfigDict(extra="forbid")


class ListAgentCategoriesInput(MCPConversationInput):
    workspace_id: UUID

    model_config = ConfigDict(extra="forbid")


class ListAgentCategoriesOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: list[AgentCategoryEntry] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ReadAgentPageInput(MCPConversationInput):
    workspace_id: UUID
    agent_page_id: UUID | None = None
    slug: str | None = None
    version_selector: str = "active"
    # D-2 twin: the ignored ``include`` list is removed (loud rejection).

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_id_or_slug(self):
        if self.agent_page_id is None and self.slug is None:
            raise ValueError("agent_page_id or slug is required")
        return self


class ReadAgentPageOutput(BaseModel):
    envelope: MCPResultEnvelope
    data: AgentPageDetail | None = None

    model_config = ConfigDict(extra="forbid")
