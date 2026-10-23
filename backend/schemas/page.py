from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from backend.schemas.common import (
    ContextHistoryStatus,
    DraftReviewReason,
    RESTResponseBase,
)
from backend.schemas.context_ingress import ContextPageType
from backend.schemas.edit_receipt import EditReceipt
from backend.schemas.input_policy import InputRequest
from backend.schemas.read import (
    PendingReviewInfo,
    PublishedVersionInfo,
    TreePathEntry,
    WikilinkTarget,
)

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
PAGE_SLUG_PATTERN = r"^[\p{L}\p{N}-]+$"
CONTENT_HASH_PATTERN = r"^[a-f0-9]{64}$"
PAGE_LIST_LIMIT_MAX = 100
PAGE_LIST_CURSOR_MAX_OFFSET = 10_000
PAGE_LIST_CURSOR_MAX_LENGTH = 512
PAGE_LIST_CURSOR_PATTERN = r"^[A-Za-z0-9_-]+={0,2}$"

# P8.5: the caller generates and holds this before the FIRST send, not on a
# retry -- there is no branch that executes because the field was omitted,
# unlike the MCP write tools' still-optional `idempotency_key` (P8.6 unifies
# those onto the same required contract later). Pattern/length match
# `backend.mcp.protocol.tool_descriptors.IDEMPOTENCY_KEY_FIELD` so a client
# can mint one key shape across Browser and MCP.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:-]+$"
CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD = Field(
    min_length=1,
    max_length=160,
    pattern=IDEMPOTENCY_KEY_PATTERN,
    description=(
        "24-hour retry key scoped to actor, workspace, and command. "
        "Generate and hold it before the first send."
    ),
)


class ContextPageLocation(BaseModel):
    workspace_id: UUID
    page_id: UUID

    model_config = ConfigDict(extra="forbid")


class ContextPageLocationResponse(RESTResponseBase):
    data: ContextPageLocation

    model_config = ConfigDict(extra="forbid")


class PageSummary(BaseModel):
    id: UUID
    slug: str
    title: str
    page_type: ContextPageType = "overview"
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    # Nullable to match ContextPage.current_version_id (set after the first
    # ContextPageVersion insert). Page-service write paths always pair page
    # row creation with a version insert, but exposing the underlying nullable
    # contract here means a mid-creation read (or admin tooling) cannot 500
    # on schema validation.
    visible_version_id: UUID | None = None
    current_version_id: UUID | None = None
    current_published_version_id: UUID | None = None
    published_version_no: int | None = None
    # Describes the version represented by this row/payload. Optional for
    # backwards-compatible schema generation and hand-constructed summaries.
    version_status: str | None = None
    history_status: ContextHistoryStatus | None = None
    # Set only with history_status="state_inconsistent": which invariant
    # failed, as stable codes.
    inconsistency_reasons: list[str] = Field(default_factory=list)
    graph_status: str | None = None
    is_visible_head: bool = False
    is_published_head: bool = False
    is_draft_head: bool = False
    is_alternative: bool = False
    is_discarded: bool = False
    updated_at: datetime
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    # Whether the page is published to the unauthenticated public read surface
    # (T16). Exposed on the authenticated read/list payloads so the publish UI
    # can initialize/reconcile its toggle without probing the anonymous URL.
    is_public: bool = False

    model_config = ConfigDict(extra="forbid")


