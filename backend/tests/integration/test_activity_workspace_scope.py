from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import select

from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.activity_service import (
    ActivityService,
    _audit_target_visible,
    target_title_for_audit_event,
)
from backend.services.read_errors import ReadValidationError
from backend.services.review_service import ReviewService


def _owner(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id="activity-workspace-owner",
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
    )


def test_activity_redacts_foreign_audit_target_like_a_missing_target(session_factory):
    service = ActivityService(ReviewService())
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        foreign_workspace = Workspace(
            slug="activity-target-foreign",
            title="Activity target foreign",
            name="Activity target foreign",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_page = ContextPage(
            workspace_id=foreign_workspace.id,
            slug="foreign-activity-target",
            title="Foreign activity target title",
            page_type="overview",
            lifecycle_status="published",
        )
        session.add(foreign_page)
        session.flush()
        foreign_version = ContextPageVersion(
            workspace_id=foreign_workspace.id,
            page_id=foreign_page.id,
            version_no=1,
            body_md="# Foreign activity target",
            content_hash="foreign-activity-target-version",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="foreign-author",
            version_status="published_snapshot",
        )
        session.add(foreign_version)
        session.flush()
        foreign_page.current_version_id = foreign_version.id
        foreign_page.current_published_version_id = foreign_version.id

        foreign_event = AuditEvent(
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id="foreign-event-author",
            action="context_page.updated",
            target_kind="context_page",
            target_id=foreign_page.id,
            event_summary="Foreign activity target title",
        )
        missing_event = AuditEvent(
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id="missing-event-author",
            action="context_page.updated",
            target_kind="context_page",
            target_id=uuid4(),
            event_summary="Missing activity target title",
        )
        session.add_all([foreign_event, missing_event])
        session.flush()
        actor = _owner(workspace.id)

        assert _audit_target_visible(session, foreign_event, actor) is False
        assert _audit_target_visible(session, missing_event, actor) is False
        assert target_title_for_audit_event(session, foreign_event) is None
        assert target_title_for_audit_event(session, missing_event) is None

        activity = service.list_activity(
            session=session,
            workspace_id=workspace.id,
            period="all",
            kind="workspace",
            limit=100,
            actor_role="owner",
            actor=actor,
        )
        items = {
            item.id: item
            for item in activity.items
            if item.id in {foreign_event.id, missing_event.id}
        }

    assert set(items) == {foreign_event.id, missing_event.id}
    for item in items.values():
        assert item.title == "context_page.updated"
        assert item.target_title is None
        assert item.target is not None
        assert item.target.target_id is None


def _activity_page(session, workspace, title="Activity details"):
    page = ContextPage(
        workspace_id=workspace.id,
        slug=str(uuid4()),
        title=title,
        page_type="guide",
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace.id,
        page_id=page.id,
        version_no=1,
        body_md="# Body",
        content_hash="hash",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="activity-workspace-owner",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_version_id = page.current_published_version_id = version.id
    return page, version


def test_context_activity_details_reviews_and_stable_cursor(session_factory):
    service = ActivityService(ReviewService())
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page, version = _activity_page(session, workspace)
        other_page, other_version = _activity_page(session, workspace, "Unrelated")
        change = ChangeSet(
            workspace_id=workspace.id,
            reason="Review reason",
            trigger_type="manual_edit",
            actor_type="human",
            status="applied",
            risk_level="low",
        )
        session.add(change)
        session.flush()
        # Two matching operations must not duplicate the one review event.
        session.add_all(
            [
                ChangeOperation(
                    workspace_id=workspace.id,
                    change_set_id=change.id,
                    target_type="context_page",
                    target_id=page.id,
                    operation="update",
                    diff_ref=f"context_page_version:{version.id}",
                )
                for _ in range(2)
            ]
        )
        # Review publication also records a metadata snapshot. Its page and
        # version need the same authorization as the content operation; treating
        # this supported operation as unknown hides the entire approval event.
        session.add(
            ChangeOperation(
                workspace_id=workspace.id,
                change_set_id=change.id,
                target_type="context_page_metadata",
                target_id=page.id,
                operation="metadata_snapshot",
                diff_ref=f"context_page_version:{version.id}",
            )
        )
        review = ReviewItem(
            workspace_id=workspace.id,
            change_set_id=change.id,
            review_type="context_update",
            status="approved",
            risk_level="low",
        )
        session.add(review)
        session.flush()
        decision = ReviewDecision(
            workspace_id=workspace.id,
            review_item_id=review.id,
            decision_type="approve",
            decision_reason="Checked recovery steps",
            actor_type="human",
            resulting_review_status="approved",
            resulting_change_set_status="applied",
        )
        session.add(decision)
        session.flush()
        stamp = datetime.now(UTC) - timedelta(seconds=5)
        events = [
            AuditEvent(
                workspace_id=workspace.id,
                actor_kind="human",
                actor_id="activity-workspace-owner",
                action=action,
                target_kind=kind,
                target_id=target,
                payload=payload,
                created_at=stamp,
            )
            for action, kind, target, payload in [
                (
                    "page.create",
                    "context_page",
                    page.id,
                    {"version_id": str(version.id)},
                ),
                (
                    "page.submit_context_edit_for_review",
                    "context_page",
                    page.id,
                    {
                        "version_id": str(version.id),
                        "change_set_id": str(change.id),
                        "review_item_id": str(review.id),
                        "reason": "Submit reason",
                    },
                ),
                (
                    "review.decision",
                    "review_item",
                    review.id,
                    {"decision_id": str(decision.id), "decision_type": "approve"},
                ),
                (
                    "page.create",
                    "context_page",
                    other_page.id,
                    {"version_id": str(other_version.id)},
                ),
            ]
        ]
        session.add_all(events)
        session.flush()
        actor = _owner(workspace.id)
        args = dict(
            session=session,
            workspace_id=workspace.id,
            period="all",
            lane="history",
            page_id=page.id,
            actor=actor,
        )
        first = service.list_activity(**args, limit=2)
        assert first.next_cursor.startswith("page-activity-v2:")
        session.add(
            AuditEvent(
                workspace_id=workspace.id,
                actor_kind="human",
                actor_id=actor.actor_id,
                action="page.update",
                target_kind="context_page",
                target_id=page.id,
                created_at=datetime.now(UTC) + timedelta(seconds=1),
            )
        )
        session.flush()
        second = service.list_activity(**args, limit=2, cursor=first.next_cursor)
        items = first.items + second.items
        assert len(items) == 3 and len({item.id for item in items}) == 3
        assert {item.id for item in items} == {event.id for event in events[:3]}
        assert second.next_cursor is None
        submitted = next(
            item for item in items if item.body == "page.submit_context_edit_for_review"
        )
        approved = next(item for item in items if item.body == "review.decision")
        assert submitted.review_item_id == review.id
        assert submitted.details.reason == "Submit reason"
        assert approved.details.reason == "Checked recovery steps"
        assert approved.details.decision_type == "approve"
        assert approved.details.version.id == version.id
        assert all(item.actor.actor_id is None for item in items)
        viewer = ActorContext(
            actor_kind="human",
            actor_id=str(uuid4()),
            token_kind="session",
            workspace_id=workspace.id,
            workspace_role="viewer",
            role="viewer",
        )
        viewer_items = service.list_activity(
            **{**args, "actor": viewer}, limit=100
        ).items
        assert not any(item.body == "review.decision" for item in viewer_items)
        viewer_submit = next(
            item
            for item in viewer_items
            if item.body == "page.submit_context_edit_for_review"
        )
        assert viewer_submit.review_item_id is None
        assert viewer_submit.details.reason is None
        with pytest.raises(ReadValidationError):
            service.list_activity(
                **{**args, "page_id": other_page.id}, limit=2, cursor=first.next_cursor
            )


def test_context_activity_never_reconstructs_private_or_foreign_details(
    session_factory,
):
    service = ActivityService(ReviewService())
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page, version = _activity_page(session, workspace)
        foreign, foreign_version = _activity_page(
            session, workspace, "Hidden relationship"
        )
        old_parent = WorkspaceTreeNode(
            workspace_id=workspace.id,
            section="context",
            node_kind="folder",
            title="Before",
            slug=str(uuid4()),
            sort_key="001",
        )
        new_parent = WorkspaceTreeNode(
            workspace_id=workspace.id,
            section="context",
            node_kind="folder",
            title="After",
            slug=str(uuid4()),
            sort_key="002",
        )
        session.add_all([old_parent, new_parent])
        session.flush()
        node = WorkspaceTreeNode(
            workspace_id=workspace.id,
            section="context",
            node_kind="context_page",
            target_kind="context_page",
            target_id=page.id,
            title=page.title,
            parent_id=new_parent.id,
            sort_key="001",
        )
        session.add(node)
        session.flush()
        payloads = [
            {
                "version_id": str(version.id),
                "editing_revision": 7,
                "was_private_edit": True,
                "reason": "private historical reason",
            },
            {"version_id": str(foreign_version.id), "reason": "foreign version reason"},
            {
                "previous_parent_id": str(old_parent.id),
                "new_parent_id": str(new_parent.id),
                "previous_parent_path": [
                    {"id": str(old_parent.id), "title": "Recorded before"}
                ],
                "new_parent_path": [
                    {"id": str(new_parent.id), "title": "Recorded after"}
                ],
                "reason": "Organize docs",
            },
        ]
        events = [
            AuditEvent(
                workspace_id=workspace.id,
                actor_kind="human",
                actor_id="different-author",
                action=action,
                target_kind=kind,
                target_id=target,
                payload=payload,
            )
            for action, kind, target, payload in [
                ("page.autosave.draft", "context_page", page.id, payloads[0]),
                ("page.update", "context_page", page.id, payloads[1]),
                ("tree.node.move", "workspace_tree_node", node.id, payloads[2]),
            ]
        ]
        events[0].event_summary = "Private summary from a former edit"
        session.add_all(events)
        session.flush()
        result = service.list_activity(
            session=session,
            workspace_id=workspace.id,
            period="all",
            lane="history",
            page_id=page.id,
            actor=_owner(workspace.id),
        )
        by_action = {i.body: i for i in result.items}
        private = by_action["page.autosave.draft"].details
        assert by_action["page.autosave.draft"].title == "page.autosave.draft"
        assert (
            private.version is None
            and private.reason is None
            and private.editing_revision is None
        )
        invalid = by_action["page.update"].details
        assert invalid.version is None and invalid.reason is None
        moved = by_action["tree.node.move"].details
        assert moved.move.before.path == ["Recorded before"]
        assert moved.move.after.path == ["Recorded after"]
        assert moved.move.before.provenance == "recorded"
        assert [event.payload for event in events] == payloads
        workspace_id, page_id = workspace.id, page.id

        def count_queries():
            session.expire_all()
            queries = []

            def observe(_conn, _cursor, statement, _params, _context, _many):
                if statement.lstrip().upper().startswith("SELECT"):
                    queries.append(statement)

            engine = session.get_bind()
            sql_event.listen(engine, "before_cursor_execute", observe)
            try:
                service.list_activity(
                    session=session,
                    workspace_id=workspace_id,
                    page_id=page_id,
                    actor=_owner(workspace_id),
                    period="all",
                    limit=100,
                )
            finally:
                sql_event.remove(engine, "before_cursor_execute", observe)
            return len(queries)

        small = count_queries()
        session.add_all(
            [
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_kind="human",
                    actor_id="different-author",
                    action="page.update",
                    target_kind="context_page",
                    target_id=page_id,
                )
                for _ in range(20)
            ]
        )
        session.flush()
        assert count_queries() == small
