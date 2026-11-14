"""The shared draft list must distinguish never-reviewed rows from history."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.context_drafts import list_context_drafts, project_context_draft
from backend.services.read_errors import ReadValidationError


def test_saved_draft_projection_pages_reviewless_and_open_rows(session_factory):
    """One DB traversal covers list eligibility, keyset continuation and facts."""
    instant = datetime(2000, 1, 1, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page = ContextPage(
            workspace_id=workspace.id,
            slug="draft-list",
            title="Draft list",
            page_type="overview",
            lifecycle_status="draft",
        )
        session.add(page)
        session.flush()

        def version(number, status="draft", *, graph="linear", parent=None):
            row = ContextPageVersion(
                id=UUID(int=number),
                workspace_id=workspace.id,
                page_id=page.id,
                version_no=number,
                body_md=f"Draft {number}",
                content_hash=f"draft-{number}",
                actor_type="human",
                actor_display_name="Author",
                created_by_actor_kind="human",
                created_by_actor_id="author",
                version_status=status,
                graph_status=graph,
                parent_version_id=parent,
                last_saved_at=instant + timedelta(minutes=number),
            )
            session.add(row)
            session.flush()
            return row

        earlier = version(1, "superseded_snapshot")
        base = version(2, "published_snapshot", parent=earlier.id)
        reviewless = version(3, graph="discarded", parent=base.id)
        tie = version(4, parent=earlier.id)
        tie.last_saved_at = reviewless.last_saved_at
        editing = version(5, "editing", parent=base.id)
        pending = version(6, parent=base.id)
        rejected = version(7, parent=base.id)
        # Legacy/corrupt ancestry must produce unknown rather than looping.
        rejected.parent_version_id = rejected.id
        superseded = version(8, parent=base.id)
        discarded = version(9, "discarded", parent=base.id)
        for row, status in (
            (pending, "pending"),
            (pending, "superseded"),
            (rejected, "rejected"),
            (superseded, "superseded"),
        ):
            change = ChangeSet(
                workspace_id=workspace.id,
                trigger_type="context_page_update",
                actor_type="human",
                status="pending_review",
            )
            session.add(change)
            session.flush()
            session.add_all(
                [
                    ChangeOperation(
                        workspace_id=workspace.id,
                        change_set_id=change.id,
                        target_type="context_page",
                        target_id=page.id,
                        operation="update",
                        diff_ref=f"context_page_version:{row.id}",
                    ),
                    ReviewItem(
                        workspace_id=workspace.id,
                        change_set_id=change.id,
                        review_type="context_update",
                        status=status,
                    ),
                ]
            )
        page.current_published_version_id = base.id
        page.current_version_id = pending.id
        page.latest_draft_version_id = pending.id
        session.flush()
        assert rejected.last_saved_at == instant + timedelta(minutes=7)
        workspace_id, page_id = workspace.id, page.id
        expected = [pending.id, editing.id, reviewless.id, tie.id]
        rejected_id, discarded_id = rejected.id, discarded.id
        latest_closed_id = superseded.id

    actor = ActorContext(
        actor_kind="human",
        actor_id="author",
        token_kind="session",
        scopes=("*",),
        workspace_id=workspace_id,
        workspace_role="editor",
        role="editor",
    )
    with session_factory() as session:
        first = list_context_drafts(
            session,
            workspace_id=workspace_id,
            page_id=page_id,
            actor=actor,
            limit=2,
        )
        second = list_context_drafts(
            session,
            workspace_id=workspace_id,
            page_id=page_id,
            actor=actor,
            limit=2,
            cursor=first.next_cursor,
        )
        assert first.total == second.total == 4
        assert [item.version_id for item in first.items + second.items] == expected
        assert first.items[0].state == "reviewing"
        assert first.items[0].review.status == "pending"
        assert first.items[1].state == "writing"
        assert first.items[1].is_editing and first.items[1].is_mine
        assert first.items[0].is_behind is False
        assert second.items[1].is_behind is True
        assert first.next_cursor and second.next_cursor is None
        viewer = replace(actor, workspace_role="viewer", role="viewer")
        assert list_context_drafts(
            session,
            workspace_id=workspace_id,
            page_id=page_id,
            actor=viewer,
        ).model_dump() == {"total": 0, "items": [], "next_cursor": None}
        page = session.get(ContextPage, page_id)
        closed = project_context_draft(
            session,
            page,
            session.get(ContextPageVersion, rejected_id),
            actor,
        )
        assert closed.review.status == "rejected"
        assert closed.is_behind is None
        assert (
            project_context_draft(
                session,
                page,
                session.get(ContextPageVersion, discarded_id),
                actor,
            )
            is None
        )
        with pytest.raises(ReadValidationError):
            list_context_drafts(
                session,
                workspace_id=workspace_id,
                page_id=UUID(int=100),
                actor=actor,
                cursor=first.next_cursor,
            )

        # Never-published default reads may select closed review evidence;
        # closed rows remain absent from the open-draft count.
        from backend.repositories.context_visibility import visible_context_version_id

        page.current_published_version_id = None
        page.current_version_id = None
        page.latest_draft_version_id = None
        session.flush()
        assert visible_context_version_id(page, actor)[0] == latest_closed_id
        assert (
            list_context_drafts(
                session,
                workspace_id=workspace_id,
                page_id=page_id,
                actor=actor,
            ).total
            == 4
        )

        # Resolve the remaining review and cover the reviewless saves with it;
        # the open list empties without erasing closed draft evidence.
        pending_review = session.scalar(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.status == "pending",
            )
        )
        pending_review.status = "rejected"
        for version_id in expected[2:]:
            session.add(
                ChangeOperation(
                    workspace_id=workspace_id,
                    change_set_id=pending_review.change_set_id,
                    target_type="context_page",
                    target_id=page_id,
                    operation="update",
                    diff_ref=f"context_page_version:{version_id}",
                )
            )
        session.get(ContextPageVersion, expected[1]).version_status = "discarded"
        session.flush()
        assert (
            list_context_drafts(
                session,
                workspace_id=workspace_id,
                page_id=page_id,
                actor=actor,
            ).total
            == 0
        )
        assert visible_context_version_id(page, actor)[0] == latest_closed_id
