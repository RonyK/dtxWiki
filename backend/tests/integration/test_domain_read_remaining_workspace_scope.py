from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.domain_read import DomainReadService
from backend.services.read_errors import ReadNotFoundError


def _owner(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id="domain-read-scope-owner",
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
    )


def test_context_read_and_fetch_treat_foreign_ids_as_missing(session_factory):
    service = DomainReadService()
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        foreign_workspace = Workspace(
            slug="domain-read-remaining-foreign",
            title="Domain read remaining foreign",
            name="Domain read remaining foreign",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign_page = ContextPage(
            workspace_id=foreign_workspace.id,
            slug="domain-read-remaining-foreign-page",
            title="Foreign context title must stay opaque",
            page_type="overview",
            lifecycle_status="published",
        )
        session.add(foreign_page)
        session.flush()
        foreign_version = ContextPageVersion(
            workspace_id=foreign_workspace.id,
            page_id=foreign_page.id,
            version_no=1,
            body_md="# Foreign context body must stay opaque",
            content_hash="domain-read-remaining-foreign-version",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="foreign-author",
            version_status="published_snapshot",
        )
        session.add(foreign_version)
        session.flush()
        foreign_page.current_version_id = foreign_version.id
        foreign_page.current_published_version_id = foreign_version.id
        missing_id = uuid4()
        actor = _owner(workspace.id)

        with pytest.raises(ReadNotFoundError) as missing_read_error:
            service.read_context_page(
                session=session,
                workspace_id=workspace.id,
                page_id=missing_id,
                actor=actor,
            )
        with pytest.raises(ReadNotFoundError) as foreign_read_error:
            service.read_context_page(
                session=session,
                workspace_id=workspace.id,
                page_id=foreign_page.id,
                actor=actor,
            )
        with pytest.raises(ReadNotFoundError) as missing_fetch_error:
            service.fetch_target(
                session=session,
                workspace_id=workspace.id,
                entity_id=missing_id,
                entity_type="context_page",
                actor=actor,
            )
        with pytest.raises(ReadNotFoundError) as foreign_fetch_error:
            service.fetch_target(
                session=session,
                workspace_id=workspace.id,
                entity_id=foreign_page.id,
                entity_type="context_page",
                actor=actor,
            )

    assert foreign_read_error.value.error_code == "not_found"
    assert str(foreign_read_error.value) == str(missing_read_error.value)
    assert foreign_fetch_error.value.error_code == "not_found"
    assert str(foreign_fetch_error.value) == str(missing_fetch_error.value)