class PagePayload(PageSummary):
    # Authenticated stable permalinks (`/p/{page_id}`) can be opened while a
    # different Workspace is selected. The authorized read response therefore
    # carries the owning Workspace identity so the client can switch before
    # resolving the page in that Workspace's Context tree. Public payloads do
    # not expose this field.
    workspace_id: UUID
    body_md: str
    version_id: UUID
    # P2.4 conflict-check value: bumped on every save to this version row
    # (autosave/update, editing or draft alike), covering body + title + slug
    # + tags + aliases + private metadata + usage as one monotonic counter.
    # A caller staging the next save states the value it last saw as
    # `expected_editing_revision`; content_hash alone cannot catch a
    # metadata-only change (see PageUpdateRequest's docstring).
    editing_revision: int = 1
    parent_version_id: UUID | None = None
    created_at: datetime
    alternative_draft_id: UUID | None = None
    change_set_id: UUID | None = None
    review_item_id: UUID | None = None
    preserved_as_alternative: bool = False
    superseded_review_count: int = 0
    # When this session last staged draft bytes, for the editor's "saved"
    # affordance only. `updated_at` keeps meaning the published/visible
    # instant, so the two must not be merged: an in-place autosave moves
    # page.updated_at but not the published version's created_at, and the
    # editor needs to show that a checkpoint landed even though the page's
    # visible timestamp did not move.
    draft_saved_at: datetime | None = None
    # Why this version is not published. Every draft carries at least one
    # stable reason, whether it is an everyday autosave, a workflow draft, or a
    # body with an objective format defect.
    publish_blocked_reasons: list[DraftReviewReason] = Field(default_factory=list)
    # What the write that produced this payload changed. Set by the MCP edit
    # commands and read by their handlers; a private attribute, so the REST
    # payload stays unchanged. The command ledger retains it separately for
    # an identical receipt when an idempotent patch is replayed.
    _edit_receipt: EditReceipt | None = PrivateAttr(default=None)


class PublicPagePayload(BaseModel):
    """The ONLY fields exposed by the unauthenticated public read endpoint
    (T16). A dedicated model that physically lacks every internal field —
    version ids, content hashes, change set / review ids, tags/aliases, actor
    identities, etc. — so no internal metadata can leak (security: this is NOT
    a filtered PagePayload). `id` is included (O-T16-2) to drive the
    logged-in→in-app redirect; the UUID grants no access on its own. The
    exposure scope is safe public policy state needed by the SPA to apply the
    crawler directive only to unlisted shares.
    """

    id: UUID
    slug: str
    title: str
    body_md: str
    updated_at: datetime
    exposure_scope: Literal["indexed_web", "unlisted_web"]

    model_config = ConfigDict(extra="forbid")


class PublishPageRequest(BaseModel):
    is_public: bool
    acknowledge_public_exposure: bool = False
    expected_published_version_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")


class PublishPageVersionRequest(InputRequest):
    reason: str = Field(min_length=1)
    publish_despite_findings: bool = False
    # The content the caller staged and means to publish. A draft row is
    # mutable, so without this a concurrent session of the same author can
    # change the body between staging and publishing. Required: this model is
    # only bound by the external publish route, and an omitted hash is an
    # opt-out of the check it exists to perform. In-process callers (review
    # approval, PageService) publish a version they already hold under the
    # write lock and call PageService.publish_draft directly, which keeps its
    # own optional parameter -- they never construct this request model.
    expected_content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)

    model_config = ConfigDict(extra="forbid")


class PageBacklink(BaseModel):
    id: UUID
    slug: str
    title: str
    updated_at: datetime
    occurrence_count: int = Field(ge=1)
    snippet: str | None = None

    model_config = ConfigDict(extra="forbid")


TAG_MAX_LENGTH = 48
TAGS_MAX_ITEMS = 32
ALIAS_MAX_LENGTH = 80
ALIASES_MAX_ITEMS = 32

# Maximum size of a single page body in bytes (UTF-8 chars).
# Beyond this clients see a 422 — protects the anchor resolver's distant-fuzzy
# scan (anchor_resolver.fuzzy_match with window=None) from DoS-grade scans on
# multi-megabyte pages, and bounds DB write / version_diff cost.
# 1 MB is generous for real markdown; raise only after re-checking the
# resolver and diff paths above.
BODY_MD_MAX_LENGTH = 1_000_000


def _check_tag_lengths(tags: list[str] | None) -> list[str] | None:
    if tags is None:
        return None
    for value in tags:
        if not isinstance(value, str):
            raise ValueError("tag must be a string")
        if len(value) > TAG_MAX_LENGTH:
            raise ValueError(
                f"tag too long: max {TAG_MAX_LENGTH} characters, got {len(value)}"
            )
    return tags


