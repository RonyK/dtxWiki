"""Read-only, actor-aware document and exact-version projections for Review.

An operation ID is not necessarily a document ID. This seam resolves ownership
before exposing a reader destination, and authorizes the snapshot separately
from the ordinary reader's current head.
"""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.change import ChangeOperation
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.feedback import FeedbackItem
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.tree import WorkspaceTreeNode
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import actor_can_read_drafts
from backend.repositories.workspace_scoped import get_in_workspace, select_in_workspace
from backend.schemas.context_write import ReviewContentTarget, ReviewDocumentTarget
from backend.services.actor_visible_read import (
    actor_can_read_agent_page,
    actor_can_read_feedback,
    agent_page_visible,
    agent_version_is_visible,
    context_page_visible,
    context_version_is_visible,
    library_item_visible,
)
from backend.services.context_lifecycle import is_publishable_draft
from backend.services.workspace_urls import (
    agent_page_url,
    library_artifact_url,
    library_item_url,
)
from backend.utils.page_frontmatter import parse_page_frontmatter
from backend.utils.uuid import parse_uuid_or_none


def operation_version_id(operation: ChangeOperation, kind: str) -> UUID | None:
    if operation.target_type == f"{kind}_version":
        return operation.target_id
    prefix = f"{kind}_version:"
    if operation.diff_ref and operation.diff_ref.startswith(prefix):
        return parse_uuid_or_none(operation.diff_ref[len(prefix) :])
    if (
        kind == "context_page"
        and operation.operation == "inline_comment"
        and operation.diff_ref
    ):
        try:
            anchor = json.loads(operation.diff_ref)
        except (ValueError, TypeError):
            return None
        if (
            isinstance(anchor, dict)
            and anchor.get("kind") == "page_inline_comment_anchor"
        ):
            return parse_uuid_or_none(anchor.get("version_id"))
    return None


def project_review_target(
    session: Session,
    operation: ChangeOperation,
    workspace_id: UUID,
    actor: ActorContext | None,
    *,
    allow_archived_context: bool = False,
) -> tuple[ReviewDocumentTarget | None, ReviewContentTarget]:
    if operation.target_type in {"context_page", "context_page_version"}:
        return _context_target(
            session,
            operation,
            workspace_id,
            actor,
            allow_archived=allow_archived_context,
        )
    if operation.target_type in {"agent_page", "agent_page_version"}:
        return _agent_target(session, operation, workspace_id, actor)
    if operation.target_type in {
        "library_item",
        "library_item_version",
        "library_artifact",
    }:
        return _library_target(session, operation, workspace_id, actor)
    if operation.target_type == "workspace_tree_node":
        return _context_placement_target(session, operation, workspace_id, actor)
    if operation.target_type == "feedback":
        # A feedback-only capture has no document; its summary is feedback
        # content, readable exactly where feedback itself is.
        feedback = get_in_workspace(
            session, FeedbackItem,
            workspace_id=workspace_id, resource_id=operation.target_id,
        )
        if feedback is None or not actor_can_read_feedback(actor):
            return None, ReviewContentTarget(
                mode="unavailable", unavailable_reason="target_unavailable"
            )
        return None, ReviewContentTarget(mode="metadata_only")
    return None, ReviewContentTarget(
        mode="unsupported",
        unavailable_reason="unsupported_target",
    )


def _context_placement_target(
    session: Session,
    operation: ChangeOperation,
    workspace_id: UUID,
    actor: ActorContext | None,
) -> tuple[ReviewDocumentTarget, ReviewContentTarget]:
    """Normalize an ingress placement only through its authorized exact page.

    The tree node's raw ID is not a document ID. A visible placement alone
    does not establish what content this change set reviewed: its associated
    Context ingress operation must bind the same page and a readable version.
    """
    from backend.services.tree_read import TreeReadService

    node = get_in_workspace(
        session, WorkspaceTreeNode,
        workspace_id=workspace_id, resource_id=operation.target_id,
    )
    if (
        actor is None
        or operation.operation != "tree_place"
        or node is None
        or node.section != "context"
        or node.node_kind != "page_ref"
        or node.target_kind != "context_page"
        or node.target_id is None
        or not TreeReadService.node_visible_to_actor(session, node, actor)
    ):
        return _unavailable("context")
    associated = session.scalars(select_in_workspace(
        ChangeOperation,
        workspace_id=workspace_id,
        criteria=[
            ChangeOperation.change_set_id == operation.change_set_id,
            ChangeOperation.target_type == "context_page",
            ChangeOperation.target_id == node.target_id,
            ChangeOperation.operation == "context_ingress_accept",
        ],
    )).all()
    if len(associated) != 1:
        return _unavailable("context")
    document, candidate = _context_target(
        session, associated[0], workspace_id, actor, allow_archived=False,
    )
    if (
        document.document_id != node.target_id
        or candidate.mode != "context_version"
        or not candidate.can_read
        or candidate.candidate_version_id is None
    ):
        return _unavailable("context")
    # This operation describes placement metadata. Binding its verified
    # candidate identifies the companion evidence without claiming that tree
    # metadata itself supplies an original body or a text comparison.
    return document, ReviewContentTarget(
        mode="metadata_only",
        candidate_version_id=candidate.candidate_version_id,
        candidate_version_no=candidate.candidate_version_no,
        is_current_candidate=candidate.is_current_candidate,
    )


