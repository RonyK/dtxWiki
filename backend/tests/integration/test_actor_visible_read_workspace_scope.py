from uuid import uuid4

from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.actor_visible_read import (
    context_page_id_visible,
    resolve_visible_context_version,
)


def _owner(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id="actor-visible-owner",
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
    )


def test_context_id_and_version_resolution_treat_foreign_ids_as_missing(
    session_factory,
):
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        foreign_workspace = Workspace(
            slug="actor-visible-read-foreign",
            title="Actor visible read foreign",
            name="Actor visible read foreign",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_page = ContextPage(
            workspace_id=foreign_workspace.id,
            slug="actor-visible-read-foreign-page",
            title="Foreign page title",
            page_type="overview",
            lifecycle_status="published",
        )
        session.add(foreign_page)
        session.flush()
        foreign_version = ContextPageVersion(
            workspace_id=foreign_workspace.id,
            page_id=foreign_page.id,
            version_no=1,
            body_md="# Foreign body",
            content_hash="actor-visible-read-foreign-version",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="foreign-author",
            version_status="published_snapshot",
        )
        session.add(foreign_version)
        session.flush()
        foreign_page.current_version_id = foreign_version.id
        foreign_page.current_published_version_id = foreign_version.id
        actor = _owner(workspace.id)

        missing_page_id = uuid4()
        assert (
            context_page_id_visible(
                session,
                foreign_page.id,
                actor,
                workspace_id=workspace.id,
            )
            is False
        )
        assert (
            context_page_id_visible(
                session,
                missing_page_id,
                actor,
                workspace_id=workspace.id,
            )
            is False
        )

        foreign_version_result = resolve_visible_context_version(
            session,
            workspace_id=workspace.id,
            page_id=None,
            version_id=foreign_version.id,
            path=None,
            version_preference="visible",
            actor=actor,
        )
        missing_version_result = resolve_visible_context_version(
            session,
            workspace_id=workspace.id,
            page_id=None,
            version_id=uuid4(),
            path=None,
            version_preference="visible",
            actor=actor,
        )
        foreign_page_result = resolve_visible_context_version(
            session,
            workspace_id=workspace.id,
            page_id=foreign_page.id,
            version_id=None,
            path=None,
            version_preference="visible",
            actor=actor,
        )
        missing_page_result = resolve_visible_context_version(
            session,
            workspace_id=workspace.id,
            page_id=missing_page_id,
            version_id=None,
            path=None,
            version_preference="visible",
            actor=actor,
        )

    assert foreign_version_result == missing_version_result == (None, None)
    assert foreign_page_result == missing_page_result == (None, None)