def _check_alias_lengths(aliases: list[str] | None) -> list[str] | None:
    if aliases is None:
        return None
    for value in aliases:
        if not isinstance(value, str):
            raise ValueError("alias must be a string")
        if len(value) > ALIAS_MAX_LENGTH:
            raise ValueError(
                f"alias too long: max {ALIAS_MAX_LENGTH} characters, got {len(value)}"
            )
    return aliases


class PageCreateRequest(InputRequest):
    workspace_slug: str = Field(min_length=1)
    # P2.3: omitting slug is page-first creation -- title defaults to "제목
    # 없음", slug to a fresh `p-{page_id}` (never a lookup key, so this never
    # upserts into an existing page), and an empty body is allowed. Naming a
    # slug keeps today's behavior: create if unused, else update that page
    # (PageService.write_page's existing slug-keyed contract, unchanged).
    slug: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=PAGE_SLUG_PATTERN
    )
    title: str | None = Field(default=None, min_length=1)
    body_md: str = Field(default="", max_length=BODY_MD_MAX_LENGTH)
    page_type: ContextPageType | None = None
    tags: list[str] | None = Field(
        default=None,
        max_length=TAGS_MAX_ITEMS,
    )
    aliases: list[str] | None = Field(
        default=None,
        max_length=ALIASES_MAX_ITEMS,
    )
    placement_parent_node_id: UUID | None = None
    as_draft: bool = False

    model_config = ConfigDict(extra="forbid")

    @field_validator("tags")
    @classmethod
    def _validate_tag_items(cls, value: list[str] | None) -> list[str] | None:
        return _check_tag_lengths(value)

    @field_validator("aliases")
    @classmethod
    def _validate_alias_items(cls, value: list[str] | None) -> list[str] | None:
        return _check_alias_lengths(value)

    @model_validator(mode="after")
    def _require_title_and_body_for_named_slug(self) -> "PageCreateRequest":
        # Only page-first creation (no slug) gets the "제목 없음" / empty-body
        # defaults -- naming a slug is today's write_page upsert contract,
        # which has always required both.
        if self.slug is not None and (not self.title or not self.body_md):
            raise ValueError("title and body_md are required when slug is given")
        return self


class PageVersionEntry(BaseModel):
    version_id: UUID
    version_no: int
    parent_version_id: UUID | None = None
    version_status: str
    review_disposition: Literal["rejected", "pending", "approved", "none"] = "none"
    history_status: ContextHistoryStatus
    graph_status: str
    actor_type: str
    actor_display_name: str | None = None
    created_at: datetime
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
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


class PageVersionDiff(BaseModel):
    baseline: Literal["version", "empty"] = "version"
    from_version_id: UUID | None = None
    to_version_id: UUID
    from_version_no: int
    to_version_no: int
    lines: list[str] = Field(default_factory=list)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    metadata_changes: list[
        Literal["title", "tags", "aliases", "page_type", "placement"]
    ] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class PageInlineCommentAnchor(BaseModel):
    anchor_text: str = Field(min_length=1, max_length=2000)
    context_before: str = Field(default="", max_length=500)
    context_after: str = Field(default="", max_length=500)
    version_id: UUID
    char_offset_hint: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class PageInlineCommentResolved(BaseModel):
    char_offset: int | None = Field(..., ge=0)
    end_offset: int | None = Field(..., ge=0)
    status: Literal["exact", "fuzzy", "fuzzy_distant", "orphan"]
    resolved_text: str | None = Field(...)

    model_config = ConfigDict(extra="forbid")


class PageInlineComment(BaseModel):
    id: UUID
    page_id: UUID
    review_item_id: UUID
    change_set_id: UUID
    change_operation_id: UUID
    status: str
    risk_level: str
    note: str
    anchor: PageInlineCommentAnchor
    resolved: PageInlineCommentResolved
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(extra="forbid")