def _unavailable(kind: str) -> tuple[ReviewDocumentTarget, ReviewContentTarget]:
    # A missing row, foreign workspace and unreadable target are indistinguishable.
    return (
        ReviewDocumentTarget(
            document_kind=kind, unavailable_reason="target_unavailable"
        ),
        ReviewContentTarget(
            mode="unavailable", unavailable_reason="target_unavailable"
        ),
    )


def _context_target(
    session: Session,
    operation: ChangeOperation,
    workspace_id: UUID,
    actor: ActorContext | None,
    *,
    allow_archived: bool,
) -> tuple[ReviewDocumentTarget, ReviewContentTarget]:
    version_id = operation_version_id(operation, "context_page")
    version = (
        get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=workspace_id,
            resource_id=version_id,
        )
        if version_id is not None
        else None
    )
    page_id = (
        operation.target_id
        if operation.target_type == "context_page"
        else version.page_id
        if version is not None
        else None
    )
    page = (
        get_in_workspace(
            session,
            ContextPage,
            workspace_id=workspace_id,
            resource_id=page_id,
        )
        if page_id is not None
        else None
    )
    if version is not None and (page is None or version.page_id != page.id):
        version = None
    ordinary = actor is not None and context_page_visible(
        page,
        actor,
        workspace_id=workspace_id,
    )
    evidence_visible = actor is not None and (
        context_page_visible(
            page,
            actor,
            workspace_id=workspace_id,
            allow_archived=allow_archived,
        )
        or (
            page is not None
            and page.lifecycle_status != "archived"
            and actor_can_read_drafts(actor)
            and version is not None
            and context_version_is_visible(version, actor)
            and page.current_version_id is None
            and page.latest_draft_version_id is None
            and page.current_published_version_id is None
        )
    )
    if page is None or not evidence_visible:
        return _unavailable("context")
    document = ReviewDocumentTarget(
        document_kind="context",
        document_id=page.id,
        title=page.title,
        slug=page.slug,
        reader_href=f"/workspaces/{workspace_id}/context/pages/{page.id}"
        if ordinary
        else None,
        can_open=ordinary,
        unavailable_reason=None if ordinary else "reader_unavailable",
    )
    if operation.operation == "source_deleted_notice":
        # This acknowledges an event about the Context page; it never reviews
        # or publishes a guessed Library source/version from the JSON notice.
        return document, ReviewContentTarget(mode="metadata_only")
    if version is None or not context_version_is_visible(version, actor):
        return document, ReviewContentTarget(
            mode="unavailable",
            unavailable_reason="candidate_unavailable",
        )
    content = ReviewContentTarget(
        mode="metadata_only"
        if operation.operation == "inline_comment"
        else "context_version",
        candidate_version_id=version.id,
        candidate_version_no=version.version_no,
        can_read=True,
        # Approving this review would publish exactly this version.
        is_current_candidate=is_publishable_draft(session, page, version),
    )
    # Approving the current candidate publishes it over the live version (or
    # makes the first one), so that is its baseline even when its parent is an
    # unapproved draft of a revision chain. Any other version shows its parent.
    baseline_id = (
        page.current_published_version_id
        if content.is_current_candidate
        else version.parent_version_id
    )
    baseline = (
        get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=workspace_id,
            resource_id=baseline_id,
        )
        if baseline_id is not None
        else None
    )
    if baseline_id is None:
        content.baseline_kind = "empty"
        content.can_diff = True
    elif (
        baseline is not None
        and baseline.page_id == page.id
        and context_version_is_visible(baseline, actor)
    ):
        content.baseline_kind = (
            "published"
            if baseline.id == page.current_published_version_id
            else "parent"
        )
        content.baseline_version_id = baseline.id
        content.baseline_version_no = baseline.version_no
        content.can_diff = True
    else:
        content.unavailable_reason = "baseline_unavailable"
    if content.is_current_candidate and version.parent_version_id not in (
        None,
        page.current_published_version_id,
    ):
        # Written on an earlier unpublished draft: comparing with it shows
        # this draft's own change, which is what rejecting it would reject.
        parent = get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=workspace_id,
            resource_id=version.parent_version_id,
        )
        if (
            parent is not None
            and parent.page_id == page.id
            and parent.version_status == "draft"
            and context_version_is_visible(parent, actor)
        ):
            content.draft_parent_version_id = parent.id
            content.draft_parent_version_no = parent.version_no
    if content.can_diff:
        # Same lexical body extraction and input ceiling as PageService.diff_versions.
        from backend.services.page_service import PAGE_DIFF_MAX_INPUT_CHARS

        old_body = (
            "" if baseline is None else parse_page_frontmatter(baseline.body_md).body
        )
        new_body = parse_page_frontmatter(version.body_md).body
        if len(old_body) + len(new_body) > PAGE_DIFF_MAX_INPUT_CHARS:
            content.can_diff = False
            content.unavailable_reason = "diff_too_large"
    return document, content


