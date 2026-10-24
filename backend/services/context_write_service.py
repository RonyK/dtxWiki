import difflib
import re
import unicodedata
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.context_media.application import SyncVersionUsage
from backend.context_media.domain import (
    ContextMediaError,
    MarkdownMediaPolicyError,
    validate_markdown_media_policy,
)
from backend.context_media.ports import ContextMediaRepository
from backend.domain.input_policy import InputPolicyError
from backend.domain.prepared_body import PreparedBody
from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.models.feedback import FeedbackItem
from backend.models.job import Job
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.mcp_chat import (
    McpChatArtifactKind,
    McpChatArtifactLink,
    McpChatSession,
)
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.repositories.context_page_repo import (
    get_page_by_id_for_update,
    get_page_by_slug_for_update,
    insert_version,
)
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.common import EnvelopeWarning
from backend.schemas.context_edit import (
    AnchorAddress,
    DeleteBlockOperation,
    EditOperation,
    InsertAfterOperation,
    InsertRowOperation,
    PatchDraftOperation,
    PatchDraftRequest,
    ReplaceBlockOperation,
    ResolveConflictOperation,
)
from backend.schemas.context_write import (
    BlockMapDeltaEntry,
    CitationInput,
    DraftReviewReason,
    PatchOperationResolution,
    PublishContextPageRequest,
    PublishContextPageResult,
    UpdateDraftRequest,
    UpdateDraftResult,
    WriteDraftRequest,
    WriteDraftResult,
)
from backend.schemas.input_policy import validate_request_inputs
from backend.schemas.page import BODY_MD_MAX_LENGTH, PageMetadataPatch
from backend.services.actor_visible_read import (
    context_page_visible,
    context_version_is_visible,
)
from backend.services.audit_service import AuditService
from backend.services.auto_approval import AutoApprovalDecision, evaluate_context_write
from backend.services.body_preparer import BodyInput, BodyPreparer, WriteContext
from backend.services.change_propagation import process_page_publish_maintenance
from backend.services.context_editing import (
    ContextEditError,
    PreparedBlockMap,
    ResolvedEdit,
    apply_operations,
    apply_operations_with_block_map,
    apply_resolved_edits,
    block_map_edit_plan,
    block_operation_provenance,
)
from backend.services.context_lifecycle import (
    apply_context_publication,
    assert_context_publish_candidate,
)
from backend.services.context_media_legacy_cutover import (
    require_no_visible_legacy_media,
)
from backend.services.context_push import (
    actual_target_ids_for_operations,
    authored_sides_by_target,
    build_clean_changes,
    build_conflict_details,
    changed_ids_between_maps,
    decide_push_outcome,
    hash_map,
    operation_target_id,
    rebase_operations_onto_map,
    reconciliation_target_sets,
    resolution_instruction,
    resolve_target_id_between_maps,
    target_ids_for_operations,
)
from backend.services.context_reconciliation_reminder import (
    enqueue_reconciliation_reminder,
)
from backend.services.context_visibility import (
    editable_context_version_id,
    visible_context_version_expression,
    visible_context_version_id,
)
from backend.services.db_write_intent import acquire_write_intent
from backend.services.job_queue import (
    JobQueueService,
    context_page_embed_idempotency_key,
)
from backend.services.library_canonical import (
    canonical_library_artifact_belongs_to_item,
    get_canonical_library_version,
)
from backend.services.library_storage import INLINE_TEXT_PREFIX
from backend.services.lint import publish_floor_findings
from backend.services.lint.rules_format import (
    body_relative_message,
)
from backend.services.mcp_client_service import find_trace_mcp_client
from backend.services.mcp_write_idempotency import (
    execute_idempotent_write,
    request_fingerprint,
)
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
from backend.services.page_name_lookup import unique_visible_page_name_match
from backend.services.page_service import (
    normalize_page_aliases,
    page_metadata_delta,
    pending_page_metadata_patch,
    refresh_page_links,
    stage_page_metadata_patch,
)
from backend.services.read_errors import (
    PageNameOccupiedError,
    ReadNotFoundError,
    ReadValidationError,
    RejectedByPolicyError,
    RevisionSubmissionRequiredError,
    StaleBaseVersionError,
    StaleContextDraftError,
)
from backend.services.review_risk_policy import (
    ReviewRiskResolution,
    ReviewRiskResolver,
    normalized_review_policy,
    review_risk_snapshot,
    risk_at_or_above,
)
from backend.services.review_supersession import supersede_review_with_lineage
from backend.services.tree_service import ensure_primary_tree_placement
from backend.spi.types import ActorContext
from backend.utils.block_identity import (
    BlockMapTransitionError,
    BlockOperationProvenance,
    block_map_delta,
    block_map_for_version,
    compute_block_map_with_ids,
    try_compute_incremental_block_map_with_ids,
    validated_block_map_for_version,
)
from backend.utils.block_map import compute_blocks
from backend.utils.content_hash import canonicalize_markdown, same_authored_markdown
from backend.utils.content_hash import content_hash as calculate_content_hash
from backend.utils.markdown_outline import parse_outline
from backend.utils.markdown_parser import parse_markdown
from backend.utils.page_frontmatter import validate_page_markdown_input
from backend.utils.uuid import parse_uuid_or_none


@dataclass(frozen=True)
class _ContextUpdateClassification:
    measured_event: str
    risk_floor_event: str
    format_only_eligible: bool
    change_operation: str


@dataclass(frozen=True)
class _PreparedDivergedPatch:
    prepared_body: PreparedBody
    pre_normalize_body: str
    normalized: bool
    outside_target_changed: bool
    risk_resolution: ReviewRiskResolution
    predicted_block_map: list[dict]
    operation_receipts: list[PatchOperationResolution]

    @property
    def body(self) -> str:
        return self.prepared_body.body


@dataclass(frozen=True)
class _RetiredConflictRestoration:
    conflict_id: str
    position: str
    neighbor_id: str | None
    content: str
    base_index: int


_DIVERGED_MANIFEST_SCHEMA = "context_push_diverged_manifest.v1"


@dataclass(frozen=True)
class UnresolvedDivergedBranch:
    page_id: UUID
    page_slug: str
    version_id: UUID


@dataclass(frozen=True)
class StaleConversationPage:
    """One actor-visible page whose conversation base trails its linear head."""

    page_id: UUID
    read_version_id: UUID
    head_version_id: UUID
    changed_block_ids: tuple[str, ...]
    omitted_changed_block_count: int
    changed_by_actor_kind: str
    changed_by_actor_id: str
    changed_by_actor_display_name: str | None
    changed_in_chat_session_id: UUID | None


_STALE_CONVERSATION_SCAN_LIMIT = 16
_STALE_CONVERSATION_RESULT_LIMIT = 4
_STALE_CONVERSATION_MESSAGE_PAGE_LIMIT = 3
_STALE_CONVERSATION_BLOCK_ID_LIMIT = 5
_STALE_CONVERSATION_BLOCK_ID_MAX_CHARS = 80
_STALE_CONVERSATION_ACTOR_LABEL_MAX_CHARS = 80
_STALE_CONVERSATION_MAX_VERSION_GAP = 512


def _next_block_map_for_edits(
    *,
    base_version: ContextPageVersion,
    provisional_body: str,
    final_body: str,
    edits: tuple[ResolvedEdit, ...] | None,
    identity_seed: str | None,
    prepared_block_map: PreparedBlockMap | None = None,
) -> list[dict]:
    """Compute one next-version map, preferring the proven §3.3 fast path."""

    if (
        prepared_block_map is not None
        and provisional_body == final_body == prepared_block_map.body
        and identity_seed == prepared_block_map.identity_seed
    ):
        return prepared_block_map.value
    previous_map = block_map_for_version(
        base_version.block_map,
        base_version.body_md,
    )
    edit_plan = block_map_edit_plan(edits)
    if edit_plan is not None and provisional_body == final_body:
        try:
            incremental = try_compute_incremental_block_map_with_ids(
                final_body,
                previous_body_md=base_version.body_md,
                previous_block_map=previous_map,
                edits=edit_plan,
                identity_seed=identity_seed,
            )
        except BlockMapTransitionError as exc:
            raise ReadValidationError(
                str(exc),
                error_code="operation_unsupported",
            ) from exc
        if incremental is not None:
            return incremental
    return compute_block_map_with_ids(
        final_body,
        previous_body_md=base_version.body_md,
        previous_block_map=previous_map,
        operation_provenance=block_operation_provenance(edits),
        identity_seed=identity_seed,
    )


def _bounded_actor_label(value: str | None) -> str | None:
    """Keep actor provenance useful without injecting multiline warning text."""

    if value is None:
        return None
    label = " ".join(value.split())
    if not label:
        return None
    return label[:_STALE_CONVERSATION_ACTOR_LABEL_MAX_CHARS]


def unresolved_diverged_branches_for_conversation(
    session: Session,
    *,
    workspace_id: UUID,
    conversation_token: str,
) -> list[UnresolvedDivergedBranch]:
    chat_session_id = session.scalar(
        select(McpChatSession.id).where(
            McpChatSession.workspace_id == workspace_id,
            McpChatSession.mcp_session_id == conversation_token,
        )
    )
    if chat_session_id is None:
        return []
    rows = session.execute(
        select(
            ContextPage.id.label("page_id"),
            ContextPage.slug.label("page_slug"),
            ContextPageVersion.id.label("version_id"),
        )
        .join(ChangeSet, ChangeSet.id == ContextPageVersion.change_set_id)
        .join(ContextPage, ContextPage.id == ContextPageVersion.page_id)
        .where(
            ChangeSet.workspace_id == workspace_id,
            ChangeSet.origin_chat_session_id == chat_session_id,
            ContextPageVersion.graph_status == "branched",
        )
        .order_by(
            ContextPage.slug,
            ContextPageVersion.created_at,
            ContextPageVersion.id,
        )
    ).all()
    return [
        UnresolvedDivergedBranch(
            page_id=row.page_id,
            page_slug=row.page_slug,
            version_id=row.version_id,
        )
        for row in rows
    ]


def unresolved_diverged_branch_warning_message(
    branches: list[UnresolvedDivergedBranch],
    *,
    workspace_id: UUID,
) -> str:
    resolutions = "; ".join(
        "page "
        f"{branch.page_slug} ({branch.page_id}), version {branch.version_id}: "
        "reconcile it with the REST reconciliation endpoint POST "
        f"/api/v1/workspaces/{workspace_id}/context/pages/{branch.page_id}"
        f"/versions/{branch.version_id}/reconciliation, or explicitly discard "
        "it with the REST discard_alternative_draft endpoint POST "
        f"/api/v1/workspaces/{workspace_id}/context/pages/{branch.page_id}"
        f"/versions/{branch.version_id}/discard"
        for branch in branches
    )
    return (
        "Unresolved diverged branch(es) owned by this conversation: "
        f"{resolutions}. No MCP tool is currently available for reconciling "
        "or discarding branches."
    )


def stale_pages_for_conversation(
    session: Session,
    *,
    workspace_id: UUID,
    conversation_token: str,
    actor: ActorContext,
    excluded_page_ids: frozenset[UUID] = frozenset(),
) -> list[StaleConversationPage]:
    """Return a bounded, actor-visible block delta for stale read pointers.

    The token lookup repeats the ownership boundary used during protocol
    resolution so this helper cannot become a token or cross-Workspace oracle
    when called from a future surface.  A page/version/map inconsistency is
    skipped (fail closed) rather than reflected into the warning.
    """

    chat = session.scalar(
        select(McpChatSession).where(
            McpChatSession.workspace_id == workspace_id,
            McpChatSession.mcp_session_id == conversation_token,
        )
    )
    if chat is None or not _conversation_chat_is_owned_by_actor(
        session,
        chat=chat,
        workspace_id=workspace_id,
        actor=actor,
    ):
        return []

    visible_head_id = visible_context_version_expression(actor)
    # One conversation can observe the same page through different semantic
    # relations (for example REFERENCED by a read, then EDITED by a patch).
    # Rank the correctness-bearing observations per page so the stale scan is
    # page-based and never emits duplicates or lets an older verb-specific row
    # shadow the conversation's actual latest base.
    ranked_links = (
        select(
            McpChatArtifactLink.id.label("link_id"),
            func.row_number()
            .over(
                partition_by=McpChatArtifactLink.resource_id,
                order_by=(
                    McpChatArtifactLink.last_read_at.desc(),
                    McpChatArtifactLink.id.desc(),
                ),
            )
            .label("read_rank"),
        )
        .where(
            McpChatArtifactLink.workspace_id == workspace_id,
            McpChatArtifactLink.chat_session_id == chat.id,
            McpChatArtifactLink.resource_kind == McpChatArtifactKind.CONTEXT_PAGE.value,
            McpChatArtifactLink.last_read_version_id.is_not(None),
        )
        .subquery()
    )
    links = session.scalars(
        select(McpChatArtifactLink)
        .join(ranked_links, ranked_links.c.link_id == McpChatArtifactLink.id)
        .join(
            ContextPage,
            (ContextPage.workspace_id == McpChatArtifactLink.workspace_id)
            & (ContextPage.id == McpChatArtifactLink.resource_id),
        )
        .where(
            McpChatArtifactLink.workspace_id == workspace_id,
            McpChatArtifactLink.chat_session_id == chat.id,
            McpChatArtifactLink.resource_kind == McpChatArtifactKind.CONTEXT_PAGE.value,
            ranked_links.c.read_rank == 1,
            ContextPage.lifecycle_status != "archived",
            visible_head_id.is_not(None),
            McpChatArtifactLink.last_read_version_id != visible_head_id,
        )
        .order_by(McpChatArtifactLink.last_read_at.desc())
        .limit(_STALE_CONVERSATION_SCAN_LIMIT)
    ).all()

    stale: list[StaleConversationPage] = []
    for link in links:
        if link.resource_id in excluded_page_ids:
            continue
        page = session.scalar(
            select(ContextPage).where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.id == link.resource_id,
            )
        )
        if not context_page_visible(page, actor, workspace_id=workspace_id):
            continue
        assert page is not None
        read_version = session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.workspace_id == workspace_id,
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.id == link.last_read_version_id,
            )
        )
        head_version_id, _is_draft = visible_context_version_id(page, actor)
        head_version = (
            session.scalar(
                select(ContextPageVersion).where(
                    ContextPageVersion.workspace_id == workspace_id,
                    ContextPageVersion.page_id == page.id,
                    ContextPageVersion.id == head_version_id,
                )
            )
            if head_version_id is not None
            else None
        )
        if (
            read_version is None
            or head_version is None
            or read_version.id == head_version.id
            or read_version.workspace_id != workspace_id
            or head_version.workspace_id != workspace_id
            or read_version.page_id != page.id
            or head_version.page_id != page.id
            or not context_version_is_visible(read_version, actor)
            or not context_version_is_visible(head_version, actor)
            or not _version_is_linear_ancestor(
                session,
                ancestor=read_version,
                descendant=head_version,
            )
        ):
            continue
        try:
            changed_ids = sorted(
                str(changed_id)[:_STALE_CONVERSATION_BLOCK_ID_MAX_CHARS]
                for changed_id in changed_ids_between_maps(
                    block_map_for_version(
                        read_version.block_map,
                        read_version.body_md,
                    ),
                    block_map_for_version(
                        head_version.block_map,
                        head_version.body_md,
                    ),
                    base_body_md=read_version.body_md,
                    candidate_body_md=head_version.body_md,
                )
            )
        except (KeyError, TypeError, ValueError):
            # A malformed historical map is not safe evidence for a block-level
            # warning.  Skip this page instead of leaking raw parser/DB details.
            continue
        changed_in_chat_session_id = (
            session.scalar(
                select(ChangeSet.origin_chat_session_id).where(
                    ChangeSet.workspace_id == workspace_id,
                    ChangeSet.id == head_version.change_set_id,
                )
            )
            if head_version.change_set_id is not None
            else None
        )
        stale.append(
            StaleConversationPage(
                page_id=page.id,
                read_version_id=read_version.id,
                head_version_id=head_version.id,
                changed_block_ids=tuple(
                    changed_ids[:_STALE_CONVERSATION_BLOCK_ID_LIMIT]
                ),
                omitted_changed_block_count=max(
                    0,
                    len(changed_ids) - _STALE_CONVERSATION_BLOCK_ID_LIMIT,
                ),
                changed_by_actor_kind=head_version.created_by_actor_kind,
                changed_by_actor_id=head_version.created_by_actor_id,
                changed_by_actor_display_name=_bounded_actor_label(
                    head_version.actor_display_name
                ),
                changed_in_chat_session_id=changed_in_chat_session_id,
            )
        )
        if len(stale) >= _STALE_CONVERSATION_RESULT_LIMIT:
            break
    return stale


def stale_conversation_warning_message(
    pages: list[StaleConversationPage],
) -> str:
    """Format only bounded server identities; never page-authored text."""

    summaries: list[str] = []
    for page in pages[:_STALE_CONVERSATION_MESSAGE_PAGE_LIMIT]:
        changed = ", ".join(page.changed_block_ids) or "none"
        if page.omitted_changed_block_count:
            changed += f" (+{page.omitted_changed_block_count} omitted)"
        actor = page.changed_by_actor_display_name or page.changed_by_actor_id
        provenance = f"changed by {actor} ({page.changed_by_actor_kind})"
        if page.changed_in_chat_session_id is not None:
            provenance += f", conversation {page.changed_in_chat_session_id}"
        summaries.append(
            f"page {page.page_id}, version {page.read_version_id} -> "
            f"{page.head_version_id}, changed block IDs: {changed}, {provenance}"
        )
    omitted_pages = max(0, len(pages) - _STALE_CONVERSATION_MESSAGE_PAGE_LIMIT)
    if omitted_pages:
        summaries.append(f"+{omitted_pages} additional stale page(s) omitted")
    return (
        "This conversation's page base is behind the visible head: "
        + "; ".join(summaries)
        + ". Re-read each page with read_structure_map before editing it."
    )


def _conversation_chat_is_owned_by_actor(
    session: Session,
    *,
    chat: McpChatSession,
    workspace_id: UUID,
    actor: ActorContext,
) -> bool:
    if actor.user_id is not None:
        return chat.owner_user_id == actor.user_id
    client = find_trace_mcp_client(
        session,
        workspace_id=workspace_id,
        actor=actor,
    )
    return (
        client is not None
        and chat.owner_user_id is None
        and chat.client_id == client.id
    )


def _version_is_linear_ancestor(
    session: Session,
    *,
    ancestor: ContextPageVersion,
    descendant: ContextPageVersion,
) -> bool:
    """Prove ancestry with one bounded query; branch siblings are not stale."""

    version_gap = descendant.version_no - ancestor.version_no
    if version_gap <= 0 or version_gap > _STALE_CONVERSATION_MAX_VERSION_GAP:
        return False
    rows = session.execute(
        select(
            ContextPageVersion.id,
            ContextPageVersion.parent_version_id,
        ).where(
            ContextPageVersion.workspace_id == ancestor.workspace_id,
            ContextPageVersion.page_id == ancestor.page_id,
            ContextPageVersion.version_no > ancestor.version_no,
            ContextPageVersion.version_no <= descendant.version_no,
        )
    ).all()
    parents = {row.id: row.parent_version_id for row in rows}
    current_id = descendant.id
    for _ in range(version_gap):
        parent_id = parents.get(current_id)
        if parent_id == ancestor.id:
            return True
        if parent_id is None:
            return False
        current_id = parent_id
    return False