class PageUpdateRequest(InputRequest):
    title: str | None = Field(default=None, min_length=1)
    body_md: str = Field(min_length=1, max_length=BODY_MD_MAX_LENGTH)
    page_type: ContextPageType | None = None
    tags: list[str] | None = Field(
        default=None,
        max_length=TAGS_MAX_ITEMS,
    )
    aliases: list[str] | None = Field(
        default=None,
        max_length=ALIASES_MAX_ITEMS,
    )
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
    session_version_id: UUID | None = None
    destination_node_id: UUID | None = None
    on_stale: Literal["conflict", "preserve_as_alternative"] = "conflict"

    model_config = ConfigDict(extra="forbid")

    @field_validator("tags")
    @classmethod
    def _validate_tag_items(cls, value: list[str] | None) -> list[str] | None:
        return _check_tag_lengths(value)

    @field_validator("aliases")
    @classmethod
    def _validate_alias_items(cls, value: list[str] | None) -> list[str] | None:
        return _check_alias_lengths(value)


class PageAutosaveRequest(PageUpdateRequest):
    """P8.1: `expected_editing_revision` is a required CAS input here, not
    optional -- unlike `PATCH /{page_id}` (`PageUpdateRequest`, which targets
    the page's own shared head and has no per-author "editing" row of its
    own), autosave always targets a specific actor's private "editing" row,
    where `content_hash` alone cannot catch a stale write that only changes
    title/tags/aliases/metadata (see `PageUpdateRequest.expected_editing_revision`'s
    comment for why the hash-only check is insufficient). Inherits
    `PageUpdateRequest`'s tag/alias length validators unchanged."""

    expected_editing_revision: int = Field(ge=1)
    idempotency_key: str = CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD
    # P8.5: ask "what happened to this key" without executing anything. A
    # caller unsure whether its own prior send landed uses this instead of
    # guessing -- a valid receipt still replays, but a missing/expired one
    # answers `operation_outcome_unknown` rather than autosaving fresh.
    retry_only: bool = False


class UpdateContextEditRequest(InputRequest):
    """P8.6: the MCP `update_context_edit` wire command -- the same autosave
    command as `PageAutosaveRequest`, minus `session_version_id`/`on_stale`.

    Those two fields back `PageService.autosave_page`'s
    `on_stale="preserve_as_alternative"` branch: resuming a REJECTED,
    already-submitted draft session by forking a successor from its exact
    rejected bytes. That flow predates the P8 editing/draft state machine and
    is out of scope for this tool (P8.6) -- MCP's plain editing-row autosave
    always targets the caller's own open "editing" row and always conflicts
    (never silently forks) on a stale base, matching `on_stale`'s default.
    Omitting the fields here (rather than accepting-and-ignoring them) keeps
    an agent from ever believing it can request the fork behavior through
    this tool.
    """

    title: str | None = Field(default=None, min_length=1)
    body_md: str = Field(min_length=1, max_length=BODY_MD_MAX_LENGTH)
    page_type: ContextPageType | None = None
    tags: list[str] | None = Field(default=None, max_length=TAGS_MAX_ITEMS)
    aliases: list[str] | None = Field(default=None, max_length=ALIASES_MAX_ITEMS)
    # Optional for the caller's own open edit: every save of that row bumps
    # editing_revision, so it alone is the concurrency check. Requiring the
    # version id and content hash as well only added two more values an agent
    # could hold stale. When sent, both are still checked.
    base_version_id: UUID | None = Field(
        default=None,
        description=(
            "Optional when saving your own open edit (expected_editing_revision "
            "is the concurrency check). If sent, base_content_hash must be sent "
            "too and both must match."
        ),
    )
    base_content_hash: str | None = Field(default=None, pattern=CONTENT_HASH_PATTERN)
    # Binds the save to the editing row the caller read. The revision alone
    # cannot say WHICH row that was: a row discarded or submitted since, and a
    # new one opened in its place, used to start from the same revisions.
    version_id: UUID | None = Field(
        default=None,
        description=(
            "Your open edit's version_id, from your last create/begin/update/"
            "patch response (or read_structure_map with version='my_edit'). "
            "When sent, the save applies only to that edit: if it was "
            "discarded or submitted, or another edit is open now, the call is "
            "a conflict and nothing is saved."
        ),
    )
    expected_editing_revision: int = Field(
        ge=1,
        description=(
            "The editing_revision from your last create/begin/update/patch "
            "response for this edit. Any other value is a conflict and nothing "
            "is saved; re-read the edit before trying again."
        ),
    )
    destination_node_id: UUID | None = None
    idempotency_key: str = CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD
    retry_only: bool = False

    model_config = ConfigDict(extra="forbid")

    @field_validator("tags")
    @classmethod
    def _validate_tag_items(cls, value: list[str] | None) -> list[str] | None:
        return _check_tag_lengths(value)

    @field_validator("aliases")
    @classmethod
    def _validate_alias_items(cls, value: list[str] | None) -> list[str] | None:
        return _check_alias_lengths(value)

    @model_validator(mode="after")
    def _base_is_all_or_nothing(self) -> "UpdateContextEditRequest":
        if (self.base_version_id is None) != (self.base_content_hash is None):
            raise ValueError(
                "send base_version_id and base_content_hash together, or neither"
            )
        return self