def _agent_target(
    session: Session,
    operation: ChangeOperation,
    workspace_id: UUID,
    actor: ActorContext | None,
) -> tuple[ReviewDocumentTarget, ReviewContentTarget]:
    version_id = operation_version_id(operation, "agent_page")
    version = (
        get_in_workspace(
            session,
            AgentPageVersion,
            workspace_id=workspace_id,
            resource_id=version_id,
        )
        if version_id is not None
        else None
    )
    page_id = (
        operation.target_id
        if operation.target_type == "agent_page"
        else version.agent_page_id
        if version is not None
        else None
    )
    page = (
        get_in_workspace(
            session,
            AgentPage,
            workspace_id=workspace_id,
            resource_id=page_id,
        )
        if page_id is not None
        else None
    )
    if (
        actor is None
        or page is None
        or page.lifecycle_status == "archived"
        or not actor_can_read_agent_page(page, actor)
    ):
        return _unavailable("agent")
    ordinary = agent_page_visible(page, actor, workspace_id=workspace_id)
    ordinary = ordinary and (
        page.current_active_version_id is not None
        or (page.latest_draft_version_id is not None and actor_can_read_drafts(actor))
    )
    readable_version = (
        version is not None
        and version.agent_page_id == page.id
        and agent_version_is_visible(page, version, actor)
    )
    if not ordinary and not readable_version:
        return _unavailable("agent")
    return ReviewDocumentTarget(
        document_kind="agent",
        document_id=page.id,
        title=page.title,
        slug=page.slug,
        reader_href=agent_page_url(workspace_id, page.id) if ordinary else None,
        can_open=ordinary,
        unavailable_reason=None if ordinary else "reader_unavailable",
    ), ReviewContentTarget(
        mode="unsupported",
        unavailable_reason="unsupported_content",
        candidate_version_id=version.id if readable_version else None,
        candidate_version_no=version.version_no if readable_version else None,
        is_current_candidate=(page.latest_draft_version_id == version.id)
        if readable_version
        else None,
    )


def _library_target(
    session: Session,
    operation: ChangeOperation,
    workspace_id: UUID,
    actor: ActorContext | None,
) -> tuple[ReviewDocumentTarget, ReviewContentTarget]:
    version = None
    artifact_id = None
    item_id = operation.target_id if operation.target_type == "library_item" else None
    if operation.target_type == "library_artifact":
        artifact = get_in_workspace(
            session,
            LibraryArtifact,
            workspace_id=workspace_id,
            resource_id=operation.target_id,
        )
        artifact_id = artifact.id if artifact is not None else None
        version_id = artifact.library_item_version_id if artifact is not None else None
    else:
        version_id = (
            operation.target_id
            if operation.target_type == "library_item_version"
            else None
        )
    if version_id is not None:
        version = get_in_workspace(
            session,
            LibraryItemVersion,
            workspace_id=workspace_id,
            resource_id=version_id,
        )
        item_id = version.library_item_id if version is not None else None
    item = (
        get_in_workspace(
            session,
            LibraryItem,
            workspace_id=workspace_id,
            resource_id=item_id,
        )
        if item_id is not None
        else None
    )
    if actor is None or not library_item_visible(
        item, actor, workspace_id=workspace_id
    ):
        return _unavailable("library")
    # Historical/processing Library versions do not inherit the ready-source read grant.
    readable_version = version is not None and library_item_visible(
        item,
        actor,
        workspace_id=workspace_id,
        version=version,
    )
    return ReviewDocumentTarget(
        document_kind="library",
        document_id=item.id,
        title=item.title,
        reader_href=(
            library_artifact_url(workspace_id, artifact_id)
            if artifact_id is not None
            else library_item_url(workspace_id, item.id)
        ),
        can_open=True,
    ), ReviewContentTarget(
        mode="unsupported",
        unavailable_reason="unsupported_content",
        candidate_version_id=version.id if readable_version else None,
        candidate_version_no=version.version_no if readable_version else None,
    )
