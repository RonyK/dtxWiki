from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.repositories.context_visibility import (
    visible_context_version_expression,
    visible_context_version_id,
)


def test_sql_visibility_matches_python_for_draft_only_legacy_head(session_factory):
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        page = ContextPage(
            workspace_id=workspace.id,
            slug="legacy-draft-head",
            title="Legacy draft head",
            page_type="overview",
            lifecycle_status="draft",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=1,
            body_md="# Legacy draft head",
            content_hash="legacy-draft-head-v1",
            actor_type="human",
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id="visibility-test",
        )
        session.add(version)
        session.flush()
        # Legacy/imported rows can have current_version_id without the newer
        # latest_draft pointer. SQL NULL comparison must still select the draft.
        page.current_version_id = version.id
        page.latest_draft_version_id = None
        page.current_published_version_id = None
        page_id = page.id
        version_id = version.id
        workspace_id = workspace.id

    actor = ActorContext(
        actor_kind="human",
        actor_id="visibility-editor",
        token_kind="session",
        scopes=("*",),
        workspace_id=workspace_id,
        workspace_role="editor",
        role="editor",
    )
    with session_factory() as session:
        page = session.get(ContextPage, page_id)
        assert page is not None
        python_id, is_draft = visible_context_version_id(page, actor)
        sql_id = session.scalar(
            select(visible_context_version_expression(actor)).where(
                ContextPage.id == page_id
            )
        )

    assert python_id == version_id
    assert is_draft is True
    assert sql_id == python_id