class MyContextEditSummary(BaseModel):
    """One row of `list_my_context_edits` (P8.3/P8.6): the caller's own open
    "editing" version on some page in the workspace. Deliberately omits an
    uninserted-media count and a conflict/reconciliation status -- there is no
    existing cheap per-version media-count helper (P4's artifact model has no
    such index) and no cheap per-version conflict signal today; adding either
    would be new machinery beyond this tool's read, not a cost this list
    should carry silently.
    """

    page_id: UUID
    version_id: UUID
    title: str
    slug: str
    base_version_id: UUID | None = None
    editing_revision: int = Field(ge=1)
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    last_saved_at: datetime

    model_config = ConfigDict(extra="forbid")


class ListMyContextEditsResult(BaseModel):
    items: list[MyContextEditSummary] = Field(default_factory=list)
    next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class PageMetadataPatch(InputRequest):
    """Metadata-only subset shared by REST page writes and MCP context writes."""

    title: str | None = Field(default=None, min_length=1)
    page_type: ContextPageType | None = None
    tags: list[str] | None = Field(default=None, max_length=TAGS_MAX_ITEMS)
    aliases: list[str] | None = Field(default=None, max_length=ALIASES_MAX_ITEMS)
    placement_parent_node_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("tags")
    @classmethod
    def _validate_tag_items(cls, value: list[str] | None) -> list[str] | None:
        return _check_tag_lengths(value)

    @field_validator("aliases")
    @classmethod
    def _validate_alias_items(cls, value: list[str] | None) -> list[str] | None:
        return _check_alias_lengths(value)


class PageRestoreRequest(BaseModel):
    version_id: UUID

    model_config = ConfigDict(extra="forbid")


class BeginContextEditRequest(BaseModel):
    """P8.3: resume-or-open the caller's own editing session.

    Both fields optional: omit both to start from whatever the current head
    happens to be (an idempotent resume of an already-open session ignores
    them entirely). Pass both together to assert the base explicitly and get
    a 409 instead of silently building on a head that moved.
    """

    base_version_id: UUID | None = None
    base_content_hash: str | None = None
    idempotency_key: str = CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD
    retry_only: bool = False

    model_config = ConfigDict(extra="forbid")


class SubmitContextEditRequest(InputRequest):
    version_id: UUID
    expected_editing_revision: int = Field(ge=1)
    reason: str = Field(default="author submitted for review", min_length=1)
    idempotency_key: str = CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD
    retry_only: bool = False

    model_config = ConfigDict(extra="forbid")


class DiscardContextEditRequest(BaseModel):
    version_id: UUID
    idempotency_key: str = CONTEXT_COMMAND_IDEMPOTENCY_KEY_FIELD
    retry_only: bool = False

    model_config = ConfigDict(extra="forbid")


class DiscardContextEditResult(BaseModel):
    """P8.5: the idempotency ledger needs a durable `BaseModel` result to
    replay, so `discard_context_edit` returns this instead of `None`."""

    version_id: UUID
    page_archived: bool

    model_config = ConfigDict(extra="forbid")


