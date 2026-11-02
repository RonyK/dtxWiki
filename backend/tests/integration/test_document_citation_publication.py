"""Final citation materialization participates in the publication transaction."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.document_reference import (
    ContextCitationPublication,
    ContextPublishedCitation,
)
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.audit_service import AuditService
from backend.services.page_service import PageService


def test_publication_audit_failure_rolls_back_citations_and_head(
    session_factory, monkeypatch
):
    # DB budget exception: command rollback cannot detect a final publisher
    # committing its head or edges before a failing audit. One target and one
    # failure/retry traversal exercise that single independent mechanism.
    with session_factory() as session:
        workspace_id = session.scalar(select(Workspace.id))
    actor = ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        scopes=("*",),
        workspace_role="owner",
        role="owner",
        workspace_id=workspace_id,
        token_kind="session",
        provenance="browser_session",
    )
    service = PageService(session_factory, actor_context=actor)
    author = dict(
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        actor_provenance=actor.provenance,
    )
    target = service.write_page(
        workspace_slug="default",
        slug="rollback-target",
        title="Target",
        body_md="# Target",
        **author,
    )
    source = service.create_page_first(
        workspace_slug="default",
        title="Source",
        body_md=f"# Source\n\n@cite{{/p/{workspace_id}/{target.id}}}",
        **author,
    )

    def publish():
        return service.publish_draft(
            page_id=source.id,
            version_id=source.version_id,
            reason="Publish",
            publish_despite_findings=False,
            expected_content_hash=source.content_hash,
            expected_references_hash=source.references_hash,
            **author,
        )

    real_emit = AuditService.emit

    def fail_publish_audit(self, *args, **kwargs):
        if kwargs.get("action") == "context_page.draft.publish":
            raise RuntimeError("publication audit unavailable")
        return real_emit(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AuditService, "emit", fail_publish_audit)
        with pytest.raises(RuntimeError, match="publication audit unavailable"):
            publish()
    with session_factory() as session:
        assert session.get(ContextPage, source.id).current_published_version_id is None
        assert (
            session.get(ContextPageVersion, source.version_id).version_status
            == "editing"
        )
        assert not session.scalars(
            select(ContextPublishedCitation).where(
                ContextPublishedCitation.source_version_id == source.version_id
            )
        ).all()
        assert session.get(ContextCitationPublication, source.version_id) is None
        assert not session.scalars(
            select(EdgeProjection).where(EdgeProjection.source_id == source.id)
        ).all()

    publish()
    with session_factory() as session:
        assert (
            session.get(ContextPage, source.id).current_published_version_id
            == source.version_id
        )
        assert (
            len(
                session.scalars(
                    select(ContextPublishedCitation).where(
                        ContextPublishedCitation.source_version_id == source.version_id
                    )
                ).all()
            )
            == 1
        )
        assert (
            session.scalar(
                select(EdgeProjection.weight).where(
                    EdgeProjection.source_id == source.id,
                    EdgeProjection.edge_kind == "citation_page",
                )
            )
            == 1
        )
