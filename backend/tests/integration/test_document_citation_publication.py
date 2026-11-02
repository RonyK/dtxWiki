"""Final publication is the owner of document Citation relationships."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.mcp.read_tools import _concept_neighbors
from backend.models.context import ContextPage, EdgeProjection
from backend.models.document_reference import ContextPublishedCitation
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.agent import AgentPageDraftRequest
from backend.schemas.context_write import CiteRequest
from backend.schemas.graph import GraphQueryRequest, GraphSeed
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.services.agent_page_service import AgentPageService
from backend.services.audit_service import AuditService
from backend.services.body_preparer import BodyPreparer
from backend.services.citation_service import CitationService
from backend.services.extraction_service import ExtractionService
from backend.services.graph_query_service import GraphQueryService
from backend.services.graph_service import GraphService
from backend.services.library_service import LibraryService
from backend.services.page_service import PageService
from backend.services.read_errors import ReadNotFoundError
from backend.services.tree_service import TreeService, ensure_primary_tree_placement
from backend.services.workspace_graph_service import WorkspaceGraphService


def test_document_citations_publish_union_copy_clear_and_rollback(
    session_factory, media_installer_factory, monkeypatch
):
    # Budget exception: this second runtime DB lifecycle observes the final
    # publisher, two actual graph readers and head/edge rollback. The command
    # lifecycle cannot prove these independent transactional failure paths.
    # Three target kinds are explicit logical inputs required by the contract;
    # no browser/locale/writer cross-product or additional E2E case is added.
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
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
    # Origins belong to this app composition, independently of global Settings.
    # The same absolute authored citation must resolve in draft and publication.
    origin = "https://citations.example.test"
    service = PageService(
        session_factory,
        actor_context=actor,
        body_preparer=BodyPreparer(self_origins=frozenset({origin})),
    )
    author = {
        "actor_kind": actor.actor_kind,
        "actor_id": actor.actor_id,
        "actor_provenance": actor.provenance,
    }

    def create(title, body=None):
        return service.create_page_first(
            workspace_slug="default",
            title=title,
            body_md=body or f"# {title}",
            **author,
        )

    def publish(page):
        return service.publish_draft(
            page_id=page.id,
            version_id=page.version_id,
            reason="Publish references",
            publish_despite_findings=False,
            expected_content_hash=page.content_hash,
            expected_references_hash=page.references_hash,
            **author,
        ).page

    target = publish(create("Citation target"))
    delayed = create("Delayed citation target")
    with session_factory.begin() as session:
        library = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/reference-fixture",
            title="Reference Library",
            format_hint="markdown",
            raw_text="# Reference Library\n\nEvidence data.",
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session, library_item_version_id=library.version.id
        )
        library_id = library.library_item.id
        agent_service = AgentPageService()
        draft = agent_service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="skill",
                title="Reference Agent",
                slug="reference-agent-publication",
                body_markdown="# Reference Agent",
                reason="Seed reference",
            ),
            **author,
        )
        agent_id = draft.agent_page_id
    context_href = f"{origin}/p/{workspace_id}/{target.id}"
    library_href = f"/workspaces/{workspace_id}/library/items/{library_id}"
    body = (
        f"# Reference owner\n\nFirst @cite{{{context_href}}}; "
        f"second @cite{{{context_href}}}.\n\nData @cite{{{library_href}}}."
    )
    source = create("Reference owner", body)
    refs = [
        {"target_kind": "context_page", "target_id": target.id},
        {"target_kind": "library_item", "target_id": library_id},
        {"target_kind": "agent_page", "target_id": agent_id},
        {"target_kind": "context_page", "target_id": delayed.id},
    ]
    with session_factory.begin() as session:
        CitationService().cite(
            session,
            workspace_id,
            CiteRequest(
                page_id=source.id,
                version_id=source.version_id,
                expected_editing_revision=source.editing_revision,
                idempotency_key="publication-register",
                add=refs,
            ),
            actor=actor,
        )
        assert not list(
            session.scalars(
                select(ContextPublishedCitation).where(
                    ContextPublishedCitation.source_version_id == source.version_id
                )
            )
        )
        assert not list(
            session.scalars(
                select(EdgeProjection).where(EdgeProjection.source_id == source.id)
            )
        )
    source = service.read_page(source.id, actor=actor)
    published = publish(source)
    with session_factory() as session:
        citations = list(
            session.scalars(
                select(ContextPublishedCitation).where(
                    ContextPublishedCitation.source_version_id == published.version_id
                )
            )
        )
        assert {
            (row.target_kind, row.target_id, row.inline_count) for row in citations
        } == {
            ("context_page", target.id, 2),
            ("library_item", library_id, 1),
            ("agent_page", agent_id, 0),
            ("context_page", delayed.id, 0),
        }
        projected = list(
            session.scalars(
                select(EdgeProjection).where(EdgeProjection.source_id == source.id)
            )
        )
        assert {
            (row.edge_kind, row.target_kind, row.target_id, row.weight)
            for row in projected
        } == {
            ("citation_page", "context_page", target.id, 2.0),
            ("citation_source", "library_item", library_id, 1.0),
        }
        local = GraphQueryService().query(
            session,
            workspace_id,
            request=GraphQueryRequest(
                seeds=[GraphSeed(kind="context_page", id=source.id)],
                layers=["evidence"],
                edge_kinds=["citation_page", "citation_source", "citation_agent"],
            ),
            actor=actor,
            usage_signals_enabled=False,
        )
        assert {edge.edge_kind for edge in local.edges} == {
            "citation_page",
            "citation_source",
        }
        overview = WorkspaceGraphService(cursor_secret="document-citation-test").query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="evidence"),
            actor=actor,
        )
        assert {edge.relation_kind for edge in overview.edges} == {
            "citation_page",
            "citation_source",
        }
    # A stored document reference does not require the target's version ID.
    # Making that target publishable refreshes incoming projections without
    # touching or republishing the source document.
    # Independent Agent lifecycle hook: a draft write is not activation.
    # The source needs no new publication when an Agent target becomes active.
    with session_factory.begin() as session:
        agent_service.activate(
            session=session,
            workspace_id=workspace_id,
            agent_page_id=agent_id,
            version_id=draft.draft_version_id,
            reason="Activate reference",
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_role="owner",
        )
    with session_factory() as session:
        projected = list(
            session.scalars(
                select(EdgeProjection).where(EdgeProjection.source_id == source.id)
            )
        )
        assert {row.edge_kind for row in projected} == {
            "citation_page",
            "citation_source",
            "citation_agent",
        }
        # The MCP read adapter applies its own relation-kind filter. Reuse
        # these same published targets to guard it against dropping Agent.
        neighbors = _concept_neighbors(
            session,
            SimpleNamespace(
                workspace_id=workspace_id,
                services=SimpleNamespace(graph_service=GraphService()),
            ),
            source,
            actor,
        )
        assert {entry.kind for entry in neighbors.citations} == {
            "context_page", "library_item", "agent_page"
        }
        local = GraphQueryService().query(
            session,
            workspace_id,
            request=GraphQueryRequest(
                seeds=[GraphSeed(kind="context_page", id=source.id)],
                layers=["evidence"],
                edge_kinds=["citation_page", "citation_source", "citation_agent"],
            ),
            actor=actor,
            usage_signals_enabled=False,
        )
        assert {edge.edge_kind for edge in local.edges} == {
            "citation_page",
            "citation_source",
            "citation_agent",
        }
        overview = WorkspaceGraphService(cursor_secret="document-citation-test").query(
            session,
            workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="evidence"),
            actor=actor,
        )
        assert {edge.relation_kind for edge in overview.edges} == {
            "citation_page",
            "citation_source",
            "citation_agent",
        }
    publish(delayed)
    with session_factory() as session:
        assert (
            session.get(ContextPage, source.id).current_published_version_id
            == published.version_id
        )
        assert (
            len(
                list(
                    session.scalars(
                        select(EdgeProjection).where(
                            EdgeProjection.source_id == source.id
                        )
                    )
                )
            )
            == 4
        )

    # Agent and Library use separate tree lifecycle arms. Exercise both
    # logical target inputs in this existing publication lifecycle, keeping
    # their incoming edges synchronous on archive and revival.
    for kind, target_id in (("agent_page", agent_id), ("library_item", library_id)):
        with session_factory.begin() as session:
            node = session.scalar(
                select(WorkspaceTreeNode).where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.target_kind == kind,
                    WorkspaceTreeNode.target_id == target_id,
                    WorkspaceTreeNode.primary_placement.is_(True),
                )
            )
            TreeService().archive_node(
                session,
                workspace_id,
                node.id,
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
                actor=actor,
            )
            assert (
                len(
                    list(
                        session.scalars(
                            select(EdgeProjection).where(
                                EdgeProjection.source_id == source.id
                            )
                        )
                    )
                )
                == 3
            )
            ensure_primary_tree_placement(
                session,
                workspace_id,
                target_kind=kind,
                target_id=target_id,
                title=node.title,
                section=node.section,
                parent_id=node.parent_id,
                default_parent_slug="skill" if kind == "agent_page" else "library",
                actor_kind=actor.actor_kind,
                actor_id=actor.actor_id,
            )
            assert (
                len(
                    list(
                        session.scalars(
                            select(EdgeProjection).where(
                                EdgeProjection.source_id == source.id
                            )
                        )
                    )
                )
                == 4
            )
            assert (
                session.get(ContextPage, source.id).current_published_version_id
                == published.version_id
            )

    editing = service.begin_context_edit(
        page_id=source.id,
        workspace_id=workspace_id,
        idempotency_key="publication-reopen",
        **author,
    )
    assert len(editing.references) == 4
    saved = service.autosave_page(
        page_id=source.id,
        workspace_id=workspace_id,
        title="Reference owner",
        body_md="# Reference owner\n\n@cite{/p/"
        + str(workspace_id)
        + "/"
        + str(uuid4())
        + "}",
        base_version_id=None,
        base_content_hash=None,
        expected_editing_revision=editing.editing_revision,
        editing_version_id=editing.version_id,
        idempotency_key="publication-invalid-body",
        **author,
    )
    with pytest.raises(ReadNotFoundError):
        publish(saved)
    with session_factory() as session:
        assert (
            session.get(ContextPage, source.id).current_published_version_id
            == published.version_id
        )
        assert (
            len(
                list(
                    session.scalars(
                        select(EdgeProjection).where(
                            EdgeProjection.source_id == source.id
                        )
                    )
                )
            )
            == 4
        )
        assert not list(
            session.scalars(
                select(ContextPublishedCitation).where(
                    ContextPublishedCitation.source_version_id == saved.version_id
                )
            )
        )
    clean = service.autosave_page(
        page_id=source.id,
        workspace_id=workspace_id,
        title="Reference owner",
        body_md="# Reference owner\n\nNo references.",
        base_version_id=None,
        base_content_hash=None,
        expected_editing_revision=saved.editing_revision,
        editing_version_id=saved.version_id,
        idempotency_key="publication-clean-body",
        **author,
    )
    with session_factory.begin() as session:
        CitationService().cite(
            session,
            workspace_id,
            CiteRequest(
                page_id=source.id,
                version_id=clean.version_id,
                expected_editing_revision=clean.editing_revision,
                idempotency_key="publication-clear",
                remove=refs,
            ),
            actor=actor,
        )
    clean = service.read_page(source.id, actor=actor)
    original_emit = AuditService.emit

    def fail_after_materialization(self, *args, **kwargs):
        if kwargs.get("action") == "context_page.draft.publish":
            raise RuntimeError("injected publication audit failure")
        return original_emit(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AuditService, "emit", fail_after_materialization)
        with pytest.raises(RuntimeError, match="injected publication audit"):
            publish(clean)
    with session_factory() as session:
        assert (
            session.get(ContextPage, source.id).current_published_version_id
            == published.version_id
        )
        assert (
            len(
                list(
                    session.scalars(
                        select(EdgeProjection).where(
                            EdgeProjection.source_id == source.id
                        )
                    )
                )
            )
            == 4
        )
        assert not list(
            session.scalars(
                select(ContextPublishedCitation).where(
                    ContextPublishedCitation.source_version_id == clean.version_id
                )
            )
        )
    final = publish(clean)
    with session_factory() as session:
        assert not list(
            session.scalars(
                select(EdgeProjection).where(EdgeProjection.source_id == source.id)
            )
        )
        assert not list(
            session.scalars(
                select(ContextPublishedCitation).where(
                    ContextPublishedCitation.source_version_id == final.version_id
                )
            )
        )
        assert (
            len(
                list(
                    session.scalars(
                        select(ContextPublishedCitation).where(
                            ContextPublishedCitation.source_version_id
                            == published.version_id
                        )
                    )
                )
            )
            == 4
        )
