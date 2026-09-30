from uuid import uuid4

from sqlalchemy import select

from backend.models.audit import AuditEvent
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.activity_service import (
    ActivityService,
    _audit_target_visible,
    target_title_for_audit_event,
)
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