class PageInlineCommentCreateRequest(InputRequest):
    note: str = Field(min_length=1, max_length=5000)
    anchor: PageInlineCommentAnchor

    model_config = ConfigDict(extra="forbid")

    @field_validator("note")
    @classmethod
    def note_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("note must not be blank")
        return stripped


class ContextPageCreateRequest(InputRequest):
    """Canonical ``POST context/pages`` body (P8.1/P9.1).

    Deliberately has no ``slug``/``as_draft`` field: the canonical create
    command always runs ``create_page_first`` (new page + empty editing),
    regardless of whether a title/body is supplied. It never branches into
    ``write_page``'s upsert-by-slug behavior -- that ambiguity is exactly what
    P8.1 retired this route to get away from.
    """

    title: str | None = Field(default=None, min_length=1)
    body_md: str = Field(default="", max_length=BODY_MD_MAX_LENGTH)
    page_type: ContextPageType | None = None
    tags: list[str] | None = Field(default=None, max_length=TAGS_MAX_ITEMS)
    aliases: list[str] | None = Field(default=None, max_length=ALIASES_MAX_ITEMS)
    placement_parent_node_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("tags")
    @classmethod
    def _validate_tag_items(cls, value: list[str] | None) -> list[str] | None:
        return _check_tag_lengths(value)

    @field_validator("aliases")
    @classmethod
    def _validate_alias_items(cls, value: list[str] | None) -> list[str] | None:
        return _check_alias_lengths(value)


class ContextPageDetail(PagePayload):
    """The single canonical Context page detail shape (P9.1).

    A superset of ``PagePayload`` (PageService's P8-verified state-machine
    fields: ``editing_revision``, ``publish_blocked_reasons``,
    ``draft_saved_at``, the privacy-projected ``parent_version_id``, etc.)
    plus the read-only projection fields the old intermediate
    ``/context-pages/{page_id}`` route computed (tree placement,
    backlinks/wikilinks, lint warnings, pending review, reconciliation
    availability) -- see ``DomainReadService.context_page_read_enrichment``,
    the shared function that computes them. Every canonical
    ``/context/pages`` REST endpoint (list item, detail, and every editing
    command's response) returns this one shape; there is no second,
    thinner Context page payload on this route family.
    """

    latest_draft_version_id: UUID | None = None
    published_version_info: PublishedVersionInfo | None = None
    tree_path: list[TreePathEntry] = Field(default_factory=list)
    parent_node_id: UUID | None = None
    children_count: int = 0
    backlink_count: int = 0
    wikilink_targets: list[WikilinkTarget] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    pending_draft_version_id: UUID | None = None
    pending_draft_warnings: list[str] = Field(default_factory=list)
    pending_review: PendingReviewInfo | None = None
    reconciliation_pending_count: int = Field(default=0, ge=0)
    reconciliation_pending_count_truncated: bool = False
    next_reconciliation_version_id: UUID | None = None
    reconciliation_available: bool = False


class ContextPageDetailResponse(RESTResponseBase):
    data: ContextPageDetail

    model_config = ConfigDict(extra="forbid")


class PublicPageResponse(RESTResponseBase):
    data: PublicPagePayload

    model_config = ConfigDict(extra="forbid")


class PageBacklinkListResponse(RESTResponseBase):
    data: list[PageBacklink]

    model_config = ConfigDict(extra="forbid")


class PageVersionListResponse(RESTResponseBase):
    data: list[PageVersionEntry]
    next_cursor: str | None = None

    model_config = ConfigDict(extra="forbid")


class PageVersionResponse(RESTResponseBase):
    data: PageVersionEntry

    model_config = ConfigDict(extra="forbid")


class PageVersionDiffResponse(RESTResponseBase):
    data: PageVersionDiff

    model_config = ConfigDict(extra="forbid")


class PageTagListResponse(RESTResponseBase):
    data: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class PageInlineCommentResponse(RESTResponseBase):
    data: PageInlineComment

    model_config = ConfigDict(extra="forbid")


class PageInlineCommentListResponse(RESTResponseBase):
    data: list[PageInlineComment]

    model_config = ConfigDict(extra="forbid")