class ContextWriteService:
    def __init__(
        self,
        audit_service: AuditService | None = None,
        job_queue: JobQueueService | None = None,
        context_media_repository_factory: Callable[[Session], ContextMediaRepository]
        | None = None,
        media_installer_factory: Callable[[Session, UUID], object] | None = None,
        body_preparer: BodyPreparer | None = None,
    ) -> None:
        self.audit_service = audit_service or AuditService()
        self.risk_resolver = ReviewRiskResolver()
        self.job_queue = job_queue or JobQueueService()
        self.context_media_repository_factory = context_media_repository_factory
        self.media_installer_factory = media_installer_factory
        self.body_preparer = body_preparer or BodyPreparer()

    def _prepare_body(
        self,
        markdown: str,
        *,
        workspace: Workspace,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        session: Session,
        page: ContextPage | None = None,
        version: ContextPageVersion | None = None,
    ) -> PreparedBody:
        try:
            return self.body_preparer.prepare(
                BodyInput(markdown),
                WriteContext(
                    workspace=workspace,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                    page=page,
                    version=version,
                    session=session,
                ),
            )
        except MarkdownMediaPolicyError as exc:
            raise RejectedByPolicyError(str(exc), error_code=exc.error_code) from exc

    def preview_body(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        markdown: str,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
    ) -> PreparedBody:
        return self.body_preparer.preview(
            markdown,
            WriteContext(
                workspace=self._require_workspace(session, workspace_id),
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                session=session,
            ),
        )

    def _prepared_base_matches(
        self,
        base_version: ContextPageVersion,
        new_body: str,
        *,
        workspace: Workspace,
        page: ContextPage,
        session: Session,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
    ) -> bool:
        try:
            return (
                self._prepare_body(
                    base_version.body_md,
                    workspace=workspace,
                    session=session,
                    page=page,
                    version=base_version,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                ).body
                == new_body
            )
        except (InputPolicyError, RejectedByPolicyError):
            # Historical invalid bytes can still be repaired by a valid new
            # write. They cannot establish a format-only auto-approval proof.
            return False

    def _enqueue_embed(
        self,
        session: Session,
        *,
        page: ContextPage,
        version: ContextPageVersion,
        marker: str,
    ) -> None:
        # Auto-index the page for vector retrieval (Epic-1 §6): the worker's
        # embed handler re-chunks + re-embeds the page's published + draft
        # scopes (a no-op when no embedding provider is configured). The key is
        # version-addressed: a content revert must enqueue again because the
        # worker resolves the current page head rather than a historical hash.
        # The enqueue shares the domain transaction so content and its trigger
        # commit atomically. In a split deployment the row is workspace-local;
        # S0 outbox/EventSink relays it to the control-side worker. Overlay jobs
        # must not reuse this core Job table.
        self.job_queue.enqueue(
            session=session,
            workspace_id=page.workspace_id,
            job_type="embed",
            trigger_type=f"context_{marker}",
            idempotency_key=context_page_embed_idempotency_key(
                page.id,
                marker,
                version.id,
                version.content_hash,
            ),
            target_ref={"kind": "context_page", "id": str(page.id)},
        )

    def enqueue_head_reindex(
        self,
        session: Session,
        *,
        page: ContextPage,
        transition_version: ContextPageVersion,
    ) -> None:
        """Queue the publish-path reindex after a visible-head transition.

        A rejected draft was already queued with the ``draft`` marker. Using
        its unused ``published`` marker makes this transition addressable and
        idempotent while the worker still resolves the page's resulting head.
        """

        self._enqueue_embed(
            session,
            page=page,
            version=transition_version,
            marker="published",
        )

    def count_workspace_pages(self, session: Session, workspace_id: UUID) -> int:
        return (
            session.scalar(
                select(func.count())
                .select_from(ContextPage)
                .where(ContextPage.workspace_id == workspace_id)
            )
            or 0
        )

    def write_draft(
        self,
        session: Session,
        workspace_id: UUID,
        request: WriteDraftRequest,
        actor_kind: str,
        actor_id: str,
        actor_role: str,
        origin_chat_session_id: UUID | None = None,
        force_new_page: bool = False,
        *,
        actor_provenance: str = "internal",
    ) -> WriteDraftResult:
        # B-P1-4 Sprint_6 Fix_4: wrap the entire write_draft body in a
        # SAVEPOINT so a mid-write exception rolls back every partial
        # INSERT/UPDATE (page, version, change_set, claim, citations,
        # tree placement, review, audit) without affecting the outer
        # request transaction. Without the savepoint, the intermediate
        # ``session.flush()`` calls only push pending state to the DB —
        # they don't commit it — but a caller that catches the exception
        # and continues using the session would still see the dirty
        # in-session rows and risk emitting them on a later flush.
        # SQLite: FOR UPDATE is a no-op and pysqlite defers BEGIN until the first
        # DML, so take write intent before the guarded reads and the version_no
        # allocation in insert_version. Outside the SAVEPOINT on purpose: the
        # writer lock belongs to the whole transaction, not to the rollback
        # scope. PostgreSQL keeps using the narrower row locks below.
        validate_request_inputs(request)
        acquire_write_intent(session)
        self._lock_context_media_usage_intent(session, workspace_id)
        with session.begin_nested():
            workspace = self._require_workspace(session, workspace_id)
            prepared_body = self._prepare_body(
                request.body_markdown,
                workspace=workspace,
                session=session,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
            )
            request = request.model_copy(update={"body_markdown": prepared_body.body})
            missing_citation_warnings = self._citation_warnings(
                workspace,
                request.citations,
            )
            citation_warnings = [
                *missing_citation_warnings,
                *self._workflow_warnings(request.citations),
                *self._dead_end_warnings(session, request.citations),
            ]
            if self._is_strict(workspace) and missing_citation_warnings:
                raise RejectedByPolicyError(
                    "core claim missing citation under strict policy"
                )
            manual_citation = any(
                citation.created_via == "manual_input" for citation in request.citations
            )
            gate = evaluate_context_write(
                session,
                workspace_id=workspace_id,
                needs_review=request.needs_review,
                body=request.body_markdown,
                lint_registry=self.body_preparer.registry,
            )
            risk_resolution = self.risk_resolver.resolve(
                workspace=workspace,
                event="context_draft_create",
                signals={
                    "citation_warning": bool(citation_warnings),
                    "manual_citation": manual_citation,
                    "risk_notes_present": bool(request.risk_notes),
                    "review_required": True,
                    **gate.force_signals(),
                },
            )
            page, page_created = self._get_or_create_page(
                session=session,
                workspace_id=workspace_id,
                title=request.title,
                page_type=request.page_type,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_role=actor_role,
                force_new_page=force_new_page,
            )
            parent_version_id = page.latest_draft_version_id or page.current_version_id
            parent_version = (
                get_in_workspace(
                    session,
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    resource_id=parent_version_id,
                )
                if parent_version_id is not None
                else None
            )
            metadata_patch = (
                {} if page_created else self._write_draft_metadata_patch(page, request)
            )
            change_set = self._create_change_set(
                session=session,
                workspace_id=workspace_id,
                reason=request.reason,
                trigger_type="mcp_write_draft",
                actor_kind=actor_kind,
                actor_id=actor_id,
                risk_level=risk_resolution.risk_level,
                cite_call_skipped=manual_citation,
                origin_chat_session_id=origin_chat_session_id,
            )
            version = self._insert_version(
                session=session,
                page=page,
                parent_version_id=parent_version_id,
                body=prepared_body,
                actor_kind=actor_kind,
                actor_id=actor_id,
                change_set_id=change_set.id,
                version_status="draft",
                graph_status="linear",
            )
            page.latest_draft_version_id = version.id
            page.current_version_id = page.current_version_id or version.id
            page.lifecycle_status = (
                "published" if page.current_published_version_id else "draft"
            )
            if page_created:
                page.page_type = request.page_type
                page.placement_parent_node_id = request.placement_parent_node_id
            else:
                self._record_metadata_patch(
                    session=session,
                    change_set_id=change_set.id,
                    page=page,
                    version=version,
                    metadata_patch=metadata_patch,
                )
            ensure_primary_tree_placement(
                session=session,
                workspace_id=workspace_id,
                target_kind="context_page",
                target_id=page.id,
                title=page.title,
                slug=page.slug,
                section="context",
                parent_id=(
                    request.placement_parent_node_id
                    if page_created
                    else page.placement_parent_node_id
                ),
                default_parent_slug="inbox",
                actor_kind=actor_kind,
                actor_id=actor_id,
                can_have_children=True,
            )
            claim = self._insert_claim(
                session=session,
                version=version,
                body_markdown=request.body_markdown,
                has_citation=bool(request.citations),
            )
            self._insert_citations(
                session=session,
                workspace_id=workspace_id,
                version=version,
                claim=claim,
                citations=request.citations,
            )
            operation = ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="context_page",
                target_id=page.id,
                operation="create_draft",
                diff_summary=request.reason,
                diff_ref=f"context_page_version:{version.id}",
            )
            session.add(operation)
            review = self._create_review_item(
                session=session,
                workspace_id=workspace_id,
                change_set_id=change_set.id,
                risk_level=change_set.risk_level,
                risk_resolution=risk_resolution,
                decision_summary=(
                    request.risk_notes or "context draft requires review"
                ),
                # `risk_notes` is caller-supplied free text with no fixed
                # translation; only the fallback copy gets a code.
                decision_summary_code=(
                    None if request.risk_notes else "context_draft_requires_review"
                ),
            )
            self.audit_service.emit(
                session=session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="context_page.draft.create",
                target_kind="context_page",
                target_id=page.id,
                payload={
                    "version_id": str(version.id),
                    "change_set_id": str(change_set.id),
                    "citation_warnings": citation_warnings,
                },
            )
            session.flush()
            refresh_page_links(session, page=page, version=version)
            self._enqueue_embed(session, page=page, version=version, marker="draft")
            return WriteDraftResult(
                page_id=page.id,
                draft_version_id=version.id,
                change_set_id=change_set.id,
                review_item_id=review.id,
                persisted_content_hash=version.content_hash,
                persisted_character_count=len(version.body_md),
                outcome="created_draft" if page_created else "updated_draft",
                body_changed=(
                    parent_version is None
                    or not same_authored_markdown(
                        parent_version.body_md, version.body_md
                    )
                ),
                metadata_changed=page_created or bool(metadata_patch),
                version_status=version.version_status,
                latest_draft_version_id=page.latest_draft_version_id,
                current_published_version_id=page.current_published_version_id,
                review_status=review.status,
                editing_revision=version.editing_revision,
                citation_warnings=citation_warnings,
                review_reasons=[
                    DraftReviewReason(code=reason.code, message=reason.message)
                    for reason in gate.reasons
                ],
                next_action=(
                    "more_citation_needed"
                    if missing_citation_warnings
                    else "review_required"
                ),
            )

    @classmethod
    def _identical_body_requires_fork(
        cls,
        session: Session,
        version: ContextPageVersion,
    ) -> bool:
        """Whether reusing ``version`` would leave the author with no way forward."""

        return version.version_status == "superseded_snapshot" or any(
            review.status == "rejected"
            for review in cls._reviews_for_version(session, version)
        )

    @classmethod
    def _is_restartable_rejected_version(
        cls,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
    ) -> bool:
        """Whether ``version`` can start a new lineage after head retirement."""

        if (
            page.latest_draft_version_id is not None
            or version.version_status != "draft"
        ):
            return False
        version_ref = f"context_page_version:{version.id}"
        return (
            session.scalar(
                select(ReviewItem.id)
                .join(ChangeSet, ChangeSet.id == ReviewItem.change_set_id)
                .join(ChangeOperation, ChangeOperation.change_set_id == ChangeSet.id)
                .where(
                    ReviewItem.workspace_id == page.workspace_id,
                    ChangeSet.workspace_id == page.workspace_id,
                    ReviewItem.review_type == "context_update",
                    ReviewItem.status == "rejected",
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page.id,
                    ChangeOperation.diff_ref == version_ref,
                )
                .limit(1)
            )
            is not None
        )

    def update_draft(
        self,
        session: Session,
        workspace_id: UUID,
        request: UpdateDraftRequest,
        actor_kind: str,
        actor_id: str,
        origin_chat_session_id: UUID | None = None,
        *,
        actor_provenance: str = "internal",
        auto_approve_format_only: bool = True,
        allow_changes_requested_revision: bool = False,
    ) -> UpdateDraftResult:
        # B-P1-4 Sprint_6 Fix_4: wrap update_draft in a SAVEPOINT (see
        # write_draft for rationale). A failure during citation insert or
        # feedback lookup will roll back the new version row and
        # change_set together.
        # SQLite: see write_draft. Write intent goes outside the SAVEPOINT so it
        # covers the guarded read below and the version_no allocation.
        validate_request_inputs(request)
        acquire_write_intent(session)
        self._lock_context_media_usage_intent(session, workspace_id)
        with session.begin_nested():
            page = self._lock_page_and_apply_update_guards(
                session=session,
                workspace_id=workspace_id,
                page_id=request.page_id,
                allow_changes_requested_revision=allow_changes_requested_revision,
            )
            base_version = get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=request.base_version_id,
            )
            latest = self._latest_edit_base(session, page)
            restart_after_reject = (
                base_version is not None
                and base_version.page_id == page.id
                and base_version.content_hash == request.base_content_hash
                and self._is_restartable_rejected_version(session, page, base_version)
            )
            if (
                base_version is None
                or base_version.page_id != page.id
                or (
                    not restart_after_reject
                    and (latest is None or latest.id != base_version.id)
                )
                or base_version.content_hash != request.base_content_hash
            ):
                disclosed_id, disclosed_hash = self._disclosable_stale_base_fields(
                    latest, actor_kind, actor_id
                )
                raise StaleBaseVersionError(
                    f"base version is stale; latest={disclosed_id} "
                    f"hash={disclosed_hash}",
                    latest_version_id=disclosed_id,
                    latest_content_hash=disclosed_hash,
                )
            # base_content_hash alone only covers the markdown body: a caller
            # that changed only title/tags/aliases/metadata on this same row
            # does not move it, so a second, stale caller whose body still
            # matches would pass the check above and silently clobber the
            # metadata change. editing_revision is bumped on every save to
            # this row regardless of what changed.
            if (
                request.expected_editing_revision is not None
                and request.expected_editing_revision != base_version.editing_revision
            ):
                disclosed_id, disclosed_hash = self._disclosable_stale_base_fields(
                    base_version, actor_kind, actor_id
                )
                raise StaleBaseVersionError(
                    f"editing revision is stale; latest={disclosed_id} "
                    f"revision={base_version.editing_revision}",
                    latest_version_id=disclosed_id,
                    latest_content_hash=disclosed_hash,
                )

            new_body = self._body_for_update(base_version, request)
            classification = _ContextUpdateClassification(
                measured_event=_measure_update_event(base_version.body_md, new_body),
                risk_floor_event=(
                    "context_full_rewrite"
                    if request.update_mode == "full_rewrite"
                    else "context_partial_update"
                ),
                format_only_eligible=request.update_mode == "full_rewrite",
                change_operation=request.update_mode,
            )
            return self._persist_context_update(
                session=session,
                workspace_id=workspace_id,
                page=page,
                base_version=base_version,
                restart_after_reject=restart_after_reject,
                new_body=new_body,
                metadata_patch=request.metadata_patch,
                citations=request.citations,
                feedback_id=request.feedback_id,
                needs_review=request.needs_review,
                reason=request.reason,
                classification=classification,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                origin_chat_session_id=origin_chat_session_id,
                auto_approve_format_only=auto_approve_format_only,
            )

    def patch_draft(
        self,
        session: Session,
        workspace_id: UUID,
        request: PatchDraftRequest,
        actor_kind: str,
        actor_id: str,
        origin_chat_session_id: UUID | None = None,
        *,
        actor_provenance: str = "internal",
        auto_approve_format_only: bool = True,
        require_current_base: bool = False,
    ) -> UpdateDraftResult:
        validate_request_inputs(request)
        anchor_operation_count = sum(
            isinstance(getattr(operation, "at", None), AnchorAddress)
            for operation in request.operations
        )
        if anchor_operation_count:
            # Usage-based deprecation signal. The audit record contains no
            # authored anchor text; workspace scope lives on AuditEvent and the
            # durable chat id supplies the conversation dimension.
            self.audit_service.emit(
                session=session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="mcp.patch_draft.anchor_address_used",
                target_kind="context_page",
                target_id=request.page_id,
                payload={
                    "conversation_session_id": (
                        str(origin_chat_session_id)
                        if origin_chat_session_id is not None
                        else None
                    ),
                    "operation_count": anchor_operation_count,
                },
            )

        def freeze_base(candidate: PatchDraftRequest) -> PatchDraftRequest:
            if candidate.base is not None:
                return candidate
            ledger_version = self._resolve_conversation_read_version(
                session,
                workspace_id=workspace_id,
                origin_chat_session_id=origin_chat_session_id,
                page_id=candidate.page_id,
            )
            if ledger_version is None:
                return candidate
            return candidate.model_copy(update={"base": str(ledger_version.id)})

        def execute_with(
            effective_request: PatchDraftRequest,
            *,
            effective_idempotency_key: str,
            fingerprint: str,
        ) -> UpdateDraftResult:
            identity_seed = request_fingerprint(
                {
                    "effective_idempotency_key": effective_idempotency_key,
                    "effective_base": effective_request.base,
                    "fingerprint": fingerprint,
                }
            )
            return self._patch_draft_once(
                session=session,
                workspace_id=workspace_id,
                request=effective_request,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                origin_chat_session_id=origin_chat_session_id,
                auto_approve_format_only=auto_approve_format_only,
                identity_seed=identity_seed,
                require_current_base=require_current_base,
            )

        # An explicit key identifies the raw caller intent, not mutable ambient
        # read-ledger state. Claim/replay it before resolving an omitted base so
        # the same retry remains replayable after the conversation reads a
        # newer version. The base is frozen exactly once only when the winner
        # actually executes.
        if request.idempotency_key is not None:
            fingerprint_payload = request.model_dump(
                mode="json", exclude={"dry_run", "idempotency_key"}
            )
            if not request.excluded_clean_change_ids:
                # Additive default: keep pre-workbench fingerprints stable so
                # an in-flight explicit replay survives this schema upgrade.
                fingerprint_payload.pop("excluded_clean_change_ids", None)
            if require_current_base:
                fingerprint_payload["_require_current_base"] = True
            fingerprint = request_fingerprint(fingerprint_payload)
            legacy_fingerprints: tuple[str, ...] = ()
            if request.reconciles is None and not require_current_base:
                legacy_payload = request.model_dump(
                    mode="json",
                    exclude={"reconciles"},
                )
                if not request.excluded_clean_change_ids:
                    legacy_payload.pop("excluded_clean_change_ids", None)
                legacy_fingerprints = (request_fingerprint(legacy_payload),)

            def execute_explicit() -> UpdateDraftResult:
                effective_request = freeze_base(request)
                return execute_with(
                    effective_request,
                    effective_idempotency_key=request.idempotency_key,
                    fingerprint=fingerprint,
                )

            if request.dry_run:
                return execute_explicit()
            return execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                tool_name="patch_draft",
                idempotency_key=request.idempotency_key,
                fingerprint=fingerprint,
                compatible_fingerprints=legacy_fingerprints,
                result_model=UpdateDraftResult,
                execute=execute_explicit,
                replay_transform=lambda result: result.model_copy(
                    update={"push_outcome": "replayed"}
                ),
            )

        effective_request = freeze_base(request)
        fingerprint_payload = effective_request.model_dump(
            mode="json", exclude={"dry_run", "idempotency_key"}
        )
        if not effective_request.excluded_clean_change_ids:
            fingerprint_payload.pop("excluded_clean_change_ids", None)
        if require_current_base:
            fingerprint_payload["_require_current_base"] = True
        fingerprint = request_fingerprint(fingerprint_payload)
        effective_idempotency_key = "auto:" + request_fingerprint(
            {
                "origin_chat_session_id": (
                    str(origin_chat_session_id)
                    if origin_chat_session_id is not None
                    else None
                ),
                "effective_base": effective_request.base,
                "fingerprint": fingerprint,
            }
        )
        effective_request = effective_request.model_copy(
            update={"idempotency_key": effective_idempotency_key}
        )

        def execute_auto() -> UpdateDraftResult:
            return execute_with(
                effective_request,
                effective_idempotency_key=effective_idempotency_key,
                fingerprint=fingerprint,
            )

        if effective_request.dry_run:
            return execute_auto()
        return execute_idempotent_write(
            session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            tool_name="patch_draft",
            idempotency_key=effective_idempotency_key,
            fingerprint=fingerprint,
            result_model=UpdateDraftResult,
            execute=execute_auto,
            replay_transform=lambda result: result.model_copy(
                update={"push_outcome": "replayed"}
            ),
        )

    def _patch_draft_once(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        request: PatchDraftRequest,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        origin_chat_session_id: UUID | None,
        auto_approve_format_only: bool,
        identity_seed: str,
        require_current_base: bool,
    ) -> UpdateDraftResult:
        acquire_write_intent(session)
        self._lock_context_media_usage_intent(session, workspace_id)
        with session.begin_nested():
            page = self._lock_page_and_apply_update_guards(
                session=session,
                workspace_id=workspace_id,
                page_id=request.page_id,
                allow_changes_requested_revision=False,
            )
            # SQLAlchemy may return an already-cached identity for the locked
            # SELECT. Refresh under the lock before resolving the editable head.
            session.refresh(page)

            # Resolve the authoritative editable head while holding the page
            # row lock. `base_version` is HEAD (H); `request.base` is the
            # frozen caller/read-ledger base (B) judged against it below.
            base_version = self._latest_edit_base(session, page)
            if base_version is None:
                raise ReadNotFoundError("editable context page version not found")
            workspace = self._require_workspace(session, workspace_id)

            # Section 6.2: a reconcile resubmission's own operations resolve
            # only the conflicting blocks a prior diverged response named;
            # everything else effective_operations carries is the branch's
            # own clean contribution, synthesized below and folded in before
            # this request goes through the exact same committed/rebased/
            # diverged decision as any other push (reconciling onto a head
            # that has since moved AGAIN correctly re-diverges rather than
            # silently overwriting).
            effective_operations: list[EditOperation] = request.operations
            reconciled_branch: ContextPageVersion | None = None
            caller_receipt_plan: list[tuple[int | None, str, str]] = []

            requested_base_version: ContextPageVersion | None = None
            push_decision = None
            prepared_stale_patch: _PreparedDivergedPatch | None = None
            stale_base_edits: tuple[ResolvedEdit, ...] | None = None
            actual_target_ids: frozenset[str] | None = None
            # A block-addressed stale batch can prove rebase safety per
            # target. Legacy/whole-document operations cannot, so a stale
            # base for those operations fails closed below rather than being
            # applied to content the caller did not read.
            # patch_draft() already froze an omitted base from the read ledger
            # before deriving the key/fingerprint. Never look it up again here:
            # a concurrent read must not change the base after that identity.
            effective_base = request.base
            if effective_base is None:
                raise ReadValidationError(
                    "patch_draft requires an explicit base or a conversation read",
                    error_code="base_required",
                )
            requested_base_version = self._resolve_push_base(
                session, page, effective_base
            )
            if requested_base_version is None:
                raise ReadValidationError(
                    "base version not found", error_code="base_not_found"
                )
            if require_current_base and requested_base_version.id != base_version.id:
                disclosed_id, disclosed_hash = self._disclosable_stale_base_fields(
                    base_version, actor_kind, actor_id
                )
                raise StaleBaseVersionError(
                    "reconciliation head advanced; refresh before committing",
                    latest_version_id=disclosed_id,
                    latest_content_hash=disclosed_hash,
                )

            if request.reconciles:
                (
                    reconciled_branch,
                    resolved_conflict_ops,
                    carried_ops,
                    retired_restorations,
                    resolution_edit_indices,
                ) = self._resolve_reconcile_operations(
                    session=session,
                    page=page,
                    reconcile_base_version=requested_base_version,
                    reconciles=request.reconciles,
                    operations=request.operations,
                    excluded_clean_change_ids=request.excluded_clean_change_ids,
                )
                effective_operations = [*resolved_conflict_ops, *carried_ops]
                restoration_receipt_indices = {
                    restoration.conflict_id: len(effective_operations) + index
                    for index, restoration in enumerate(retired_restorations)
                }
                edit_index = 0
                for operation in request.operations:
                    if isinstance(operation, ResolveConflictOperation):
                        source_index = resolution_edit_indices.get(operation.block)
                        if source_index is None:
                            source_index = restoration_receipt_indices.get(
                                operation.block
                            )
                        if operation.resolution != "keep_head" and source_index is None:
                            raise RuntimeError(
                                "applied conflict resolution has no edit receipt"
                            )
                        caller_receipt_plan.append(
                            (
                                source_index,
                                operation.op,
                                "no_effect"
                                if operation.resolution == "keep_head"
                                else "applied",
                            )
                        )
                    else:
                        caller_receipt_plan.append(
                            (edit_index, operation.op, "applied")
                        )
                        edit_index += 1
            else:
                retired_restorations = []
                if any(
                    isinstance(operation, ResolveConflictOperation)
                    for operation in request.operations
                ):
                    raise ReadValidationError(
                        "resolve_conflict is only valid with reconciles",
                        error_code="reconcile_target_invalid",
                    )
                effective_operations = list(request.operations)
                caller_receipt_plan = [
                    (index, operation.op, "applied")
                    for index, operation in enumerate(request.operations)
                ]

            def project_caller_receipts(
                result: UpdateDraftResult,
                *,
                source: list[PatchOperationResolution] | None = None,
            ) -> UpdateDraftResult:
                available = source if source is not None else result.operation_receipts
                available = available or []
                projected: list[PatchOperationResolution] = []
                for index, operation_name, synthetic_outcome in caller_receipt_plan:
                    if index is None or index >= len(available):
                        projected.append(
                            PatchOperationResolution(
                                op=operation_name,
                                outcome=synthetic_outcome,
                                chars_delta=0,
                            )
                        )
                    else:
                        projected.append(available[index])
                target_map = result.predicted_block_map
                if target_map is None and result.draft_version_id is not None:
                    result_version = session.scalar(
                        select(ContextPageVersion).where(
                            ContextPageVersion.workspace_id == workspace_id,
                            ContextPageVersion.page_id == page.id,
                            ContextPageVersion.id == result.draft_version_id,
                        )
                    )
                    if result_version is None:
                        raise RuntimeError(
                            "patch result version is unavailable for map delta"
                        )
                    target_map = block_map_for_version(
                        result_version.block_map, result_version.body_md
                    )
                if target_map is None:
                    # A result without a new/predicted version is necessarily a
                    # no-effect projection of the locked head.
                    target_map = head_block_map
                source_map = (
                    requested_base_map
                    if result.push_outcome == "diverged"
                    else head_block_map
                )
                return result.model_copy(
                    update={
                        "operation_receipts": projected,
                        "block_map_delta": [
                            BlockMapDeltaEntry.model_validate(entry)
                            for entry in block_map_delta(source_map, target_map)
                        ],
                    }
                )

            target_ids = target_ids_for_operations(effective_operations)
            base_is_stale = requested_base_version.id != base_version.id
            if retired_restorations and base_is_stale:
                disclosed_id, disclosed_hash = self._disclosable_stale_base_fields(
                    base_version, actor_kind, actor_id
                )
                raise StaleBaseVersionError(
                    "restore_yours requires the current linear head as base",
                    latest_version_id=disclosed_id,
                    latest_content_hash=disclosed_hash,
                )
            restoration_target_ids = {
                restoration.neighbor_id
                for restoration in retired_restorations
                if restoration.neighbor_id is not None
            }
            if retired_restorations and any(
                restoration.neighbor_id is None for restoration in retired_restorations
            ):
                if base_is_stale:
                    target_ids = None
            elif target_ids is not None:
                target_ids = frozenset(target_ids | restoration_target_ids)
            if target_ids is None and base_is_stale:
                # Legacy/whole-document operations do not name a block/row
                # target. A stale write therefore has no sound rebase proof or
                # conflict set: never apply it to content the caller did not
                # read and never invent a positional target.
                disclosed_id, disclosed_hash = self._disclosable_stale_base_fields(
                    base_version, actor_kind, actor_id
                )
                raise StaleBaseVersionError(
                    "unaddressable patch_draft operation has a stale base",
                    latest_version_id=disclosed_id,
                    latest_content_hash=disclosed_hash,
                )
            requested_base_map = block_map_for_version(
                requested_base_version.block_map,
                requested_base_version.body_md,
            )
            head_block_map = block_map_for_version(
                base_version.block_map, base_version.body_md
            )
            if base_is_stale:
                try:
                    stale_body, stale_base_edits, stale_prepared_block_map = (
                        self._apply_operations_with_restorations(
                            body=requested_base_version.body_md,
                            block_map=requested_base_map,
                            operations=effective_operations,
                            restorations=retired_restorations,
                            identity_seed=identity_seed,
                        )
                    )
                except ContextEditError as exc:
                    raise ReadValidationError(
                        str(exc),
                        error_code=exc.error_code,
                        warnings=list(exc.warnings),
                    ) from exc
                prepared_stale_patch = self._prepare_diverged_patch(
                    workspace=workspace,
                    session=session,
                    page=page,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                    base_version=requested_base_version,
                    new_body=stale_body,
                    edits=stale_base_edits,
                    identity_seed=identity_seed,
                    prepared_block_map=stale_prepared_block_map,
                )
                actual_target_ids = actual_target_ids_for_operations(
                    effective_operations,
                    base_block_map=requested_base_map,
                    candidate_block_map=prepared_stale_patch.predicted_block_map,
                    base_body_md=requested_base_version.body_md,
                    candidate_body_md=prepared_stale_patch.body,
                )
                if prepared_stale_patch.body != requested_base_version.body_md:
                    actual_target_ids = frozenset(
                        actual_target_ids | restoration_target_ids
                    )
            else:
                actual_target_ids = target_ids
            push_decision = decide_push_outcome(
                base_version_id=requested_base_version.id,
                head_version_id=base_version.id,
                base_block_map=requested_base_map,
                head_block_map=head_block_map,
                target_ids=actual_target_ids,
                base_body_md=requested_base_version.body_md,
                head_body_md=base_version.body_md,
            )

            if (
                base_is_stale
                and not actual_target_ids
                and not request.needs_review
                and not self._identical_body_requires_fork(session, base_version)
            ):
                assert prepared_stale_patch is not None
                no_effect_classification = _ContextUpdateClassification(
                    measured_event=_measure_update_event(
                        base_version.body_md, base_version.body_md
                    ),
                    risk_floor_event="context_partial_update",
                    format_only_eligible=False,
                    change_operation="context_patch",
                )
                _gate, risk_resolution = self._resolve_update_risk(
                    session,
                    workspace=workspace,
                    classification=no_effect_classification,
                    new_body=base_version.body_md,
                    needs_review=request.needs_review,
                    manual_citation=False,
                )
                result = self._unchanged_update_result(
                    page,
                    base_version,
                    request.reason,
                    operation_receipts=prepared_stale_patch.operation_receipts,
                ).model_copy(
                    update={
                        "push_outcome": "rebased",
                        "changed_since_base_ids": sorted(push_decision.changed_ids),
                        "normalized": prepared_stale_patch.normalized,
                        "outside_target_changed": (
                            prepared_stale_patch.outside_target_changed
                        ),
                        "predicted_block_map": (
                            head_block_map if request.dry_run else None
                        ),
                        "risk": risk_resolution.risk_level,
                    }
                )
                no_effect_receipts = [
                    receipt.model_copy(
                        update={"outcome": "no_effect", "chars_delta": 0}
                    )
                    for receipt in prepared_stale_patch.operation_receipts
                ]
                result = project_caller_receipts(result, source=no_effect_receipts)
                if reconciled_branch is not None and not request.dry_run:
                    self._complete_reconciled_branch(
                        session=session,
                        workspace_id=workspace_id,
                        page=page,
                        branch=reconciled_branch,
                        result=result,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                    )
                return result

            if push_decision is not None and push_decision.outcome == "diverged":
                assert actual_target_ids is not None
                assert prepared_stale_patch is not None
                assert stale_base_edits is not None
                conflicting_ids = push_decision.changed_ids & actual_target_ids
                prepared_diverged = prepared_stale_patch
                diverged_edits = stale_base_edits
                if request.dry_run:
                    return project_caller_receipts(
                        UpdateDraftResult(
                            diff_summary=request.reason,
                            persisted_content_hash=(
                                prepared_diverged.prepared_body.content_hash
                            ),
                            persisted_character_count=len(prepared_diverged.body),
                            outcome="updated_draft",
                            body_changed=(
                                prepared_diverged.body != requested_base_version.body_md
                            ),
                            metadata_changed=False,
                            version_status=requested_base_version.version_status,
                            latest_draft_version_id=page.latest_draft_version_id,
                            current_published_version_id=(
                                page.current_published_version_id
                            ),
                            push_outcome="diverged",
                            changed_since_base_ids=sorted(conflicting_ids),
                            normalized=prepared_diverged.normalized,
                            outside_target_changed=(
                                prepared_diverged.outside_target_changed
                            ),
                            risk=prepared_diverged.risk_resolution.risk_level,
                            preview_body_markdown=prepared_diverged.body,
                            operation_receipts=prepared_diverged.operation_receipts,
                            predicted_block_map=prepared_diverged.predicted_block_map,
                            conflicts=build_conflict_details(
                                conflicting_ids=conflicting_ids,
                                base_body_md=requested_base_version.body_md,
                                base_block_map=block_map_for_version(
                                    requested_base_version.block_map,
                                    requested_base_version.body_md,
                                ),
                                head_body_md=base_version.body_md,
                                head_block_map=block_map_for_version(
                                    base_version.block_map, base_version.body_md
                                ),
                                head_version_id=base_version.id,
                                head_actor_kind=base_version.actor_type,
                                head_actor_display_name=base_version.actor_display_name,
                                head_created_at=base_version.created_at,
                                operations=effective_operations,
                                authored_body_md=prepared_diverged.body,
                                authored_block_map=(
                                    prepared_diverged.predicted_block_map
                                ),
                            ),
                            clean_changes=build_clean_changes(
                                changed_ids=push_decision.changed_ids,
                                conflicting_ids=conflicting_ids,
                                head_body_md=base_version.body_md,
                                head_block_map=block_map_for_version(
                                    base_version.block_map, base_version.body_md
                                ),
                            ),
                            resolution_instruction=resolution_instruction(),
                        )
                    )
                result = self._persist_diverged_branch(
                    session=session,
                    workspace_id=workspace_id,
                    page=page,
                    requested_base_version=requested_base_version,
                    head_version=base_version,
                    operations=effective_operations,
                    prepared=prepared_diverged,
                    edits=diverged_edits,
                    identity_seed=identity_seed,
                    reason=request.reason,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    origin_chat_session_id=origin_chat_session_id,
                    changed_ids=push_decision.changed_ids,
                    conflicting_ids=conflicting_ids,
                    actual_target_ids=actual_target_ids,
                    supersedes_branch=reconciled_branch,
                )
                result = project_caller_receipts(result)
                if reconciled_branch is not None:
                    self._complete_reconciled_branch(
                        session=session,
                        workspace_id=workspace_id,
                        page=page,
                        branch=reconciled_branch,
                        result=result,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        completion="rediverged",
                    )
                return result

            operations_for_head = effective_operations
            active_effective_indices = list(range(len(effective_operations)))
            if base_is_stale and push_decision.outcome == "rebased":
                assert actual_target_ids is not None
                active_effective_indices = [
                    index
                    for index, operation in enumerate(effective_operations)
                    if operation_target_id(operation) in actual_target_ids
                ]
                operations_for_head = [
                    effective_operations[index] for index in active_effective_indices
                ]

            def project_head_receipts(result: UpdateDraftResult) -> UpdateDraftResult:
                if len(operations_for_head) == len(effective_operations):
                    return project_caller_receipts(result)
                assert prepared_stale_patch is not None
                merged = [
                    receipt.model_copy(
                        update={"outcome": "no_effect", "chars_delta": 0}
                    )
                    for receipt in prepared_stale_patch.operation_receipts
                ]
                head_receipts = iter(result.operation_receipts or [])
                for index in active_effective_indices:
                    merged[index] = next(head_receipts)
                return project_caller_receipts(result, source=merged)

            # Every non-dry_run write now carries an idempotency key (explicit
            # or server-derived -- see patch_draft's MCP wrapper), so a retry
            # is replayed from the ledger before it ever reaches apply_operations.
            # That handler therefore never guesses whether a past write already
            # landed; markdown_analysis here is table-anchor ADDRESS RECOVERY
            # only (Auto-fix may have re-padded a table row's column widths),
            # computed internally by apply_operations when an operation needs it.
            try:
                new_body, edits, prepared_block_map = (
                    self._apply_operations_with_restorations(
                        body=base_version.body_md,
                        block_map=block_map_for_version(
                            base_version.block_map, base_version.body_md
                        ),
                        operations=operations_for_head,
                        restorations=retired_restorations,
                        identity_seed=identity_seed,
                    )
                )
            except ContextEditError as exc:
                # Relay the recovery facts. Without them the caller only learns
                # that the edit failed and must re-read the whole page, which is
                # the cost this tool exists to remove.
                raise ReadValidationError(
                    str(exc),
                    error_code=exc.error_code,
                    warnings=list(exc.warnings),
                ) from exc

            # B-2: an explicit needs_review must never be silently dropped,
            # even when every operation resolved to a no-op -- mirrors the
            # identical guard _persist_context_update already applies to its
            # own no-op-under-Auto-fix shortcut (`and not needs_review`
            # below). A caller that asked for review gets one; do not invent
            # a second mechanism for the same rule.
            if (
                all(edit.no_op for edit in edits)
                and not request.needs_review
                and not request.dry_run
                and reconciled_branch is None
                and not self._identical_body_requires_fork(session, base_version)
            ):
                no_effect_classification = _ContextUpdateClassification(
                    measured_event=_measure_update_event(
                        base_version.body_md, base_version.body_md
                    ),
                    risk_floor_event="context_partial_update",
                    format_only_eligible=False,
                    change_operation="context_patch",
                )
                _gate, risk_resolution = self._resolve_update_risk(
                    session,
                    workspace=workspace,
                    classification=no_effect_classification,
                    new_body=base_version.body_md,
                    needs_review=request.needs_review,
                    manual_citation=False,
                )
                result = self._unchanged_update_result(
                    page,
                    base_version,
                    request.reason,
                    operation_receipts=_operation_receipts(
                        base_version.body_md,
                        edits,
                        provisional_body=base_version.body_md,
                        final_body=base_version.body_md,
                    ),
                )
                result = result.model_copy(
                    update={
                        "push_outcome": push_decision.outcome,
                        "changed_since_base_ids": sorted(push_decision.changed_ids),
                        "risk": risk_resolution.risk_level,
                    }
                )
                if reconciled_branch is not None and not request.dry_run:
                    self._complete_reconciled_branch(
                        session=session,
                        workspace_id=workspace_id,
                        page=page,
                        branch=reconciled_branch,
                        result=result,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                    )
                return project_caller_receipts(result)

            classification = _ContextUpdateClassification(
                measured_event=_measure_update_event(base_version.body_md, new_body),
                risk_floor_event=(
                    "context_full_rewrite"
                    if any(
                        operation.op == "replace_document"
                        for operation in operations_for_head
                    )
                    else "context_partial_update"
                ),
                format_only_eligible=False,
                change_operation="context_patch",
            )
            if request.dry_run:
                if len(new_body) > BODY_MD_MAX_LENGTH:
                    raise ReadValidationError(
                        "merged context body exceeds the maximum length",
                        error_code="body_length_exceeded",
                    )
                pre_normalize_body = new_body
                prepared_body = self._prepare_body(
                    new_body,
                    workspace=workspace,
                    session=session,
                    page=page,
                    version=base_version,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    actor_provenance=actor_provenance,
                )
                new_body = prepared_body.body
                outside_target_changed = _body_changed_outside_edit_ranges(
                    base_version.body_md,
                    pre_normalize_body,
                    new_body,
                    edits,
                )
                # B-1: a preview must classify the body it is actually
                # previewing (post-normalization), the same as a real write
                # would -- otherwise the preview's risk disagrees with what
                # persisting the identical request produces.
                classification = replace(
                    classification,
                    measured_event=_measure_update_event(
                        base_version.body_md,
                        new_body,
                        outside_target_changed=outside_target_changed,
                    ),
                )
                _gate, risk_resolution = self._resolve_update_risk(
                    session,
                    workspace=workspace,
                    classification=classification,
                    new_body=new_body,
                    needs_review=request.needs_review,
                    manual_citation=False,
                )
                requires_fork = self._identical_body_requires_fork(
                    session, base_version
                )
                engine_noop = all(edit.no_op for edit in edits)
                normalized_noop = (
                    workspace.auto_fix_enabled
                    and new_body == base_version.body_md
                    and not requires_fork
                )
                would_create_workflow = (
                    request.needs_review
                    or requires_fork
                    or not (engine_noop or normalized_noop)
                )
                would_create_version = would_create_workflow and (
                    not same_authored_markdown(new_body, base_version.body_md)
                    or requires_fork
                )
                return project_head_receipts(
                    UpdateDraftResult(
                        draft_version_id=None,
                        diff_summary=request.reason,
                        persisted_content_hash=prepared_body.content_hash,
                        persisted_character_count=len(new_body),
                        outcome=(
                            "no_change"
                            if not would_create_workflow
                            else "updated_draft"
                            if would_create_version
                            else "review_only"
                        ),
                        body_changed=not same_authored_markdown(
                            new_body, base_version.body_md
                        ),
                        metadata_changed=False,
                        version_status=(
                            "draft"
                            if would_create_version
                            else base_version.version_status
                        ),
                        latest_draft_version_id=page.latest_draft_version_id,
                        current_published_version_id=page.current_published_version_id,
                        review_status=("pending" if would_create_workflow else None),
                        normalized=new_body != pre_normalize_body,
                        outside_target_changed=outside_target_changed,
                        risk=risk_resolution.risk_level,
                        preview_body_markdown=new_body,
                        operation_receipts=_operation_receipts(
                            base_version.body_md,
                            edits,
                            provisional_body=pre_normalize_body,
                            final_body=new_body,
                        ),
                        predicted_block_map=_next_block_map_for_edits(
                            base_version=base_version,
                            provisional_body=pre_normalize_body,
                            final_body=new_body,
                            edits=edits,
                            identity_seed=identity_seed,
                            prepared_block_map=prepared_block_map,
                        ),
                        push_outcome=(
                            push_decision.outcome if push_decision is not None else None
                        ),
                        changed_since_base_ids=(
                            sorted(push_decision.changed_ids)
                            if push_decision is not None
                            else None
                        ),
                    )
                )
            result = self._persist_context_update(
                session=session,
                workspace_id=workspace_id,
                page=page,
                base_version=base_version,
                restart_after_reject=False,
                new_body=new_body,
                metadata_patch=None,
                citations=[],
                feedback_id=None,
                needs_review=request.needs_review,
                reason=request.reason,
                classification=classification,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                origin_chat_session_id=origin_chat_session_id,
                auto_approve_format_only=auto_approve_format_only,
                edits=edits,
                identity_seed=identity_seed,
                prepared_block_map=prepared_block_map,
                force_new_version=reconciled_branch is not None,
            )
            if push_decision is not None:
                result = result.model_copy(
                    update={
                        "push_outcome": push_decision.outcome,
                        "changed_since_base_ids": sorted(push_decision.changed_ids),
                    }
                )
            if reconciled_branch is not None and (
                push_decision is None or push_decision.outcome != "diverged"
            ):
                self._complete_reconciled_branch(
                    session=session,
                    workspace_id=workspace_id,
                    page=page,
                    branch=reconciled_branch,
                    result=result,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
            return project_head_receipts(result)

    @staticmethod
    def _unchanged_update_result(
        page: ContextPage,
        base_version: ContextPageVersion,
        reason: str,
        *,
        operation_receipts: list[PatchOperationResolution] | None = None,
    ) -> UpdateDraftResult:
        return UpdateDraftResult(
            draft_version_id=base_version.id,
            diff_summary=reason,
            persisted_content_hash=base_version.content_hash,
            persisted_character_count=len(base_version.body_md),
            outcome="no_change",
            body_changed=False,
            metadata_changed=False,
            version_status=base_version.version_status,
            latest_draft_version_id=page.latest_draft_version_id,
            current_published_version_id=page.current_published_version_id,
            review_status=None,
            editing_revision=base_version.editing_revision,
            # Nothing ran auto-fix or touched the document -- both are known
            # facts, not absent ones, so they are False rather than null.
            # `risk` stays null: no classification ran because there was
            # nothing to classify. `operation_receipts` is real when the
            # caller resolved operations before finding them all no-ops
            # (patch_draft); update_draft has no per-operation concept, so it
            # stays null there.
            normalized=False,
            outside_target_changed=False,
            operation_receipts=operation_receipts,
        )

    @staticmethod
    def _lock_page_and_apply_update_guards(
        *,
        session: Session,
        workspace_id: UUID,
        page_id: UUID,
        allow_changes_requested_revision: bool,
    ) -> ContextPage:
        page = get_page_by_id_for_update(session, page_id)
        if page is None or page.workspace_id != workspace_id:
            raise ReadNotFoundError("context page not found")
        if allow_changes_requested_revision:
            return page
        active_change_request = session.scalar(
            select(ReviewItem)
            .join(ChangeSet, ChangeSet.id == ReviewItem.change_set_id)
            .join(
                ChangeOperation,
                ChangeOperation.change_set_id == ChangeSet.id,
            )
            .where(
                ReviewItem.workspace_id == workspace_id,
                ChangeSet.workspace_id == workspace_id,
                ReviewItem.status == "changes_requested",
                ReviewItem.review_type == "context_update",
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page.id,
            )
            .order_by(ReviewItem.created_at.desc(), ReviewItem.id.desc())
        )
        if active_change_request is not None:
            raise RevisionSubmissionRequiredError(active_change_request.id)
        return page

    def _resolve_update_risk(
        self,
        session: Session,
        *,
        workspace: Workspace,
        classification: _ContextUpdateClassification,
        new_body: str,
        needs_review: bool,
        manual_citation: bool,
    ) -> tuple[AutoApprovalDecision, ReviewRiskResolution]:
        """Gate + risk resolution shared by a real persist and a dry-run
        preview of the same write -- a preview promising a risk level the
        real write would then re-classify differently is worse than not
        previewing risk at all (see patch_draft's dry_run branch).
        """
        gate = evaluate_context_write(
            session,
            workspace_id=workspace.id,
            needs_review=needs_review,
            body=new_body,
            lint_registry=self.body_preparer.registry,
        )
        declared_defaults = normalized_review_policy(workspace.review_policy)[
            "defaults"
        ]
        risk_resolution = self.risk_resolver.resolve(
            workspace=workspace,
            event=classification.measured_event,
            floor_level=declared_defaults.get(
                classification.risk_floor_event, "medium"
            ),
            signals={
                "manual_citation": manual_citation,
                "review_required": True,
                **gate.force_signals(),
            },
        )
        return gate, risk_resolution

    @staticmethod
    def _apply_operations_with_restorations(
        *,
        body: str,
        block_map: list[dict],
        operations: list[EditOperation],
        restorations: list[_RetiredConflictRestoration],
        identity_seed: str | None = None,
    ) -> tuple[str, tuple[ResolvedEdit, ...], PreparedBlockMap | None]:
        # Block-addressed batches resolve from the persisted map's local
        # ``gap + lines`` coordinates and validate each addressed raw slice by
        # hash.  Building an outline here would parse the complete document
        # before that fast path even gets a chance to run.  Legacy text/section
        # operations still need the outline and keep the authoritative parser
        # path.
        if operations:
            outline = (
                None
                if target_ids_for_operations(operations) is not None
                else parse_outline(body)
            )
            applied = apply_operations_with_block_map(
                body,
                outline,
                operations,
                block_map=block_map,
                identity_seed=identity_seed,
            )
            provisional_body = applied.body
            edits = applied.edits
            predicted_block_map = applied.predicted_block_map
        else:
            provisional_body, edits, predicted_block_map = body, (), None
        if not restorations:
            return provisional_body, edits, predicted_block_map

        fresh_blocks = compute_blocks(body)
        if len(fresh_blocks) != len(block_map):
            raise ReadValidationError(
                "reconcile base block map no longer matches its body",
                error_code="reconcile_target_invalid",
            )
        parsed = parse_markdown(body)
        newline = "\r\n" if "\r\n" in body else "\r" if "\r" in body else "\n"
        grouped: dict[
            tuple[str, str | None],
            list[tuple[_RetiredConflictRestoration, str]],
        ] = {}
        for restoration in sorted(restorations, key=lambda item: item.base_index):
            normalized_content = restoration.content.rstrip("\r\n") + newline
            if len(compute_blocks(normalized_content)) != 1:
                raise ReadValidationError(
                    "restore_yours must restore exactly one top-level block",
                    error_code="reconcile_target_invalid",
                )
            grouped.setdefault(
                (restoration.position, restoration.neighbor_id), []
            ).append((restoration, normalized_content))

        restoration_edits: list[ResolvedEdit] = []
        for (position, neighbor_id), members in grouped.items():
            normalized_contents = [content for _restoration, content in members]
            restored_content = newline.join(normalized_contents)
            if len(compute_blocks(restored_content)) != len(members):
                raise ReadValidationError(
                    "combined restore_yours blocks are structurally ambiguous",
                    error_code="reconcile_target_invalid",
                )
            if position == "document":
                if body or block_map or len(grouped) != 1:
                    raise ReadValidationError(
                        "document restoration is only valid for an empty page",
                        error_code="reconcile_target_invalid",
                    )
                point = 0
                end = point
                replacement = restored_content
                provenance = BlockOperationProvenance(
                    "insert_document", None, len(members)
                )
            else:
                matches = [
                    index
                    for index, entry in enumerate(block_map)
                    if entry["id"] == neighbor_id
                ]
                if len(matches) != 1:
                    raise ReadValidationError(
                        "restore_yours neighbor is no longer unique",
                        error_code="reconcile_target_invalid",
                    )
                block = fresh_blocks[matches[0]]
                if position == "replace":
                    if len(members) != 1:
                        raise ReadValidationError(
                            "multiple retired blocks cannot replace one target",
                            error_code="reconcile_target_invalid",
                        )
                    point = parsed.char_offset(block.line_start)
                    end = parsed.char_offset(block.line_end)
                    replacement = restored_content
                    provenance = BlockOperationProvenance("replace_block", neighbor_id)
                elif position == "before":
                    point = parsed.char_offset(block.line_start)
                    end = point
                    replacement = restored_content + newline
                    provenance = BlockOperationProvenance(
                        "insert_before", neighbor_id, len(members)
                    )
                elif position == "after":
                    point = parsed.char_offset(block.line_end)
                    end = point
                    prefix = (
                        ""
                        if point == 0
                        or (
                            point >= len(newline) * 2
                            and body.startswith(newline * 2, point - len(newline) * 2)
                        )
                        else newline
                    )
                    suffix = (
                        ""
                        if point == len(body) or body.startswith(newline, point)
                        else newline
                    )
                    replacement = prefix + restored_content + suffix
                    provenance = BlockOperationProvenance(
                        "insert_after", neighbor_id, len(members)
                    )
                else:
                    raise ReadValidationError(
                        "restore_yours position is invalid",
                        error_code="reconcile_target_invalid",
                    )
            restoration_edits.append(
                ResolvedEdit(
                    point,
                    end,
                    replacement,
                    "resolve_conflict",
                    carries_block_id=(neighbor_id if position == "replace" else None),
                    block_operation=provenance,
                )
            )

        combined = (*edits, *restoration_edits)
        for index, left in enumerate(combined):
            for right in combined[index + 1 :]:
                if _resolved_edit_ranges_overlap(left, right):
                    raise ReadValidationError(
                        "restore_yours overlaps another reconciliation edit",
                        error_code="reconcile_target_invalid",
                    )
        return apply_resolved_edits(body, combined), combined, None

    def _prepare_diverged_patch(
        self,
        *,
        workspace: Workspace,
        session: Session,
        page: ContextPage,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        base_version: ContextPageVersion,
        new_body: str,
        edits: tuple[ResolvedEdit, ...],
        identity_seed: str,
        prepared_block_map: PreparedBlockMap | None = None,
    ) -> _PreparedDivergedPatch:
        """Run the exact validation/normalization pipeline for branch preview/write."""
        if len(new_body) > BODY_MD_MAX_LENGTH:
            raise ReadValidationError(
                "merged context body exceeds the maximum length",
                error_code="body_length_exceeded",
            )
        pre_normalize_body = new_body
        prepared_body = self._prepare_body(
            new_body,
            workspace=workspace,
            session=session,
            page=page,
            version=base_version,
            actor_kind=actor_kind,
            actor_id=actor_id,
            actor_provenance=actor_provenance,
        )
        new_body = prepared_body.body
        outside_target_changed = _body_changed_outside_edit_ranges(
            base_version.body_md,
            pre_normalize_body,
            new_body,
            edits,
        )
        risk_resolution = self.risk_resolver.resolve(
            workspace=workspace,
            event="stale_alternative",
            signals={"review_required": True},
        )
        return _PreparedDivergedPatch(
            prepared_body=prepared_body,
            pre_normalize_body=pre_normalize_body,
            normalized=new_body != pre_normalize_body,
            outside_target_changed=outside_target_changed,
            risk_resolution=risk_resolution,
            predicted_block_map=_next_block_map_for_edits(
                base_version=base_version,
                provisional_body=pre_normalize_body,
                final_body=new_body,
                edits=edits,
                identity_seed=identity_seed,
                prepared_block_map=prepared_block_map,
            ),
            operation_receipts=_operation_receipts(
                base_version.body_md,
                edits,
                provisional_body=pre_normalize_body,
                final_body=new_body,
            ),
        )

    def _persist_diverged_branch(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        page: ContextPage,
        requested_base_version: ContextPageVersion,
        head_version: ContextPageVersion,
        operations: list[EditOperation],
        prepared: _PreparedDivergedPatch,
        edits: tuple[ResolvedEdit, ...],
        identity_seed: str,
        reason: str,
        actor_kind: str,
        actor_id: str,
        origin_chat_session_id: UUID | None,
        changed_ids: frozenset[str],
        conflicting_ids: frozenset[str],
        actual_target_ids: frozenset[str],
        supersedes_branch: ContextPageVersion | None = None,
    ) -> UpdateDraftResult:
        """Land a patch_draft push that conflicted with head as a branch,
        never touching head (design doc section 5's "diverged" row).

        Reuses this codebase's existing alternative-draft branch mechanism
        (version_status="draft", graph_status="branched") -- the same shape
        page_service's browser-editor stale-base path already produces, so
        History/the review rail need no new vocabulary. Applied against
        `requested_base_version.body_md`, NOT head's -- a diverged push must
        never silently land on content the caller never saw.
        """
        from backend.services.context_reconciliation import (
            build_reconciliation_normalization_proof,
        )

        change_set = self._create_change_set(
            session=session,
            workspace_id=workspace_id,
            reason=reason,
            trigger_type="mcp_patch_draft_diverged",
            actor_kind=actor_kind,
            actor_id=actor_id,
            risk_level=prepared.risk_resolution.risk_level,
            cite_call_skipped=True,
            origin_chat_session_id=origin_chat_session_id,
        )
        version = self._insert_version(
            session=session,
            page=page,
            parent_version_id=requested_base_version.id,
            body=prepared.prepared_body,
            actor_kind=actor_kind,
            actor_id=actor_id,
            change_set_id=change_set.id,
            version_status="draft",
            graph_status="branched",
            operation_provenance=block_operation_provenance(edits),
            identity_seed=identity_seed,
            prepared_block_map=prepared.predicted_block_map,
        )
        base_block_map = block_map_for_version(
            requested_base_version.block_map, requested_base_version.body_md
        )
        head_block_map = block_map_for_version(
            head_version.block_map, head_version.body_md
        )
        conflicts = build_conflict_details(
            conflicting_ids=conflicting_ids,
            base_body_md=requested_base_version.body_md,
            base_block_map=base_block_map,
            head_body_md=head_version.body_md,
            head_block_map=head_block_map,
            head_version_id=head_version.id,
            head_actor_kind=head_version.actor_type,
            head_actor_display_name=head_version.actor_display_name,
            head_created_at=head_version.created_at,
            operations=operations,
            authored_body_md=prepared.body,
            authored_block_map=prepared.predicted_block_map,
        )
        clean_changes = build_clean_changes(
            changed_ids=changed_ids,
            conflicting_ids=conflicting_ids,
            head_body_md=head_version.body_md,
            head_block_map=head_block_map,
        )
        original_operations = [
            operation.model_dump(mode="json") for operation in operations
        ]
        clean_operation_models = [
            operation
            for operation in operations
            if operation_target_id(operation) in actual_target_ids
            and operation_target_id(operation) not in conflicting_ids
        ]
        conflict_operation_models = [
            operation
            for operation in operations
            if operation_target_id(operation) in conflicting_ids
        ]
        active_body, _active_edits = apply_operations(
            requested_base_version.body_md,
            None,
            [*clean_operation_models, *conflict_operation_models],
            block_map=base_block_map,
        )
        normalization_proof = build_reconciliation_normalization_proof(
            prepared.pre_normalize_body, prepared.body
        )
        normalization_proof["active"] = build_reconciliation_normalization_proof(
            active_body, prepared.body
        )
        manifest = {
            "schema": _DIVERGED_MANIFEST_SCHEMA,
            "branch_version_id": str(version.id),
            "supersedes_branch_version_id": (
                str(supersedes_branch.id) if supersedes_branch is not None else None
            ),
            "base": {
                "version_id": str(requested_base_version.id),
                "content_hash": requested_base_version.content_hash,
            },
            "head": {
                "version_id": str(head_version.id),
                "content_hash": head_version.content_hash,
            },
            "branch": {
                "version_id": str(version.id),
                "content_hash": version.content_hash,
                "parent_version_id": str(version.parent_version_id),
            },
            "normalization": normalization_proof,
            "target_ids": sorted(target_ids_for_operations(operations) or frozenset()),
            "changed_since_base_ids": sorted(changed_ids),
            "conflicting_ids": sorted(conflicting_ids),
            "conflicts": [
                {
                    "base_target_id": entry["block_id"],
                    "resolution_target_id": entry.get("resolution_target_id"),
                    "kind": entry["kind"],
                }
                for entry in conflicts
            ],
            "original_operations": original_operations,
            "clean_operations": [
                operation.model_dump(mode="json")
                for operation in clean_operation_models
            ],
            "conflict_operations": [
                operation.model_dump(mode="json")
                for operation in conflict_operation_models
            ],
            "operation_receipts": [
                receipt.model_dump(mode="json")
                for receipt in prepared.operation_receipts
            ],
        }
        version.reconcile_manifest = manifest
        session.add(version)
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="context_page",
                target_id=page.id,
                operation="patch_draft_diverged_manifest",
                diff_summary=(
                    "diverged context patch; reconciliation manifest is stored "
                    "on the referenced branch version"
                ),
                diff_ref=f"context_page_version:{version.id}",
            )
        )
        review = self._create_review_item(
            session=session,
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            risk_level=prepared.risk_resolution.risk_level,
            risk_resolution=prepared.risk_resolution,
            decision_summary="diverged context patch requires conflict reconciliation",
            review_type="context_reconciliation",
        )
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="context_page.draft.diverged",
            target_kind="context_page",
            target_id=page.id,
            payload={
                "version_id": str(version.id),
                "change_set_id": str(change_set.id),
                "review_item_id": str(review.id),
                "base_version_id": str(requested_base_version.id),
                "head_version_id": str(head_version.id),
                "conflicting_ids": sorted(conflicting_ids),
                "supersedes_branch_version_id": (
                    str(supersedes_branch.id) if supersedes_branch is not None else None
                ),
            },
        )
        session.flush()
        enqueue_reconciliation_reminder(
            session=session,
            job_queue=self.job_queue,
            workspace_id=workspace_id,
            page_id=page.id,
            branch_version_id=version.id,
            review_item_id=review.id,
        )
        refresh_page_links(session, page=page, version=version)
        self._enqueue_embed(session, page=page, version=version, marker="draft")
        return UpdateDraftResult(
            draft_version_id=version.id,
            change_set_id=change_set.id,
            diff_summary=reason,
            persisted_content_hash=version.content_hash,
            persisted_character_count=len(version.body_md),
            outcome="updated_draft",
            body_changed=True,
            metadata_changed=False,
            version_status=version.version_status,
            latest_draft_version_id=page.latest_draft_version_id,
            current_published_version_id=page.current_published_version_id,
            review_item_id=review.id,
            review_status=review.status,
            editing_revision=version.editing_revision,
            normalized=prepared.normalized,
            outside_target_changed=prepared.outside_target_changed,
            risk=prepared.risk_resolution.risk_level,
            push_outcome="diverged",
            alternative_draft_id=version.id,
            changed_since_base_ids=sorted(conflicting_ids),
            conflicts=conflicts,
            clean_changes=clean_changes,
            resolution_instruction=resolution_instruction(),
            operation_receipts=prepared.operation_receipts,
        )

    def _complete_reconciled_branch(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        page: ContextPage,
        branch: ContextPageVersion,
        result: UpdateDraftResult,
        actor_kind: str,
        actor_id: str,
        completion: str = "reconciled",
    ) -> None:
        """Close the branch and its review workflow after a successful reconcile."""
        branch.graph_status = "superseded"
        # The git-merge "second parent" edge: only a clean landing actually
        # merges this branch into a version. A re-diverge instead produces a
        # NEW branch (successor_branch_version_id below) that still needs its
        # own future reconcile, so it stays unmerged for now.
        if completion == "reconciled" and result.draft_version_id is not None:
            branch.merged_into_version_id = result.draft_version_id
        session.add(branch)
        reviews: list[ReviewItem] = []
        change_set = None
        if branch.change_set_id is not None:
            reviews = list(
                session.scalars(
                    select(ReviewItem).where(
                        ReviewItem.workspace_id == workspace_id,
                        ReviewItem.change_set_id == branch.change_set_id,
                    )
                )
            )
            for review in reviews:
                if review.status in {"pending", "changes_requested"}:
                    review.status = "superseded"
                    review.claimed_by = None
                    review.claimed_at = None
                    session.add(review)
            change_set = session.scalar(
                select(ChangeSet).where(
                    ChangeSet.workspace_id == workspace_id,
                    ChangeSet.id == branch.change_set_id,
                )
            )
            if change_set is not None and change_set.status != "applied":
                change_set.status = "superseded"
                session.add(change_set)
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action=(
                "context_page.draft.reconcile_rediverged"
                if completion == "rediverged"
                else "context_page.draft.reconciled"
            ),
            target_kind="context_page",
            target_id=page.id,
            payload={
                "branch_version_id": str(branch.id),
                "completion": completion,
                "successor_branch_version_id": (
                    str(result.alternative_draft_id)
                    if completion == "rediverged"
                    and result.alternative_draft_id is not None
                    else None
                ),
                "branch_change_set_id": (
                    str(branch.change_set_id)
                    if branch.change_set_id is not None
                    else None
                ),
                "superseded_review_item_ids": [
                    str(review.id)
                    for review in reviews
                    if review.status == "superseded"
                ],
                "result_version_id": (
                    str(result.draft_version_id)
                    if result.draft_version_id is not None
                    else None
                ),
                "result_change_set_id": (
                    str(result.change_set_id)
                    if result.change_set_id is not None
                    else None
                ),
                "result_review_item_id": (
                    str(result.review_item_id)
                    if result.review_item_id is not None
                    else None
                ),
            },
        )

    def _persist_context_update(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        page: ContextPage,
        base_version: ContextPageVersion,
        restart_after_reject: bool,
        new_body: str,
        metadata_patch: PageMetadataPatch | None,
        citations: list[CitationInput],
        feedback_id: UUID | None,
        needs_review: bool,
        reason: str,
        classification: _ContextUpdateClassification,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
        origin_chat_session_id: UUID | None,
        auto_approve_format_only: bool,
        edits: tuple[ResolvedEdit, ...] | None = None,
        identity_seed: str | None = None,
        prepared_block_map: PreparedBlockMap | None = None,
        force_new_version: bool = False,
    ) -> UpdateDraftResult:
        """Persist a fully resolved Context update.

        Caller precondition: invoke within the caller's begin_nested() savepoint
        after acquiring write intent, the Context media usage lock, and the page
        row lock. This helper deliberately does not acquire locks or resolve a
        caller-specific base.

        `edits` is the resolved char ranges from context_editing.apply_operations
        -- only patch_draft has these (update_draft assigns/appends whole-body,
        so there is no per-operation range to check "outside" of). Passing it
        enables the `outside_target_changed` receipt signal below; omitting it
        leaves that field null rather than falsely claiming "nothing changed
        outside the target" for a caller that never named a target.
        """
        # D-5: BODY_MD_MAX_LENGTH is only enforced by the old request schema on
        # its input body field; append_section merges that input onto the stored
        # base, so the merged result can exceed the
        # limit even though every individual field passed validation.
        # Re-check the actual persisted body, not just its ingredients.
        if len(new_body) > BODY_MD_MAX_LENGTH:
            raise ReadValidationError(
                "merged context body exceeds the maximum length "
                f"({len(new_body)} > {BODY_MD_MAX_LENGTH} characters)",
                error_code="body_length_exceeded",
            )
        workspace = self._require_workspace(session, workspace_id)
        # F2c: Auto-fix mode normalizes the resulting body before the gate +
        # persistence (append/replace results are normalized as a whole).
        pre_normalize_body = new_body
        prepared_body = self._prepare_body(
            new_body,
            workspace=workspace,
            session=session,
            page=page,
            version=base_version,
            actor_kind=actor_kind,
            actor_id=actor_id,
            actor_provenance=actor_provenance,
        )
        new_body = prepared_body.body
        if workspace.auto_fix_enabled and pre_normalize_body:
            # I13 (re-review r2 findings 3-7): a true no-op under Auto-fix
            # (the normalized body is byte-identical to the stored base,
            # nothing else to apply) must not mint a change_set / review —
            # otherwise idempotent re-saves churn out spurious reviews.
            # Returns null change_set/review ids; submit_revision_for_review
            # (the only caller that must distinguish a no-op) checks for them.
            #
            # The compare is intentionally normalized-new vs RAW base (r3 #335
            # is NOT a bug): when the stored base is non-canonical, a real save
            # under Auto-fix SHOULD normalize it (that is the feature) — it
            # falls through here and creates exactly one auto-approved
            # format-only version. Only an already-canonical base re-saved
            # as-is is a true no-op.
            #
            # needs_review is honored (r3 #334): an explicit review request is
            # never silently dropped, even on a no-op — it falls through to
            # create a pending review.
            if (
                new_body == base_version.body_md
                and not metadata_patch
                and not citations
                and feedback_id is None
                and not needs_review
                and not force_new_version
                and not self._identical_body_requires_fork(session, base_version)
            ):
                no_effect_classification = replace(
                    classification,
                    measured_event=_measure_update_event(
                        base_version.body_md,
                        new_body,
                        outside_target_changed=False,
                    ),
                )
                _gate, risk_resolution = self._resolve_update_risk(
                    session,
                    workspace=workspace,
                    classification=no_effect_classification,
                    new_body=new_body,
                    needs_review=needs_review,
                    manual_citation=False,
                )
                return UpdateDraftResult(
                    draft_version_id=base_version.id,
                    diff_summary=reason,
                    persisted_content_hash=base_version.content_hash,
                    persisted_character_count=len(base_version.body_md),
                    outcome="no_change",
                    body_changed=False,
                    metadata_changed=False,
                    version_status=base_version.version_status,
                    latest_draft_version_id=page.latest_draft_version_id,
                    current_published_version_id=(page.current_published_version_id),
                    review_status=None,
                    editing_revision=base_version.editing_revision,
                    # The persisted body is byte-identical to base, so by
                    # definition nothing changed anywhere, in-target or out.
                    normalized=new_body != pre_normalize_body,
                    outside_target_changed=False,
                    risk=risk_resolution.risk_level,
                    operation_receipts=(
                        _operation_receipts(
                            base_version.body_md,
                            edits,
                            provisional_body=pre_normalize_body,
                            final_body=new_body,
                        )
                        if edits is not None
                        else None
                    ),
                )
        normalized = new_body != pre_normalize_body
        outside_target_changed = (
            _body_changed_outside_edit_ranges(
                base_version.body_md,
                pre_normalize_body,
                new_body,
                edits,
            )
            if edits is not None
            else None
        )
        # B-1: classify what is actually about to be persisted, not the
        # pre-auto-fix merge result -- otherwise a reformat that auto-fix
        # applies outside the caller's target is scored under the smaller,
        # stale delta and can buy a lower risk tier than the real change
        # deserves.
        classification = replace(
            classification,
            measured_event=_measure_update_event(
                base_version.body_md,
                new_body,
                outside_target_changed=bool(outside_target_changed),
            ),
        )
        citation_warnings = self._workflow_warnings(citations)
        manual_citation = any(
            citation.created_via == "manual_input" for citation in citations
        )
        gate, risk_resolution = self._resolve_update_risk(
            session,
            workspace=workspace,
            classification=classification,
            new_body=new_body,
            needs_review=needs_review,
            manual_citation=manual_citation,
        )
        # F3/DEC-8 + I3 (+ re-review finding 1): auto-approve ONLY a genuine,
        # pure body reformat — never when the gate forced review
        # (caller_needs_review / lint_error), citations or metadata changed,
        # or it is not a full-body rewrite. A format-only edit has no content
        # delta, so it is treated as LOW risk; it auto-approves only when the
        # workspace's review threshold permits low-risk auto-approval. This
        # honors strict policies (conservative review_from="low" reviews
        # everything, incl. format-only) while letting balanced/fast_moving
        # (review_from="medium") auto-approve — matching the user's "lowest
        # tier OR approved" intent. (Gating on the EVENT tier is impossible:
        # full_rewrite is "medium" under every preset, so it would either
        # never fire or, without a guard, force-approve high-risk reviews
        # under the conservative preset.)
        review_from = risk_resolution.policy_snapshot.get(
            "review_required_from_risk", "low"
        )
        format_only = (
            workspace.auto_fix_enabled
            and classification.format_only_eligible
            and not metadata_patch
            and not citations
            and bool(new_body)
            and base_version is not None
            and self._prepared_base_matches(
                base_version,
                new_body,
                workspace=workspace,
                session=session,
                page=page,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
            )
            and not gate.force_signals()
            and not risk_at_or_above("low", review_from)
        )
        change_set = self._create_change_set(
            session=session,
            workspace_id=workspace_id,
            reason=reason,
            trigger_type="mcp_update_draft",
            actor_kind=actor_kind,
            actor_id=actor_id,
            risk_level=risk_resolution.risk_level,
            cite_call_skipped=manual_citation,
            origin_chat_session_id=origin_chat_session_id,
        )
        operation_provenance = block_operation_provenance(edits)
        explicit_block_replacements = (
            None if operation_provenance else _explicit_block_replacements(edits)
        )
        if restart_after_reject:
            next_block_map = _next_block_map_for_edits(
                base_version=base_version,
                provisional_body=pre_normalize_body,
                final_body=new_body,
                edits=edits,
                identity_seed=identity_seed,
                prepared_block_map=prepared_block_map,
            )
            version = self._restart_rejected_draft(
                session=session,
                page=page,
                rejected_version=base_version,
                body=prepared_body,
                actor_kind=actor_kind,
                actor_id=actor_id,
                change_set_id=change_set.id,
                operation_provenance=operation_provenance,
                identity_seed=identity_seed,
                explicit_block_replacements=explicit_block_replacements,
                prepared_block_map=next_block_map,
            )
        else:
            version = self._version_for_update(
                session=session,
                page=page,
                base_version=base_version,
                body=prepared_body,
                actor_kind=actor_kind,
                actor_id=actor_id,
                change_set_id=change_set.id,
                require_draft=bool(metadata_patch),
                operation_provenance=operation_provenance,
                identity_seed=identity_seed,
                explicit_block_replacements=explicit_block_replacements,
                provisional_body=pre_normalize_body,
                edits=edits,
                prepared_block_map=prepared_block_map,
                force_new_version=force_new_version,
            )
        body_changed = not same_authored_markdown(version.body_md, base_version.body_md)
        metadata_changed = self._metadata_patch_changes(
            session=session,
            page=page,
            version=version,
            metadata_patch=metadata_patch,
        )
        self._record_metadata_patch(
            session=session,
            change_set_id=change_set.id,
            page=page,
            version=version,
            metadata_patch=metadata_patch,
        )
        claim = self._ensure_claim(
            session=session,
            version=version,
            body_markdown=new_body,
            has_citation=bool(citations),
        )
        self._insert_citations(
            session=session,
            workspace_id=workspace_id,
            version=version,
            claim=claim,
            citations=citations,
        )
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="context_page",
                target_id=page.id,
                operation=classification.change_operation,
                diff_summary=reason,
                diff_ref=f"context_page_version:{version.id}",
            )
        )
        review = self._create_review_item(
            session=session,
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            risk_level=risk_resolution.risk_level,
            risk_resolution=risk_resolution,
            decision_summary=(
                "context update draft requires review"
                if not citation_warnings
                else ", ".join(citation_warnings)
            ),
            # `citation_warnings` here is only ever `_workflow_warnings`'
            # output, which is `[]` or exactly `["workflow_sequence_
            # incomplete"]` (see its definition) -- never free text -- so
            # both branches have a fixed, codeable summary.
            decision_summary_code=(
                "context_update_requires_review"
                if not citation_warnings
                else "context_update_workflow_sequence_incomplete"
            ),
        )
        # Page-wide and claim-blind, unlike the reject-sibling supersession
        # in ReviewService: a new draft makes every older pending review of
        # this page stale content, so leaving one alive would let a human
        # approve text the author already replaced. A claim is cleared with
        # it, because the claimant was working a draft that no longer
        # exists. Reject's rule is narrower for a reason -- see the note at
        # its call site before making these match.
        previous_reviews = (
            session.scalars(
                select(ReviewItem)
                .join(ChangeSet, ChangeSet.id == ReviewItem.change_set_id)
                .join(
                    ChangeOperation,
                    ChangeOperation.change_set_id == ChangeSet.id,
                )
                .where(
                    ReviewItem.workspace_id == workspace_id,
                    ChangeSet.workspace_id == workspace_id,
                    ReviewItem.status == "pending",
                    ReviewItem.review_type == "context_update",
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page.id,
                    ChangeOperation.operation != "patch_draft_diverged_manifest",
                    ReviewItem.id != review.id,
                )
            )
            .unique()
            .all()
        )
        for previous_review in previous_reviews:
            supersede_review_with_lineage(
                session=session,
                audit_service=self.audit_service,
                workspace_id=workspace_id,
                review=previous_review,
                new_change_set_id=change_set.id,
                new_review_item_id=review.id,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
        # auto_approve_format_only=False (submit_revision_for_review) never
        # auto-approves: a change responding to a reviewer's request must go
        # to a human ("the AI never approves" — Risk 6). (re-review r2 #1/#2)
        if format_only and auto_approve_format_only and not restart_after_reject:
            self._auto_approve_format_only(session, workspace_id, review.id)
        if feedback_id is not None:
            feedback = get_in_workspace(
                session,
                FeedbackItem,
                workspace_id=workspace_id,
                resource_id=feedback_id,
            )
            if feedback is None:
                raise ReadNotFoundError("feedback not found")
            feedback.status = "draft_created"
            feedback.resolved_by_change_set_id = change_set.id
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="context_page.draft.update",
            target_kind="context_page",
            target_id=page.id,
            payload={
                "version_id": str(version.id),
                "change_set_id": str(change_set.id),
                "base_version_id": str(base_version.id),
            },
        )
        session.flush()
        refresh_page_links(session, page=page, version=version)
        self._enqueue_embed(session, page=page, version=version, marker="draft")
        return UpdateDraftResult(
            draft_version_id=version.id,
            change_set_id=change_set.id,
            review_item_id=review.id,
            diff_summary=reason,
            persisted_content_hash=version.content_hash,
            persisted_character_count=len(version.body_md),
            outcome=(
                "updated_draft"
                if (
                    version.id != base_version.id
                    or body_changed
                    or metadata_changed
                    or bool(citations)
                )
                else "review_only"
            ),
            body_changed=body_changed,
            metadata_changed=metadata_changed,
            version_status=version.version_status,
            latest_draft_version_id=page.latest_draft_version_id,
            current_published_version_id=page.current_published_version_id,
            review_status=review.status,
            editing_revision=version.editing_revision,
            review_reasons=[
                DraftReviewReason(code=reason.code, message=reason.message)
                for reason in gate.reasons
            ],
            normalized=normalized,
            outside_target_changed=outside_target_changed,
            risk=risk_resolution.risk_level,
            operation_receipts=(
                _operation_receipts(
                    base_version.body_md,
                    edits,
                    provisional_body=pre_normalize_body,
                    final_body=version.body_md,
                )
                if edits is not None
                else None
            ),
        )

    def publish_draft(
        self,
        session: Session,
        workspace_id: UUID,
        page_id: UUID,
        request: PublishContextPageRequest,
        actor_kind: str,
        actor_id: str,
        *,
        publish_despite_findings: bool = False,
    ) -> PublishContextPageResult:
        # `publish_despite_findings` is an argument rather than a request field
        # so that only a caller holding a reference to this service can set it.
        # The REST publish route and the review-approval path both bind
        # PublishContextPageRequest and pass nothing, so neither can waive the
        # floor; the browser publish surface passes it after the author
        # acknowledged the finding.
        # B-P1-4 Sprint_6 Fix_4: wrap publish_draft in a SAVEPOINT so a
        # failure during change_set/audit emission rolls back the
        # version_status flip + page pointer updates atomically.
        # SQLite: see write_draft. page_service.publish_draft already holds write
        # intent, but read.py and review_service reach this entry point directly,
        # and repeating the statement in one transaction is safe.
        validate_request_inputs(request)
        acquire_write_intent(session)
        self._lock_context_media_usage_intent(session, workspace_id)
        with session.begin_nested():
            page = get_page_by_id_for_update(session, page_id)
            if page is None or page.workspace_id != workspace_id:
                raise ReadNotFoundError("context page not found")
            # A locked SELECT may resolve to an identity already present in the
            # Session. Refresh while the lock is held so archive/head checks do
            # not use the pre-lock snapshot.
            session.refresh(page)
            version = get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=request.version_id,
            )
            if version is None or version.page_id != page.id:
                raise ReadNotFoundError("context page version not found")
            validate_page_markdown_input(version.body_md)
            # A draft row is mutable, so without this a concurrent session of
            # the same author can change the body between staging and
            # publishing. PublishContextPageRouteRequest (the externally
            # bound subclass) makes the field mandatory; in-process callers
            # (review approval, PageService.publish_draft) construct the base
            # PublishContextPageRequest and pass None, since they already
            # hold the version object under this write lock.
            if (
                request.expected_content_hash is not None
                and version.content_hash != request.expected_content_hash
            ):
                disclosed_id, disclosed_hash = self._disclosable_stale_base_fields(
                    version, actor_kind, actor_id
                )
                raise StaleBaseVersionError(
                    "published content hash mismatch",
                    latest_version_id=disclosed_id,
                    latest_content_hash=disclosed_hash,
                )
            # The historical resolver is permanently disabled. Give attempts
            # to re-expose those references the stable cutover conflict
            # before the general media lexical policy so REST and MCP
            # callers receive the migration-specific recovery signal.
            require_no_visible_legacy_media(
                workspace_id=workspace_id,
                body_markdown=version.body_md,
            )
            # Publication is a fresh authorization decision over the exact
            # stored bytes. Re-run lexical policy and replace Usage in this
            # same transaction so stale/missing/foreign assets cannot become
            # public merely because the request carries no body.
            self._sync_media_usage_for_publish(
                session=session,
                page=page,
                version=version,
            )
            # Human approval publishes the reviewed draft. A client can still
            # race that decision (or retry the former explicit publish step),
            # so replaying the publish of the *current* published head is a
            # successful no-op. Do not broaden this to historical published,
            # superseded, or archived versions: only pointer identity proves
            # that the requested transition has already completed.
            if (
                version.version_status == "published_snapshot"
                and version.graph_status == "linear"
                and page.current_published_version_id == version.id
                and page.current_version_id == version.id
                and page.latest_draft_version_id is None
                and page.lifecycle_status == "published"
            ):
                publish_operation = session.scalar(
                    select(ChangeOperation)
                    .join(ChangeSet, ChangeSet.id == ChangeOperation.change_set_id)
                    .where(
                        ChangeSet.workspace_id == workspace_id,
                        ChangeSet.trigger_type == "context_page_publish",
                        ChangeOperation.target_type == "context_page",
                        ChangeOperation.target_id == page.id,
                        ChangeOperation.operation == "publish",
                        ChangeOperation.diff_ref
                        == f"context_page_version:{version.id}",
                    )
                    .order_by(ChangeOperation.created_at.desc())
                )
                if publish_operation is None:
                    raise StaleContextDraftError(
                        "current published ContextPage version has no publish record"
                    )
                # Name the audit by what it recorded -- this version, published
                # by this change set -- never by being the page's newest one.
                # `created_at` is wall-clock time, which is not monotonic (NTP
                # or hypervisor time steps, a resumed VM, app nodes whose
                # clocks differ by more than the gap between two publishes), so
                # the previous publish's audit can carry the later timestamp
                # and would be returned for this version's replay.
                audit_event = session.scalar(
                    select(AuditEvent)
                    .where(
                        AuditEvent.workspace_id == workspace_id,
                        AuditEvent.action == "context_page.draft.publish",
                        AuditEvent.target_kind == "context_page",
                        AuditEvent.target_id == page.id,
                        AuditEvent.payload["version_id"].as_string() == str(version.id),
                        AuditEvent.payload["change_set_id"].as_string()
                        == str(publish_operation.change_set_id),
                    )
                    .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                    .limit(1)
                )
                if audit_event is None:
                    raise StaleContextDraftError(
                        "current published ContextPage version has no publish audit"
                    )
                previous_id = parse_uuid_or_none(
                    audit_event.payload.get("previous_published_version_id")
                )
                return PublishContextPageResult(
                    page_id=page.id,
                    published_version_id=version.id,
                    previous_published_version_id=previous_id,
                    change_set_id=publish_operation.change_set_id,
                    audit_event_id=audit_event.id,
                    findings=[
                        DraftReviewReason(
                            code=finding.rule_id,
                            message=body_relative_message(
                                finding.suggested_action or finding.rule_id,
                                version.body_md,
                            ),
                        )
                        for finding in publish_floor_findings(
                            version.body_md, registry=self.body_preparer.registry
                        )
                    ],
                )
            assert_context_publish_candidate(
                session,
                page=page,
                version=version,
                require_latest_draft=True,
            )
            if version.version_status != "draft":
                raise RejectedByPolicyError(
                    "only draft ContextPage versions can publish"
                )
            workspace = self._require_workspace(session, workspace_id)
            # The objective floor is rechecked at the one place that moves the
            # published pointer. Everything upstream can be wrong or stale -- an
            # approval recorded before the body was revised, a review row that
            # should not exist -- and none of it may put a malformed body live.
            findings = [
                DraftReviewReason(
                    code=finding.rule_id,
                    message=body_relative_message(
                        finding.suggested_action or finding.rule_id,
                        version.body_md,
                    ),
                )
                for finding in publish_floor_findings(
                    version.body_md, registry=self.body_preparer.registry
                )
            ]
            if findings and not publish_despite_findings:
                # Carry the defects out with the refusal: the author is looking
                # at the editor and needs the line to go fix, not just a "no".
                raise RejectedByPolicyError(
                    "cannot publish a draft whose body has a format error",
                    warnings=[
                        EnvelopeWarning(
                            code="lint_publish_blocked",
                            message=f"Not published. {finding.message}",
                        )
                        for finding in findings
                    ],
                )
            # A draft is publishable only when every review that still speaks
            # for it is approved (a draft with no review needs none), so review
            # state and published content never diverge and no pending
            # ReviewItem is left dangling — still eligible for the auto-approve
            # sweep — after the content is already live.
            #
            # "Still speaks for it" excludes superseded exact-version reviews.
            # Reviewed content revisions fork a successor version and supersede
            # the predecessor review, so lineage reviews cannot block the new
            # version. A version whose own reviews are all superseded is still
            # refused because it has no live review claim.
            draft_reviews = self._reviews_for_version(session, version)
            current_reviews = [
                review for review in draft_reviews if review.status != "superseded"
            ]
            if draft_reviews and not current_reviews:
                raise StaleContextDraftError(
                    "cannot publish a Context draft whose review was superseded"
                )
            blocking_reviews = [
                review for review in current_reviews if review.status != "approved"
            ]
            if blocking_reviews:
                if any(review.status == "rejected" for review in blocking_reviews):
                    raise StaleContextDraftError(
                        "cannot publish a Context draft whose review was rejected"
                    )
                raise StaleContextDraftError(
                    "Context draft review must be approved before publish"
                )
            risk_resolution = self.risk_resolver.resolve(
                workspace=workspace,
                event="context_publish",
                signals={"review_required": True},
            )

            was_archived = page.lifecycle_status == "archived"
            previous_published_version_id = apply_context_publication(
                session,
                page=page,
                version=version,
                require_latest_draft=True,
            )

            change_set = self._create_change_set(
                session=session,
                workspace_id=workspace_id,
                reason=request.reason,
                trigger_type="context_page_publish",
                actor_kind=actor_kind,
                actor_id=actor_id,
                risk_level=risk_resolution.risk_level,
                cite_call_skipped=False,
            )
            session.add(
                ChangeOperation(
                    workspace_id=change_set.workspace_id,
                    change_set_id=change_set.id,
                    target_type="context_page",
                    target_id=page.id,
                    operation="publish",
                    diff_summary=request.reason,
                    diff_ref=f"context_page_version:{version.id}",
                )
            )
            applied_metadata_patch = self._apply_pending_metadata_patches(
                session,
                page,
                version,
            )
            # Publishing an archived page (draft created before the folder
            # cascade archived it, then approved) resurrects it — revive its
            # tree node too, or it becomes a searchable-but-tree-invisible
            # ghost (B-8). Placement/title patches already trigger the same
            # call; the was_archived arm covers the no-patch publish path.
            if (
                was_archived
                or {"title", "placement_parent_node_id"} & applied_metadata_patch.keys()
            ):
                ensure_primary_tree_placement(
                    session=session,
                    workspace_id=workspace_id,
                    target_kind="context_page",
                    target_id=page.id,
                    title=page.title,
                    slug=page.slug,
                    section="context",
                    parent_id=page.placement_parent_node_id,
                    default_parent_slug="inbox",
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    can_have_children=True,
                )
            change_set.status = "applied"
            # The publishing draft's own change sets move approved -> applied:
            # their reviewed change is now the live published content.
            for review in draft_reviews:
                draft_change_set = get_in_workspace(
                    session,
                    ChangeSet,
                    workspace_id=workspace_id,
                    resource_id=review.change_set_id,
                )
                if (
                    draft_change_set is not None
                    and draft_change_set.status == "approved"
                ):
                    draft_change_set.status = "applied"
            audit_event = self.audit_service.emit(
                session=session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="context_page.draft.publish",
                target_kind="context_page",
                target_id=page.id,
                payload={
                    "version_id": str(version.id),
                    "previous_published_version_id": (
                        str(previous_published_version_id)
                        if previous_published_version_id
                        else None
                    ),
                    "change_set_id": str(change_set.id),
                },
            )
            session.flush()
            refresh_page_links(session, page=page, version=version)
            process_page_publish_maintenance(
                session,
                page=page,
                published_version_id=version.id,
                reason=request.reason,
            )
            self._enqueue_embed(session, page=page, version=version, marker="published")
            return PublishContextPageResult(
                page_id=page.id,
                published_version_id=version.id,
                previous_published_version_id=previous_published_version_id,
                change_set_id=change_set.id,
                audit_event_id=audit_event.id,
                findings=findings,
            )

    def _require_workspace(self, session: Session, workspace_id: UUID) -> Workspace:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        return workspace

    @staticmethod
    def _reviews_for_version(
        session: Session,
        version: ContextPageVersion,
    ) -> list[ReviewItem]:
        """Every ReviewItem whose change set produced ``version`` — via the
        version's own change_set_id plus any change set whose operation
        diff_ref targets the version (metadata patches, no-op revisions)."""
        change_set_ids = set(
            session.scalars(
                select(ChangeOperation.change_set_id).where(
                    ChangeOperation.diff_ref == f"context_page_version:{version.id}"
                )
            )
        )
        if version.change_set_id is not None:
            change_set_ids.add(version.change_set_id)
        if not change_set_ids:
            return []
        return list(
            session.scalars(
                select(ReviewItem).where(ReviewItem.change_set_id.in_(change_set_ids))
            )
        )

    def _get_or_create_page(
        self,
        session: Session,
        workspace_id: UUID,
        title: str,
        page_type: str,
        actor_kind: str,
        actor_id: str,
        actor_role: str,
        force_new_page: bool = False,
    ) -> tuple[ContextPage, bool]:
        if not force_new_page:
            actor = ActorContext(
                actor_kind=actor_kind,
                actor_id=actor_id,
                workspace_role=actor_role,
                role=actor_role,
                workspace_id=workspace_id,
            )
            # Detect occupancy independently of visibility so a hidden draft
            # cannot produce a duplicate "-2" page. Reuse is a separate,
            # actor-visible decision: finding a name is not authorization to
            # move the occupying page's editable head.
            match = unique_visible_page_name_match(
                session,
                workspace_id,
                name=title,
                actor=actor,
                include_unpublished=True,
            )
            if match is not None:
                visible_version_id, _is_draft = visible_context_version_id(
                    match.page, actor
                )
                if visible_version_id is None:
                    raise PageNameOccupiedError()
                return match.page, False
        base_slug = unicodedata.normalize("NFKC", slugify(title))[:64].rstrip("-")
        base_slug = base_slug or "context-page"
        slug = base_slug
        suffix = 2
        while True:
            page = get_page_by_slug_for_update(session, workspace_id, slug)
            if page is not None:
                if page.title == title and not force_new_page:
                    return page, False
                suffix_text = f"-{suffix}"
                slug = f"{base_slug[: 64 - len(suffix_text)].rstrip('-')}{suffix_text}"
                suffix += 1
                continue
            page = ContextPage(
                workspace_id=workspace_id,
                slug=slug,
                title=title,
                page_type=page_type,
                lifecycle_status="draft",
                created_by_actor_type=actor_kind,
                created_by_actor_id=parse_uuid_or_none(actor_id),
            )
            try:
                with session.begin_nested():
                    session.add(page)
                    session.flush()
            except IntegrityError:
                # Concurrent first-touch already inserted this slug
                # (uq_page_ws_slug; Postgres READ COMMITTED — FOR UPDATE
                # cannot lock an absent row). Re-read the surviving row and
                # converge on it, or keep uniquifying when its title differs.
                if page in session:
                    session.expunge(page)
                existing = get_page_by_slug_for_update(session, workspace_id, slug)
                if existing is None:
                    raise
                if existing.title == title and not force_new_page:
                    return existing, False
                suffix_text = f"-{suffix}"
                slug = f"{base_slug[: 64 - len(suffix_text)].rstrip('-')}{suffix_text}"
                suffix += 1
                continue
            return page, True

    @staticmethod
    def _is_strict(workspace: Workspace) -> bool:
        return workspace.citation_policy.get("strictness") == "strict"

    @staticmethod
    def _citation_warnings(
        workspace: Workspace,
        citations: list[CitationInput],
    ) -> list[str]:
        if citations:
            return []
        if workspace.citation_policy.get("strictness") in {"standard", "strict"}:
            return ["citation_missing"]
        return []

    @staticmethod
    def _workflow_warnings(citations: list[CitationInput]) -> list[str]:
        if any(citation.created_via == "manual_input" for citation in citations):
            return ["workflow_sequence_incomplete"]
        return []

    @staticmethod
    def _dead_end_warnings(
        session: Session,
        citations: list[CitationInput],
    ) -> list[str]:
        """Warn when a context_page citation targets a page that does not
        transitively reach a raw source — a provenance dead-end (Codex #15).

        Surfaced as a (non-blocking) warning so ungrounded context-to-context
        chains are visible to review without rejecting otherwise-valid writes.
        """
        for citation in citations:
            if citation.citation_type != "context_page":
                continue
            version_id = citation.cited_context_page_version_id
            if version_id is None:
                continue
            if not ContextWriteService._has_raw_lineage(session, version_id, set()):
                return ["provenance_dead_end"]
        return []

    @staticmethod
    def _has_raw_lineage(
        session: Session,
        version_id: UUID,
        visited: set[UUID],
    ) -> bool:
        """True if ``version_id`` transitively reaches a raw_source/derived
        artifact citation. Cycle-safe via ``visited``."""
        if version_id in visited:
            return False
        visited.add(version_id)
        claim_ids = session.scalars(
            select(ContextClaim.id).where(ContextClaim.page_version_id == version_id)
        ).all()
        target_ids = [version_id, *claim_ids]
        citations = session.scalars(
            select(ContextCitation).where(ContextCitation.target_id.in_(target_ids))
        ).all()
        for citation in citations:
            if citation.citation_type in ("raw_source", "derived_artifact"):
                return True
            cited = citation.cited_context_page_version_id
            if (
                citation.citation_type == "context_page"
                and cited is not None
                and ContextWriteService._has_raw_lineage(session, cited, visited)
            ):
                return True
        return False

    def _insert_version(
        self,
        session: Session,
        page: ContextPage,
        parent_version_id: UUID | None,
        body: PreparedBody,
        actor_kind: str,
        actor_id: str,
        change_set_id: UUID,
        version_status: str,
        graph_status: str,
        operation_provenance: Sequence[BlockOperationProvenance | Mapping[str, object]]
        | None = None,
        identity_seed: str | None = None,
        explicit_block_replacements: dict[str, str] | None = None,
        prepared_block_map: list[dict] | None = None,
    ) -> ContextPageVersion:
        try:
            return insert_version(
                session,
                page_id=page.id,
                parent_version_id=parent_version_id,
                body=body,
                actor_kind=actor_kind,
                actor_id=actor_id,
                version_status=version_status,
                graph_status=graph_status,
                change_set_id=change_set_id,
                context_media_repository=(
                    self.context_media_repository_factory(session)
                    if self.context_media_repository_factory is not None
                    else None
                ),
                operation_provenance=operation_provenance,
                identity_seed=identity_seed,
                explicit_block_replacements=explicit_block_replacements,
                prepared_block_map=prepared_block_map,
            )
        except MarkdownMediaPolicyError as exc:
            raise RejectedByPolicyError(str(exc), error_code=exc.error_code) from exc
        except ContextMediaError as exc:
            raise RejectedByPolicyError(str(exc), error_code=exc.error_code) from exc
        except ValueError as exc:
            if "dtx-media://asset/" in body.body:
                raise RejectedByPolicyError(
                    str(exc),
                    error_code=MarkdownMediaPolicyError.error_code,
                ) from exc
            raise

    def _lock_context_media_usage_intent(
        self,
        session: Session,
        workspace_id: UUID,
    ) -> None:
        """Acquire the media Usage intent before any ContextPage row lock."""

        if self.context_media_repository_factory is None:
            return
        self.context_media_repository_factory(session).lock_workspace_for_usage(
            workspace_id
        )

    @staticmethod
    def _insert_claim(
        session: Session,
        version: ContextPageVersion,
        body_markdown: str,
        has_citation: bool,
    ) -> ContextClaim:
        claim = ContextClaim(
            workspace_id=version.workspace_id,
            page_version_id=version.id,
            claim_text=first_claim_text(body_markdown),
            location_in_page={"kind": "whole_page"},
            claim_type="summary",
            confidence="unknown",
            status="active",
            is_core=not has_citation,
            created_by_actor_type=version.actor_type,
        )
        session.add(claim)
        session.flush()
        return claim

    def _ensure_claim(
        self,
        session: Session,
        version: ContextPageVersion,
        body_markdown: str,
        has_citation: bool,
    ) -> ContextClaim:
        claim = session.scalar(
            select(ContextClaim)
            .where(ContextClaim.page_version_id == version.id)
            .order_by(ContextClaim.created_at, ContextClaim.id)
        )
        if claim is not None:
            if has_citation:
                claim.is_core = False
            return claim
        return self._insert_claim(
            session=session,
            version=version,
            body_markdown=body_markdown,
            has_citation=has_citation,
        )

    def _insert_citations(
        self,
        session: Session,
        workspace_id: UUID,
        version: ContextPageVersion,
        claim: ContextClaim,
        citations: list[CitationInput],
    ) -> None:
        for citation_input in citations:
            self._validate_citation_ref(
                session,
                workspace_id,
                citation_input,
                media_installer_factory=self.media_installer_factory,
            )
            citation = ContextCitation(
                workspace_id=workspace_id,
                target_kind=citation_input.target_kind,
                target_id=claim.id
                if citation_input.target_kind == "context_claim"
                else version.id,
                target_location=citation_input.target_locator or {},
                library_item_version_id=citation_input.library_item_version_id,
                library_artifact_id=citation_input.library_artifact_id,
                cited_context_page_version_id=(
                    citation_input.cited_context_page_version_id
                ),
                location=citation_input.location or {},
                snippet=citation_input.snippet,
                citation_type=citation_input.citation_type,
                verification_status="weak",
                created_via=citation_input.created_via,
                created_by_actor_type=version.actor_type,
            )
            session.add(citation)

    @staticmethod
    def _validate_citation_ref(
        session: Session,
        workspace_id: UUID,
        citation: CitationInput,
        *,
        media_installer_factory: Callable[[Session, UUID], object] | None = None,
    ) -> None:
        if citation.citation_type == "raw_source":
            version = get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=citation.library_item_version_id,
            )
            source = (
                get_in_workspace(
                    session,
                    LibraryItem,
                    workspace_id=workspace_id,
                    resource_id=version.library_item_id,
                )
                if version
                else None
            )
            if source is None or version is None:
                raise ReadNotFoundError("source version not found")
            canonical_version = (
                get_canonical_library_version(session, source) if source else None
            )
            if canonical_version is None or canonical_version.id != version.id:
                ContextWriteService._promote_legacy_inline_citation_source(
                    session=session,
                    source=source,
                    version=version,
                    media_installer_factory=media_installer_factory,
                )
                canonical_version = get_canonical_library_version(session, source)
            if canonical_version is None or canonical_version.id != version.id:
                raise ReadNotFoundError("source version not found")
            return
        if citation.citation_type == "derived_artifact":
            artifact = get_in_workspace(
                session,
                LibraryArtifact,
                workspace_id=workspace_id,
                resource_id=citation.library_artifact_id,
            )
            version = (
                get_in_workspace(
                    session,
                    LibraryItemVersion,
                    workspace_id=workspace_id,
                    resource_id=artifact.library_item_version_id,
                )
                if artifact
                else None
            )
            source = (
                get_in_workspace(
                    session,
                    LibraryItem,
                    workspace_id=workspace_id,
                    resource_id=version.library_item_id,
                )
                if version
                else None
            )
            if (
                source is None
                or artifact is None
                or not canonical_library_artifact_belongs_to_item(
                    session,
                    item=source,
                    artifact_id=artifact.id,
                )
            ):
                raise ReadNotFoundError("source artifact not found")
            return
        cited = get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=workspace_id,
            resource_id=citation.cited_context_page_version_id,
        )
        page = (
            get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=cited.page_id,
            )
            if cited
            else None
        )
        if page is None:
            raise ReadNotFoundError("cited context page version not found")

    @staticmethod
    def _promote_legacy_inline_citation_source(
        *,
        session: Session,
        source: LibraryItem,
        version: LibraryItemVersion,
        media_installer_factory: Callable[[Session, UUID], object] | None = None,
    ) -> None:
        """Synchronously finish only the old trusted inline-text write lane.

        Legacy callers register raw text and cite the returned version in one
        transaction. The binary/converter lane remains asynchronous and must
        never gain canonical visibility through this compatibility bridge.
        """

        if (
            source.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
            or version.processing_status != "pending"
            or version.source_blob_id is not None
            or version.selected_conversion_attempt_id is not None
            or version.raw_storage_ref is None
            or not version.raw_storage_ref.startswith(INLINE_TEXT_PREFIX)
        ):
            return
        if media_installer_factory is None:
            raise RuntimeError(
                "ContextWriteService requires media_installer_factory to "
                "promote a pending inline-text Library source"
            )
        extraction_job = session.scalar(
            select(Job).where(
                Job.workspace_id == source.workspace_id,
                Job.job_type == "extraction",
                Job.idempotency_key == str(version.id),
            )
        )
        from backend.services.extraction_service import (
            ExtractionError,
            ExtractionService,
        )

        try:
            ExtractionService(
                media_installer_factory=media_installer_factory,
            ).extract_library_item_version(
                session=session,
                library_item_version_id=version.id,
                job=extraction_job,
            )
        except ExtractionError:
            # The ordinary canonical check below returns the public not-found
            # result; extraction already persisted safe failure provenance.
            return

    @staticmethod
    def _create_change_set(
        session: Session,
        workspace_id: UUID,
        reason: str,
        trigger_type: str,
        actor_kind: str,
        actor_id: str,
        risk_level: str,
        cite_call_skipped: bool,
        origin_chat_session_id: UUID | None = None,
    ) -> ChangeSet:
        change_set = ChangeSet(
            workspace_id=workspace_id,
            reason=reason,
            trigger_type=trigger_type,
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            # T15/F2: origin chat session for chat_owner review routing,
            # resolved lookup-only from the conversation token (Option A). NULL
            # is expected on the first write of a brand-new conversation (the
            # chat session row is created later by trace capture) and for
            # non-MCP/human writes with no token — chat_owner routing falls
            # back to unavailable for those changes.
            origin_chat_session_id=origin_chat_session_id,
            status="draft_created",
            risk_level=risk_level,
            cite_call_skipped=cite_call_skipped,
        )
        session.add(change_set)
        session.flush()
        return change_set

    @staticmethod
    def _create_review_item(
        session: Session,
        workspace_id: UUID,
        change_set_id: UUID,
        risk_level: str,
        decision_summary: str,
        risk_resolution: ReviewRiskResolution | None = None,
        # I18N-08a: a stable code for a fixed-copy `decision_summary`, so the
        # frontend can localize it. Leave unset for caller-supplied free text
        # (e.g. `risk_notes`) that has no fixed translation -- the frontend
        # falls back to `decision_summary` whenever this is None.
        decision_summary_code: str | None = None,
        review_type: str = "context_update",
    ) -> ReviewItem:
        review = ReviewItem(
            workspace_id=workspace_id,
            change_set_id=change_set_id,
            review_type=review_type,
            status="pending",
            risk_level=risk_level,
            **review_risk_snapshot(risk_resolution),
            decision_summary=decision_summary,
            decision_summary_code=decision_summary_code,
        )
        session.add(review)
        session.flush()
        return review

    def _auto_approve_format_only(
        self, session: Session, workspace_id: UUID, review_item_id: UUID
    ) -> None:
        # F3/DEC-8: approve a pure-formatting change through the canonical review
        # decision path (system actor) so it gets a proper ReviewDecision row,
        # audit entry, and change_set status transition — never a bare flip.
        from backend.models.workspace import default_actor_id
        from backend.schemas.context_write import ReviewDecisionRequest
        from backend.services.review_service import ReviewService

        ReviewService(
            audit_service=self.audit_service,
            context_media_repository_factory=self.context_media_repository_factory,
            body_preparer=self.body_preparer,
        ).apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=review_item_id,
            request=ReviewDecisionRequest(
                decision_type="approve",
                decision_reason="auto-fix: format-only change",
            ),
            actor_kind="system",
            actor_id=str(default_actor_id()),
            actor_user_id=None,
            actor_role="owner",
            actor_provenance="internal",
        )

    def _version_for_update(
        self,
        session: Session,
        page: ContextPage,
        base_version: ContextPageVersion,
        body: PreparedBody,
        actor_kind: str,
        actor_id: str,
        change_set_id: UUID,
        require_draft: bool = False,
        operation_provenance: Sequence[BlockOperationProvenance | Mapping[str, object]]
        | None = None,
        identity_seed: str | None = None,
        explicit_block_replacements: dict[str, str] | None = None,
        provisional_body: str | None = None,
        edits: tuple[ResolvedEdit, ...] | None = None,
        prepared_block_map: PreparedBlockMap | None = None,
        force_new_version: bool = False,
    ) -> ContextPageVersion:
        # The canonical hash ignores trailing spaces, including Markdown hard
        # breaks. Keep the established NFC/newline equivalence without losing
        # a hard-break-only save.
        same_body = same_authored_markdown(body.body, base_version.body_md)
        identical_body_requires_fork = self._identical_body_requires_fork(
            session, base_version
        )
        if (
            same_body
            and not force_new_version
            and not identical_body_requires_fork
            and (not require_draft or base_version.version_status == "draft")
        ):
            # Reached only when the caller's true no-op check upstream already
            # ruled out "nothing changed at all" -- so a same-body return here
            # means metadata/citations/feedback/needs_review changed on this
            # same row. Bump the row's own conflict-check counter.
            base_version.editing_revision += 1
            return base_version
        next_block_map = _next_block_map_for_edits(
            base_version=base_version,
            provisional_body=(
                body.body if provisional_body is None else provisional_body
            ),
            final_body=body.body,
            edits=edits,
            identity_seed=identity_seed,
            prepared_block_map=prepared_block_map,
        )
        version = self._insert_version(
            session=session,
            page=page,
            parent_version_id=base_version.id,
            body=body,
            actor_kind=actor_kind,
            actor_id=actor_id,
            change_set_id=change_set_id,
            version_status="draft",
            graph_status="linear",
            operation_provenance=operation_provenance,
            identity_seed=identity_seed,
            explicit_block_replacements=explicit_block_replacements,
            prepared_block_map=next_block_map,
        )
        page.latest_draft_version_id = version.id
        return version

    def _restart_rejected_draft(
        self,
        *,
        session: Session,
        page: ContextPage,
        rejected_version: ContextPageVersion,
        body: PreparedBody,
        actor_kind: str,
        actor_id: str,
        change_set_id: UUID,
        operation_provenance: Sequence[BlockOperationProvenance | Mapping[str, object]]
        | None = None,
        identity_seed: str | None = None,
        explicit_block_replacements: dict[str, str] | None = None,
        prepared_block_map: list[dict] | None = None,
    ) -> ContextPageVersion:
        """Create a fresh actionable draft parented by retired evidence.

        The surrounding ``update_draft`` transition creates the fresh pending
        review. The rejected version and its original decision are never
        mutated, and this path is actor-kind agnostic for browser and agent use.
        """

        version = self._insert_version(
            session=session,
            page=page,
            parent_version_id=rejected_version.id,
            body=body,
            actor_kind=actor_kind,
            actor_id=actor_id,
            change_set_id=change_set_id,
            version_status="draft",
            graph_status="linear",
            operation_provenance=operation_provenance,
            identity_seed=identity_seed,
            explicit_block_replacements=explicit_block_replacements,
            prepared_block_map=prepared_block_map,
        )
        page.latest_draft_version_id = version.id
        page.lifecycle_status = (
            "published" if page.current_published_version_id else "draft"
        )
        return version

    @staticmethod
    def _validate_media_markdown(body_markdown: str) -> tuple[UUID, ...]:
        try:
            return validate_markdown_media_policy(body_markdown)
        except MarkdownMediaPolicyError as exc:
            raise RejectedByPolicyError(str(exc), error_code=exc.error_code) from exc

    def _sync_media_usage_for_publish(
        self,
        *,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
    ) -> None:
        references = self._validate_media_markdown(version.body_md)
        if self.context_media_repository_factory is None:
            if references:
                raise RejectedByPolicyError(
                    "Context media references require an available Usage writer.",
                    error_code=MarkdownMediaPolicyError.error_code,
                )
            return
        try:
            SyncVersionUsage().execute(
                self.context_media_repository_factory(session),
                workspace_id=page.workspace_id,
                page_id=page.id,
                page_version_id=version.id,
                body_markdown=version.body_md,
            )
        except MarkdownMediaPolicyError as exc:
            raise RejectedByPolicyError(str(exc), error_code=exc.error_code) from exc
        except ContextMediaError as exc:
            raise RejectedByPolicyError(str(exc), error_code=exc.error_code) from exc

    @staticmethod
    def _write_draft_metadata_patch(
        page: ContextPage,
        request: WriteDraftRequest,
    ) -> dict:
        patch: dict = {}
        if request.page_type != page.page_type:
            patch["page_type"] = request.page_type
        if (
            request.placement_parent_node_id is not None
            and request.placement_parent_node_id != page.placement_parent_node_id
        ):
            patch["placement_parent_node_id"] = str(request.placement_parent_node_id)
        return patch

    def _record_metadata_patch(
        self,
        session: Session,
        change_set_id: UUID,
        page: ContextPage,
        version: ContextPageVersion,
        metadata_patch: dict | PageMetadataPatch | None,
    ) -> None:
        patch = (
            metadata_patch.model_dump(exclude_none=True)
            if isinstance(metadata_patch, PageMetadataPatch)
            else metadata_patch
        )
        if page.current_published_version_id is None:
            self._apply_metadata_patch(page, patch)
            return
        stage_page_metadata_patch(
            session,
            change_set_id=change_set_id,
            page=page,
            version=version,
            patch=patch,
        )

    @staticmethod
    def _metadata_patch_changes(
        *,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
        metadata_patch: PageMetadataPatch | None,
    ) -> bool:
        """Whether a metadata request changes the version's effective values.

        Published pages stage metadata beside the draft rather than mutating the
        live page. Compare against any patch already staged for the reused draft
        so a repeated same-value request reports ``review_only`` rather than a
        metadata change that did not occur.
        """

        if metadata_patch is None:
            return False
        patch = metadata_patch.model_dump(exclude_none=True)
        if not patch:
            return False
        baseline_patch = (
            pending_page_metadata_patch(
                session,
                page=page,
                version_id=version.id,
            )
            if page.current_published_version_id is not None
            and version.version_status == "draft"
            else {}
        )
        delta = page_metadata_delta(
            page,
            title=patch.get("title"),
            page_type=patch.get("page_type"),
            tags=patch.get("tags"),
            aliases=patch.get("aliases"),
            baseline_patch=baseline_patch,
        )
        if "placement_parent_node_id" in patch:
            requested_parent = patch["placement_parent_node_id"]
            effective_parent = baseline_patch.get(
                "placement_parent_node_id",
                page.placement_parent_node_id,
            )
            if str(requested_parent) != str(effective_parent):
                delta["placement_parent_node_id"] = requested_parent
        return bool(delta)

    def _apply_pending_metadata_patches(
        self,
        session: Session,
        page: ContextPage,
        version: ContextPageVersion,
    ) -> dict[str, object]:
        patch = pending_page_metadata_patch(
            session,
            page=page,
            version_id=version.id,
        )
        self._apply_metadata_patch(page, patch)
        return patch

    @staticmethod
    def _apply_metadata_patch(
        page: ContextPage,
        metadata_patch: dict | PageMetadataPatch | None,
    ) -> None:
        if not metadata_patch:
            return
        try:
            patch = (
                metadata_patch
                if isinstance(metadata_patch, PageMetadataPatch)
                else PageMetadataPatch.model_validate(metadata_patch)
            )
            validate_request_inputs(patch)
        except ValidationError as exc:
            raise InputPolicyError(
                "stored metadata violates current input policy"
            ) from exc
        fields = patch.model_fields_set
        if "title" in fields:
            page.title = patch.title
        if "page_type" in fields:
            page.page_type = patch.page_type
        if "tags" in fields:
            page.tags = patch.tags
        if "aliases" in fields:
            page.aliases = normalize_page_aliases(patch.aliases or [])
        if "placement_parent_node_id" in fields:
            page.placement_parent_node_id = patch.placement_parent_node_id

    @staticmethod
    def _latest_edit_base(
        session: Session,
        page: ContextPage,
    ) -> ContextPageVersion | None:
        latest_id = editable_context_version_id(page)
        return (
            get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=page.workspace_id,
                resource_id=latest_id,
            )
            if latest_id
            else None
        )

    @staticmethod
    def _disclosable_stale_base_fields(
        version: ContextPageVersion | None,
        actor_kind: str | None,
        actor_id: str | None,
    ) -> tuple[UUID | None, str | None]:
        """P2.2/P2.6, MCP analog of ``page_service._disclosable_version_
        fields``: ``version`` here typically comes from ``_latest_edit_base``
        (``editable_context_version_id``, a page-global pointer that -- since
        P8.1 -- can durably name a DIFFERENT author's private "editing" row).
        A ``StaleBaseVersionError`` naming that row's id/content_hash to a
        caller who is not its author is the same disclosure P2.2/P2.6 exists
        to prevent on the REST side; every raise site here must route through
        this check rather than disclosing ``version``'s fields directly.
        """

        if version is None:
            return None, None
        if version.version_status == "editing" and (
            version.created_by_actor_kind != actor_kind
            or version.created_by_actor_id != actor_id
        ):
            return None, None
        return version.id, version.content_hash

    @staticmethod
    def _resolve_conversation_read_version(
        session: Session,
        *,
        workspace_id: UUID,
        origin_chat_session_id: UUID | None,
        page_id: UUID,
    ) -> ContextPageVersion | None:
        """The conversation base ledger (design doc section 9): the version
        last observed through a versioned read or the conversation's own
        successful patch, or None when no correctness-bearing observation was
        recorded for this page."""
        if origin_chat_session_id is None:
            return None
        link = session.scalar(
            select(McpChatArtifactLink)
            .where(
                McpChatArtifactLink.workspace_id == workspace_id,
                McpChatArtifactLink.chat_session_id == origin_chat_session_id,
                McpChatArtifactLink.resource_kind
                == McpChatArtifactKind.CONTEXT_PAGE.value,
                McpChatArtifactLink.resource_id == page_id,
                McpChatArtifactLink.last_read_version_id.is_not(None),
            )
            .order_by(
                McpChatArtifactLink.last_read_at.desc(),
                McpChatArtifactLink.id.desc(),
            )
            .limit(1)
        )
        if link is None or link.last_read_version_id is None:
            return None
        return session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.workspace_id == workspace_id,
                ContextPageVersion.page_id == page_id,
                ContextPageVersion.id == link.last_read_version_id,
            )
        )

    @staticmethod
    def _resolve_push_base(
        session: Session,
        page: ContextPage,
        base: str,
    ) -> ContextPageVersion | None:
        """Resolve patch_draft's `base` (design doc section 3.2: a version
        id OR the root fold hash / content_hash) to one of this page's own
        versions, or None if it names neither."""
        version_id = parse_uuid_or_none(base)
        if version_id is not None:
            return session.scalar(
                select(ContextPageVersion).where(
                    ContextPageVersion.workspace_id == page.workspace_id,
                    ContextPageVersion.page_id == page.id,
                    ContextPageVersion.id == version_id,
                )
            )
        matches = list(
            session.scalars(
                select(ContextPageVersion).where(
                    ContextPageVersion.workspace_id == page.workspace_id,
                    ContextPageVersion.page_id == page.id,
                    ContextPageVersion.content_hash == base,
                )
            )
        )
        if len(matches) > 1:
            # A reverted body may have the same fold/content hash at multiple
            # points in the DAG while carrying different block/row identities.
            # A hash proves content, not which version's identity map the
            # caller edited, so selecting either row would be nondeterministic.
            raise ReadValidationError(
                "base hash names more than one version; use version_id",
                error_code="base_ambiguous",
            )
        return matches[0] if matches else None

    @staticmethod
    def _resolve_reconcile_operations(
        *,
        session: Session,
        page: ContextPage,
        reconcile_base_version: ContextPageVersion,
        reconciles: list[UUID],
        operations: list[PatchDraftOperation],
        excluded_clean_change_ids: list[str],
    ) -> tuple[
        ContextPageVersion,
        list[EditOperation],
        list[EditOperation],
        list[_RetiredConflictRestoration],
        dict[str, int | None],
    ]:
        """Validate reconciliation against the branch's durable manifest.

        The caller's operations must explicitly resolve every original
        conflict and no unrelated target. Non-conflicting branch operations
        are replayed in their original forms; a target changed since the
        original head fails closed instead of being skipped or overwritten.
        """
        if len(reconciles) != 1:
            raise ReadValidationError(
                "reconciles requires exactly one diverged version id",
                error_code="reconcile_target_invalid",
            )
        branch = session.scalar(
            select(ContextPageVersion)
            .where(
                ContextPageVersion.workspace_id == page.workspace_id,
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.id == reconciles[0],
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            branch is None
            or branch.page_id != page.id
            or branch.graph_status != "branched"
            or branch.version_status != "draft"
        ):
            raise ReadValidationError(
                "reconciles must name an active diverged branch of this page",
                error_code="reconcile_target_invalid",
            )
        if branch.change_set_id is None:
            raise ReadValidationError(
                "diverged branch has no durable change-set provenance",
                error_code="reconcile_target_invalid",
            )
        from backend.services.context_reconciliation import (
            load_verified_reconciliation_manifest,
        )

        verified_manifest = load_verified_reconciliation_manifest(
            session,
            page=page,
            branch=branch,
        )
        original_head = verified_manifest.original_head
        active_branch_operations = verified_manifest.active_operations

        if (
            reconcile_base_version.graph_status != "linear"
            or reconcile_base_version.version_no < original_head.version_no
        ):
            raise ReadValidationError(
                "reconcile base must be the original head or a later linear version",
                error_code="reconcile_target_invalid",
            )

        resolution_operations = [
            operation
            for operation in operations
            if isinstance(operation, ResolveConflictOperation)
        ]
        edit_operations = [
            operation
            for operation in operations
            if not isinstance(operation, ResolveConflictOperation)
        ]
        request_target_ids = target_ids_for_operations(edit_operations)
        if request_target_ids is None:
            raise ReadValidationError(
                "reconciles requires block-addressed operations",
                error_code="reconcile_target_invalid",
            )
        branch_base_map = verified_manifest.base_map
        try:
            reconcile_base_map = validated_block_map_for_version(
                reconcile_base_version.block_map,
                reconcile_base_version.body_md,
            )
        except BlockMapTransitionError as exc:
            raise ReadValidationError(
                "reconcile base block map no longer matches its body",
                error_code="reconcile_target_invalid",
            ) from exc
        active_target_ids = target_ids_for_operations(active_branch_operations)
        if active_target_ids is None:
            raise ReadValidationError(
                "diverged branch manifest contains an unaddressable operation",
                error_code="reconcile_target_invalid",
            )
        branch_map = verified_manifest.branch_map
        # Recompute the conflict set against the locked current head. A target
        # that was clean when the branch first diverged may have changed while
        # a person had the workbench open; it becomes a real conflict now
        # instead of being silently replayed or rejected as an unexplained
        # stale clean operation.
        target_sets = reconciliation_target_sets(
            operations=active_branch_operations,
            base_block_map=branch_base_map,
            authored_body_md=branch.body_md,
            authored_block_map=branch_map,
            head_block_map=reconcile_base_map,
            base_body_md=verified_manifest.base.body_md,
            head_body_md=reconcile_base_version.body_md,
        )
        dynamic_conflict_ids = set(target_sets.conflict_ids)
        already_satisfied_ids = set(target_sets.already_satisfied_ids)
        excluded_clean_ids = set(excluded_clean_change_ids)
        selectable_clean_ids = (
            set(active_target_ids) - dynamic_conflict_ids - already_satisfied_ids
        )
        invalid_excluded_ids = excluded_clean_ids - selectable_clean_ids
        if invalid_excluded_ids:
            raise ReadValidationError(
                "excluded_clean_change_ids must name only current server-reported "
                f"clean branch changes; invalid={sorted(invalid_excluded_ids)}",
                error_code="reconcile_target_invalid",
            )
        resolution_by_id = {
            operation.block: operation for operation in resolution_operations
        }
        if len(resolution_by_id) != len(resolution_operations):
            raise ReadValidationError(
                "each reconcile conflict may be resolved only once",
                error_code="reconcile_target_invalid",
            )
        unexpected_resolutions = set(resolution_by_id) - dynamic_conflict_ids
        current_target_by_base = {
            target_id: resolve_target_id_between_maps(
                branch_base_map, reconcile_base_map, target_id
            )
            for target_id in dynamic_conflict_ids
        }
        targets_resolved_by_decision = set(resolution_by_id)
        required_low_level_targets = {
            current_target
            for base_target, current_target in current_target_by_base.items()
            if base_target not in targets_resolved_by_decision
            and current_target is not None
        }
        retired_without_decision = {
            base_target
            for base_target, current_target in current_target_by_base.items()
            if base_target not in targets_resolved_by_decision
            and current_target is None
        }
        if (
            request_target_ids != required_low_level_targets
            or unexpected_resolutions
            or retired_without_decision
        ):
            missing = sorted(required_low_level_targets - request_target_ids)
            unexpected = sorted(request_target_ids - required_low_level_targets)
            raise ReadValidationError(
                "reconcile must explicitly resolve every current conflict "
                "and only those conflicts; "
                f"missing={missing}, unexpected={unexpected}, "
                f"missing_retired={sorted(retired_without_decision)}, "
                f"unexpected_decisions={sorted(unexpected_resolutions)}",
                error_code="reconcile_target_invalid",
            )
        authored_sides = authored_sides_by_target(
            operations=active_branch_operations,
            base_block_map=branch_base_map,
            authored_body_md=branch.body_md,
            authored_block_map=branch_map,
        )
        current_hashes = hash_map(reconcile_base_map)
        restorations: list[_RetiredConflictRestoration] = []
        synthesized_operations: list[EditOperation] = []
        resolution_edit_indices: dict[str, int | None] = {}
        current_ids = [entry["id"] for entry in reconcile_base_map]
        current_positions = {
            target_id: index for index, target_id in enumerate(current_ids)
        }
        base_ids = [entry["id"] for entry in branch_base_map]
        for resolution in resolution_operations:
            current_target_id = current_target_by_base[resolution.block]
            if current_target_id is not None:
                current_hash = current_hashes.get(current_target_id)
                if current_hash is None or resolution.expected != current_hash:
                    raise ReadValidationError(
                        "reconcile resolution expected hash does not match "
                        "current head",
                        error_code="reconcile_target_invalid",
                    )
            elif resolution.expected is not None:
                raise ReadValidationError(
                    "a retired reconcile target must not carry an expected hash",
                    error_code="reconcile_target_invalid",
                )
            if resolution.resolution == "keep_head":
                resolution_edit_indices[resolution.block] = None
                continue
            matching_operations = [
                operation
                for operation in active_branch_operations
                if operation_target_id(operation) == resolution.block
            ]
            if len(matching_operations) != 1:
                raise ReadValidationError(
                    "conflict decision has no unique authored operation",
                    error_code="reconcile_target_invalid",
                )
            authored_operation = matching_operations[0]
            if current_target_id is not None:
                updates: dict[str, str] = {
                    "expected": current_hashes[current_target_id]
                }
                if isinstance(authored_operation, InsertRowOperation):
                    updates["row"] = current_target_id
                else:
                    updates["block"] = current_target_id
                if resolution.resolution == "custom":
                    assert resolution.content is not None
                    custom_content = resolution.content
                    if isinstance(authored_operation, DeleteBlockOperation):
                        synthesized: EditOperation = ReplaceBlockOperation(
                            op="replace_block",
                            block=current_target_id,
                            expected=current_hashes[current_target_id],
                            content=custom_content,
                        )
                    else:
                        if not isinstance(authored_operation, InsertRowOperation):
                            fragment = custom_content.rstrip("\r\n") + "\n"
                            if len(compute_blocks(fragment)) != 1:
                                raise ReadValidationError(
                                    "custom resolution must produce exactly one block",
                                    error_code="reconcile_target_invalid",
                                )
                        updates["content"] = custom_content
                        synthesized = authored_operation.model_copy(update=updates)
                else:
                    synthesized = authored_operation.model_copy(update=updates)
                resolution_edit_indices[resolution.block] = len(edit_operations) + len(
                    synthesized_operations
                )
                synthesized_operations.append(synthesized)
                continue
            if isinstance(authored_operation, DeleteBlockOperation):
                raise ReadValidationError(
                    "a deleted retired identity has no content to restore; "
                    "use keep_head",
                    error_code="reconcile_target_invalid",
                )
            if isinstance(authored_operation, InsertRowOperation):
                raise ReadValidationError(
                    "restore_yours cannot safely reconstruct a retired table row",
                    error_code="reconcile_target_invalid",
                )
            if isinstance(authored_operation, InsertAfterOperation):
                authored_target_index = next(
                    (
                        index
                        for index, entry in enumerate(branch_map)
                        if entry["id"] == resolution.block
                    ),
                    None,
                )
                base_block_ids = {entry["id"] for entry in branch_base_map}
                inserted_count = 0
                if authored_target_index is not None:
                    for entry in branch_map[authored_target_index + 1 :]:
                        if entry["id"] in base_block_ids:
                            break
                        inserted_count += 1
                if inserted_count != 1:
                    raise ReadValidationError(
                        "restore_yours cannot prove the complete authored "
                        "insert_after block delta",
                        error_code="reconcile_target_invalid",
                    )
            side = authored_sides.get(resolution.block)
            content = (
                resolution.content
                if resolution.resolution == "custom"
                else side.get("text")
                if side is not None
                else None
            )
            if not isinstance(content, str):
                raise ReadValidationError(
                    "restore_yours has no durable authored block text",
                    error_code="reconcile_target_invalid",
                )
            try:
                base_index = base_ids.index(resolution.block)
            except ValueError as exc:
                raise ReadValidationError(
                    "restore_yours target is absent from the durable base map",
                    error_code="reconcile_target_invalid",
                ) from exc
            previous_id = next(
                (
                    target_id
                    for target_id in reversed(base_ids[:base_index])
                    if target_id in current_positions
                ),
                None,
            )
            next_id = next(
                (
                    target_id
                    for target_id in base_ids[base_index + 1 :]
                    if target_id in current_positions
                ),
                None,
            )
            candidate_id: str | None = None
            position: str
            neighbor_id: str | None
            if previous_id is not None and next_id is not None:
                previous_position = current_positions[previous_id]
                next_position = current_positions[next_id]
                if previous_position >= next_position:
                    raise ReadValidationError(
                        "restore_yours neighbors changed order",
                        error_code="reconcile_target_invalid",
                    )
                between = current_ids[previous_position + 1 : next_position]
                if len(between) > 1:
                    raise ReadValidationError(
                        "restore_yours position is ambiguous between its neighbors",
                        error_code="reconcile_target_invalid",
                    )
                candidate_id = between[0] if between else None
                position = "after"
                neighbor_id = previous_id
            elif base_index == 0 and next_id is not None:
                next_position = current_positions[next_id]
                before = current_ids[:next_position]
                if len(before) > 1:
                    raise ReadValidationError(
                        "restore_yours start position is ambiguous",
                        error_code="reconcile_target_invalid",
                    )
                candidate_id = before[0] if before else None
                position = "before"
                neighbor_id = next_id
            elif base_index == len(base_ids) - 1 and previous_id is not None:
                previous_position = current_positions[previous_id]
                after = current_ids[previous_position + 1 :]
                if len(after) > 1:
                    raise ReadValidationError(
                        "restore_yours end position is ambiguous",
                        error_code="reconcile_target_invalid",
                    )
                candidate_id = after[0] if after else None
                position = "after"
                neighbor_id = previous_id
            elif len(base_ids) == 1 and not current_ids:
                position = "document"
                neighbor_id = None
            elif len(base_ids) == 1 and len(current_ids) == 1:
                candidate_id = current_ids[0]
                position = "document"
                neighbor_id = None
            else:
                raise ReadValidationError(
                    "restore_yours has no unique surviving neighbor proof",
                    error_code="reconcile_target_invalid",
                )
            if candidate_id is not None:
                # A sole occupant proves only that there is one block in the
                # old slot. It does not prove that the block is a rewrite of
                # the retired target rather than an independent insertion.
                # No linear-version operation ledger currently supplies that
                # identity edge, so both replacement and relative insertion
                # must fail closed instead of consuming or positioning against
                # somebody else's contribution.
                raise ReadValidationError(
                    "restore_yours cannot prove that the block occupying "
                    "the retired target slot is the same logical target",
                    error_code="reconcile_target_invalid",
                )
            restorations.append(
                _RetiredConflictRestoration(
                    conflict_id=resolution.block,
                    position=position,
                    neighbor_id=neighbor_id,
                    content=content,
                    base_index=base_index,
                )
            )
            resolution_edit_indices[resolution.block] = None

        remaining_operations = [
            operation
            for operation in active_branch_operations
            if operation_target_id(operation)
            not in dynamic_conflict_ids | excluded_clean_ids | already_satisfied_ids
        ]
        carried_operations, stale_clean_ids = rebase_operations_onto_map(
            operations=remaining_operations,
            source_block_map=branch_base_map,
            target_block_map=reconcile_base_map,
        )
        if stale_clean_ids:
            raise ReadValidationError(
                "a non-conflicting branch operation can no longer be replayed "
                f"without overwriting newer content; targets={sorted(stale_clean_ids)}",
                error_code="reconcile_target_invalid",
            )
        return (
            branch,
            [*edit_operations, *synthesized_operations],
            carried_operations,
            sorted(restorations, key=lambda item: item.base_index),
            resolution_edit_indices,
        )

    @staticmethod
    def _body_for_update(
        base_version: ContextPageVersion,
        request: UpdateDraftRequest,
    ) -> str:
        if request.body_markdown is not None:
            if request.update_mode == "append_section":
                base_body = base_version.body_md.rstrip()
                return f"{base_body}\n\n{request.body_markdown}"
            return request.body_markdown
        if request.patch_description is not None:
            return (
                f"{base_version.body_md.rstrip()}\n\n"
                f"<!-- patch: {request.patch_description} -->"
            )
        return base_version.body_md


# D-3: thresholds for the update-event classifier below. This is a
# WHITELIST -- the default measured event is always "context_full_rewrite"
# (medium); a change only measures as "context_partial_update" (low) when it
# is provably small along every dimension checked.
_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_STRICT_APPEND_MAX_CHARS = 8192
_SMALL_EDIT_MAX_CHANGED_CHARS = 1024
_SMALL_EDIT_MAX_RATIO = 0.10
_SMALL_EDIT_MAX_SECTIONS = 1


def _measure_update_event(
    before_raw: str,
    after_raw: str,
    *,
    outside_target_changed: bool = False,
) -> str:
    """Classify what an update ACTUALLY did, not what `update_mode` claims.

    `_body_for_update` treats every non-append `update_mode` (including
    `replace_section`) as a whole-body assignment, so the update_mode string
    cannot distinguish a one-line tweak from a whole-document replace. This
    measures the real delta between the stored base body and the body about
    to be persisted and returns the honest event name for it —
    `context_partial_update` only for a provably small change,
    `context_full_rewrite` for everything else (see module constants above
    for the exact whitelist thresholds).

    The caller (`update_draft`) still applies the declared mode's own risk
    as a FLOOR on top of this (`review_risk_policy.resolve`'s `floor_level`)
    so a small edit submitted under `full_rewrite` can't use this measurement
    to buy a lower severity than `full_rewrite` already implied.
    """
    if before_raw == after_raw:
        return "context_partial_update"

    # Whole delta lives in the tail: the entire unchanged prefix matches, so
    # nothing outside the appended tail could have been touched.
    if after_raw.startswith(before_raw):
        appended_chars = len(after_raw) - len(before_raw)
        return (
            "context_partial_update"
            if appended_chars <= _STRICT_APPEND_MAX_CHARS
            else "context_full_rewrite"
        )

    before_n = canonicalize_markdown(before_raw)
    after_n = canonicalize_markdown(after_raw)
    if before_n == after_n:
        # Only line-ending / trailing-whitespace churn survives
        # canonicalization, so it is not a semantic-content risk by itself.
        # It still breaks patch locality when Auto-fix applied that churn
        # outside the addressed characters; the whitelist requires BOTH a
        # small semantic delta and no outside-target change.
        return (
            "context_full_rewrite"
            if outside_target_changed
            else "context_partial_update"
        )

    before_lines = before_n.splitlines(keepends=True)
    after_lines = after_n.splitlines(keepends=True)
    prefix_lines = 0
    shared_line_count = min(len(before_lines), len(after_lines))
    while (
        prefix_lines < shared_line_count
        and before_lines[prefix_lines] == after_lines[prefix_lines]
    ):
        prefix_lines += 1
    suffix_lines = 0
    while (
        suffix_lines < shared_line_count - prefix_lines
        and before_lines[len(before_lines) - suffix_lines - 1]
        == after_lines[len(after_lines) - suffix_lines - 1]
    ):
        suffix_lines += 1
    before_middle_end = len(before_lines) - suffix_lines
    after_middle_end = len(after_lines) - suffix_lines
    middle_opcodes = difflib.SequenceMatcher(
        a=before_lines[prefix_lines:before_middle_end],
        b=after_lines[prefix_lines:after_middle_end],
        # Repeated-line pages make autojunk=False quadratic under the page row
        # lock. Ignoring popular lines can only coarsen hunks here; the
        # whitelist then counts more changed characters/sections and falls
        # back to full_rewrite, never to a lower-risk classification.
        autojunk=True,
    ).get_opcodes()
    opcodes = [
        (
            tag,
            i1 + prefix_lines,
            i2 + prefix_lines,
            j1 + prefix_lines,
            j2 + prefix_lines,
        )
        for tag, i1, i2, j1, j2 in middle_opcodes
    ]

    changed_chars = _diff_hunk_chars(before_lines, after_lines, opcodes)
    ratio = changed_chars / max(1, len(before_n))
    before_sections = _section_index_by_line(before_lines)
    changed_sections = _diff_touched_sections(before_lines, opcodes, before_sections)
    structure_changed = _headings(before_n) != _headings(after_n)
    small_in_place_edit = (
        changed_chars <= _SMALL_EDIT_MAX_CHANGED_CHARS
        and ratio <= _SMALL_EDIT_MAX_RATIO
        and changed_sections <= _SMALL_EDIT_MAX_SECTIONS
        and not structure_changed
        and not outside_target_changed
    )
    return "context_partial_update" if small_in_place_edit else "context_full_rewrite"


def _body_changed_outside_edit_ranges(
    before_raw: str,
    provisional_raw: str,
    after_raw: str,
    edits: tuple[ResolvedEdit, ...],
) -> bool:
    """Whether Auto-fix changed anything patch_draft did not target.

    `provisional_raw` is the exact result of applying `edits`; `after_raw` is
    what the whole-document normalizer will persist. Comparing those two keeps
    the operation's intended replacement out of this signal. The target ranges
    themselves originate in `before_raw`, so they are mapped through every
    preceding edit into provisional coordinates before the comparison.

    Uses `edit.changed_ranges`, not `(edit.char_start, edit.char_end)`: for
    replace_text the latter is the SEARCH SCOPE apply_resolved_edits splices
    into -- up to the entire document when `at` is omitted -- not the target,
    which would pin this signal to False for the most common operation.
    `changed_ranges` is empty for every other operation, where the resolved
    range genuinely IS the target (a section, an anchor point, the document);
    fall back to it there.

    The resulting signal is also passed into `_measure_update_event`; receipt
    and risk therefore describe the same persisted body and target boundary.
    """
    if provisional_raw == after_raw:
        return False

    targets: list[tuple[int, int]] = []
    deletion_points: list[int] = []
    cumulative_delta = 0
    for edit in sorted(edits, key=lambda item: (item.char_start, item.char_end)):
        mapped_start = edit.char_start + cumulative_delta
        if edit.changed_ranges:
            # replace_text splices its whole search scope but only the concrete
            # occurrences are targets. The unchanged text between occurrences
            # is byte-identical, so replacement length minus those unchanged
            # characters yields the total post-edit target length.
            unchanged_chars = (edit.char_end - edit.char_start) - sum(
                end - start for start, end in edit.changed_ranges
            )
            replacement_target_chars = len(edit.replacement) - unchanged_chars
            target_count = len(edit.changed_ranges)
            if (
                replacement_target_chars < 0
                or replacement_target_chars % target_count != 0
            ):
                # ResolvedEdit is an internal contract and this should be
                # unreachable. Fail closed: with no approved target range, any
                # normalization is outside the target rather than silently low
                # risk.
                return True
            replacement_target_length = replacement_target_chars // target_count
            before_cursor = edit.char_start
            after_cursor = mapped_start
            for start, end in edit.changed_ranges:
                after_cursor += start - before_cursor
                target_end = after_cursor + replacement_target_length
                targets.append((after_cursor, target_end))
                if replacement_target_length == 0 and end > start:
                    deletion_points.append(after_cursor)
                after_cursor += replacement_target_length
                before_cursor = end
        elif edit.no_op:
            targets.append(
                (mapped_start, mapped_start + (edit.char_end - edit.char_start))
            )
        else:
            target_end = mapped_start + len(edit.replacement)
            targets.append((mapped_start, target_end))
            if not edit.replacement and edit.char_end > edit.char_start:
                deletion_points.append(mapped_start)

        if not edit.no_op:
            cumulative_delta += len(edit.replacement) - (
                edit.char_end - edit.char_start
            )

    targets.sort()

    deletion_whitespace_closures: dict[int, tuple[int, int]] = {}
    provisional_lines = parse_markdown(provisional_raw).lines
    provisional_line_starts = tuple(line.char_start for line in provisional_lines)

    def deletion_whitespace_closure(point: int) -> tuple[int, int]:
        """Return only whitespace whose cleanup the deletion can induce.

        A line made blank by the deletion owns its complete adjacent blank-line
        run. A line that still has content owns only horizontal whitespace
        touching the deletion point. In particular, neither form reaches into
        trailing whitespace on the preceding nonblank line.
        """
        cached = deletion_whitespace_closures.get(point)
        if cached is not None:
            return cached

        line_index = max(0, bisect_right(provisional_line_starts, point) - 1)
        line = provisional_lines[line_index]
        line_start = line.char_start
        content_end = line.content_char_end

        if not provisional_raw[line_start:content_end].strip(" \t"):
            first_blank_index = line_index
            while first_blank_index > 0:
                previous = provisional_lines[first_blank_index - 1]
                if provisional_raw[
                    previous.char_start : previous.content_char_end
                ].strip(" \t"):
                    break
                first_blank_index -= 1

            last_blank_index = line_index
            while last_blank_index + 1 < len(provisional_lines):
                following = provisional_lines[last_blank_index + 1]
                if provisional_raw[
                    following.char_start : following.content_char_end
                ].strip(" \t"):
                    break
                last_blank_index += 1
            closure = (
                provisional_lines[first_blank_index].char_start,
                provisional_lines[last_blank_index].char_end,
            )
        else:
            horizontal_start = point
            while (
                horizontal_start > line_start
                and provisional_raw[horizontal_start - 1] in " \t"
            ):
                horizontal_start -= 1
            horizontal_end = point
            while (
                horizontal_end < content_end
                and provisional_raw[horizontal_end] in " \t"
            ):
                horizontal_end += 1
            closure = (horizontal_start, horizontal_end)

        deletion_whitespace_closures[point] = closure
        return closure

    def is_targeted(start: int, end: int) -> bool:
        # A zero-width insertion is targeted when it lands anywhere on an
        # addressed range, including either boundary. That covers intended
        # prefix/suffix growth such as old_text -> old_text + suffix.
        if start == end:
            return any(
                target_start <= start <= target_end
                for target_start, target_end in targets
                if not (
                    target_start == target_end == start and start in deletion_points
                )
            )

        cursor = start
        for target_start, target_end in targets:
            if target_end <= cursor:
                continue
            if target_start > cursor:
                return False
            cursor = max(cursor, target_end)
            if cursor >= end:
                return True
        return False

    def is_deletion_whitespace_cleanup(
        start: int,
        end: int,
        after_segment: str,
    ) -> bool:
        """Attribute only whitespace cleanup touching a landed deletion.

        Deleting the last non-whitespace bytes on a line leaves a zero-width
        target in the provisional document. Auto-fix may then remove the newly
        adjacent blank line(s) or trailing space, producing a non-zero diff
        opcode that the point cannot cover. Treat that whitespace-only seam as
        the deletion's influence closure. Do not widen the target by an
        arbitrary number of characters or lines: table/heading/list changes,
        and whitespace cleanup elsewhere, must remain outside-target.
        """
        if not deletion_points:
            return False
        removed = provisional_raw[start:end]
        if removed and not removed.isspace():
            return False
        if after_segment and not after_segment.isspace():
            return False
        for point in deletion_points:
            closure_start, closure_end = deletion_whitespace_closure(point)
            if closure_start <= start and end <= closure_end:
                return True
        return False

    # The normal case is one compact normalization seam. Strip its identical
    # prefix/suffix linearly before invoking any diff heuristic; this both
    # preserves exact deletion attribution on highly repetitive pages and
    # keeps a one-line blank collapse O(document), not quadratic.
    prefix = 0
    shared_length = min(len(provisional_raw), len(after_raw))
    while prefix < shared_length and provisional_raw[prefix] == after_raw[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < shared_length - prefix
        and provisional_raw[len(provisional_raw) - suffix - 1]
        == after_raw[len(after_raw) - suffix - 1]
    ):
        suffix += 1
    provisional_diff_end = len(provisional_raw) - suffix
    after_diff_end = len(after_raw) - suffix
    after_segment = after_raw[prefix:after_diff_end]
    if is_targeted(prefix, provisional_diff_end) or is_deletion_whitespace_cleanup(
        prefix,
        provisional_diff_end,
        after_segment,
    ):
        return False
    if not any(
        target_start <= provisional_diff_end and prefix <= target_end
        for target_start, target_end in targets
    ):
        return True

    before_lines = provisional_raw.splitlines(keepends=True)
    after_lines = after_raw.splitlines(keepends=True)
    line_opcodes = difflib.SequenceMatcher(
        a=before_lines,
        b=after_lines,
        # Same repeated-line bound as _measure_update_event. A coarser hunk is
        # fail-closed for locality: it can report outside-target, never hide a
        # definite external normalization.
        autojunk=True,
    ).get_opcodes()
    before_offsets = [0]
    for line in before_lines:
        before_offsets.append(before_offsets[-1] + len(line))

    for tag, i1, i2, j1, j2 in line_opcodes:
        if tag == "equal":
            continue
        hunk_start = before_offsets[i1]
        hunk_end = before_offsets[i2]
        if is_targeted(hunk_start, hunk_end):
            continue
        if not any(
            target_start <= hunk_end and hunk_start <= target_end
            for target_start, target_end in targets
        ):
            return True

        # The changed hunk shares a line with a target but is not wholly
        # covered by it. Refine only this hunk at character granularity; doing
        # a character SequenceMatcher over an entire 1MB page would turn a
        # receipt calculation into a quadratic hot path for repetitive text.
        before_hunk = "".join(before_lines[i1:i2])
        after_hunk = "".join(after_lines[j1:j2])
        char_opcodes = difflib.SequenceMatcher(
            a=before_hunk,
            b=after_hunk,
            # For a pathological single 1MB repetitive line, disabling
            # autojunk makes SequenceMatcher quadratic. A coarser opcode is
            # fail-closed here (it can only classify more of the hunk as
            # outside-target), while ordinary short lines retain exact diffs.
            autojunk=True,
        ).get_opcodes()
        for char_tag, char_i1, char_i2, char_j1, char_j2 in char_opcodes:
            if char_tag == "equal":
                continue
            char_start = hunk_start + char_i1
            char_end = hunk_start + char_i2
            if not is_targeted(char_start, char_end) and not (
                is_deletion_whitespace_cleanup(
                    char_start,
                    char_end,
                    after_hunk[char_j1:char_j2],
                )
            ):
                return True
    return False


def _mapped_provisional_edit_spans(
    edits: tuple[ResolvedEdit, ...],
) -> dict[int, tuple[int, int]]:
    """Map each base-coordinate edit to the provisional patched body."""

    spans: dict[int, tuple[int, int]] = {}
    cumulative_delta = 0
    for index, edit in sorted(
        enumerate(edits),
        key=lambda item: (item[1].char_start, item[1].char_end, item[0]),
    ):
        mapped_start = edit.char_start + cumulative_delta
        mapped_length = (
            edit.char_end - edit.char_start if edit.no_op else len(edit.replacement)
        )
        spans[index] = (mapped_start, mapped_start + mapped_length)
        if not edit.no_op:
            cumulative_delta += len(edit.replacement) - (
                edit.char_end - edit.char_start
            )
    return spans


def _landed_new_section_hashes(
    provisional_body: str,
    final_body: str,
    edits: tuple[ResolvedEdit, ...],
) -> dict[int, str | None]:
    """Hash the actual final-outline subtree created by each section edit.

    Auto-fix is context-sensitive, and a replacement may contain nested or
    same-level headings. Hashing either the raw replacement or that substring
    normalized in isolation can therefore disagree with read_context_outline.
    The provisional and final documents preserve heading order; require a
    heading at the operation's exact section-root coordinate, carry its ordinal
    across after validating structural identity, and hash the final parser
    coordinates.
    """

    applicable = {
        index for index, edit in enumerate(edits) if edit.resolves_new_section_hash
    }
    if not applicable:
        return {}

    provisional_headings = [
        section
        for section in parse_outline(provisional_body).sections
        if section.kind == "heading"
    ]
    final_headings = (
        provisional_headings
        if provisional_body == final_body
        else [
            section
            for section in parse_outline(final_body).sections
            if section.kind == "heading"
        ]
    )
    hashes: dict[int, str | None] = {index: None for index in applicable}
    if len(provisional_headings) != len(final_headings):
        return hashes

    spans = _mapped_provisional_edit_spans(edits)
    for index in applicable:
        edit = edits[index]
        replacement_offset = edit.new_section_offset_in_replacement
        if replacement_offset is None:
            continue
        span_start, _span_end = spans[index]
        expected_start = span_start + replacement_offset
        candidates = [
            (ordinal, section)
            for ordinal, section in enumerate(provisional_headings)
            if section.char_start == expected_start
        ]
        if not candidates:
            continue
        ordinal, provisional_section = candidates[0]
        final_section = final_headings[ordinal]
        provisional_path = tuple(
            unicodedata.normalize("NFC", segment)
            for segment in provisional_section.heading_path
        )
        final_path = tuple(
            unicodedata.normalize("NFC", segment)
            for segment in final_section.heading_path
        )
        if (
            final_section.level != provisional_section.level
            or final_path != provisional_path
        ):
            continue
        hashes[index] = calculate_content_hash(
            final_body[final_section.char_start : final_section.subtree_char_end]
        )
    return hashes


def _explicit_block_replacements(
    edits: tuple[ResolvedEdit, ...] | None,
) -> dict[str, str] | None:
    """Section 2.2's operation-based carry-forward tier: a replace_block
    names its own target explicitly, so its identity should not have to
    survive context_editing's generic content-diff similarity threshold (a
    full rewrite of the target is exactly the case that threshold exists
    to catch for callers that did NOT name a target -- see block_identity.
    py's own docstring)."""
    replacements = {
        edit.carries_block_id: calculate_content_hash(
            edit.replacement
            if edit.replacement.endswith("\n")
            else edit.replacement + "\n"
        )
        for edit in (edits or ())
        if edit.carries_block_id is not None
    }
    return replacements or None


def _resolved_edit_ranges_overlap(left: ResolvedEdit, right: ResolvedEdit) -> bool:
    if left.char_start == left.char_end and right.char_start == right.char_end:
        return left.char_start == right.char_start
    if left.char_start == left.char_end:
        return right.char_start <= left.char_start <= right.char_end
    if right.char_start == right.char_end:
        return left.char_start <= right.char_start <= left.char_end
    return left.char_start < right.char_end and right.char_start < left.char_end


def _operation_receipts(
    base_body: str,
    edits: tuple[ResolvedEdit, ...],
    *,
    provisional_body: str,
    final_body: str,
) -> list[PatchOperationResolution]:
    """Per-operation patch_draft receipt detail, straight from what
    context_editing actually resolved for each operation (`edits`, in the
    same order as request.operations -- resolve_operations preserves it).

    `op`/chars_delta/resolved_line_range are derivable from every
    ResolvedEdit; the rest (resolved_heading_path, anchor_status/preview,
    old_section_hash/new-section-hash intent is only ever set on a ResolvedEdit
    by the handler
    for the operation kind it applies to, so it is null here exactly where
    it does not apply -- never because nobody populated it. New-section hashes
    are then projected from the final parsed outline so they describe the same
    subtree and hash space as read_context_outline.

    resolved_line_range uses the same `changed_ranges or (char_start, char_end)`
    fallback as outside_target_changed above: char_start:char_end is
    replace_text's SEARCH SCOPE, so reporting it as "the line range" would
    answer "the whole document" for a one-line change -- the same
    scope-vs-target confusion, one level over.
    """
    landed_hashes = _landed_new_section_hashes(provisional_body, final_body, edits)
    receipts = []
    for index, edit in enumerate(edits):
        ranges = edit.changed_ranges or ((edit.char_start, edit.char_end),)
        span_start = min(start for start, _ in ranges)
        span_end = max(end for _, end in ranges)
        line_start = base_body.count("\n", 0, span_start) + 1
        line_end = base_body.count("\n", 0, max(span_start, span_end)) + 1
        receipts.append(
            PatchOperationResolution(
                op=edit.operation,
                resolved_heading_path=(
                    list(edit.resolved_heading_path)
                    if edit.resolved_heading_path is not None
                    else None
                ),
                resolved_line_range={"start_line": line_start, "end_line": line_end},
                anchor_status=edit.anchor_status,
                anchor_preview=edit.anchor_preview,
                old_section_hash=edit.old_section_hash,
                new_section_hash=(
                    landed_hashes.get(index) if edit.resolves_new_section_hash else None
                ),
                chars_delta=(
                    0
                    if edit.no_op
                    else len(edit.replacement) - (edit.char_end - edit.char_start)
                ),
                outcome=(
                    getattr(edit, "outcome", None)
                    or ("already_landed" if edit.no_op else "applied")
                ),
            )
        )
    return receipts


def _diff_hunk_chars(
    before_lines: list[str],
    after_lines: list[str],
    opcodes: list[tuple[str, int, int, int, int]],
) -> int:
    """Sum of deleted + inserted characters across non-equal diff hunks.

    Counted per whole line within each hunk rather than per changed
    character inside a line: cheap on large bodies (avoids a char-level
    diff over up to BODY_MD_MAX_LENGTH characters) and it only ever
    overcounts, which biases toward the safer "full_rewrite" classification
    rather than under-counting a real change as small.
    """
    changed = 0
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        changed += sum(len(line) for line in before_lines[i1:i2])
        changed += sum(len(line) for line in after_lines[j1:j2])
    return changed


def _section_index_by_line(lines: list[str]) -> list[int]:
    """Section id (0 = preamble) each line belongs to, by heading boundary."""
    section_of = []
    current = 0
    for line in lines:
        if _HEADING_LINE_RE.match(line):
            current += 1
        section_of.append(current)
    return section_of


def _insertion_anchor_section(sections: list[int], i1: int) -> int:
    """Section a pure insertion (no before-lines consumed) attaches to."""
    if i1 < len(sections):
        return sections[i1]
    return sections[-1] if sections else 0


def _diff_touched_sections(
    before_lines: list[str],
    opcodes: list[tuple[str, int, int, int, int]],
    before_sections: list[int],
) -> int:
    touched: set[int] = set()
    for tag, i1, i2, _j1, _j2 in opcodes:
        if tag == "equal":
            continue
        if i1 < i2:
            touched.update(before_sections[i1:i2])
        else:
            touched.add(_insertion_anchor_section(before_sections, i1))
    return len(touched)


def _headings(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if _HEADING_LINE_RE.match(line)]


def slugify(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).lower()
    slug = re.sub(r"[\W_]+", "-", normalized).strip("-")
    return slug or "context-page"


def first_claim_text(body_markdown: str) -> str:
    for line in body_markdown.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:500]
    return "Untitled claim"
