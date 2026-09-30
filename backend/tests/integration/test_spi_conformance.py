"""SPI 1.0 signature freeze and search backend killer contracts."""

from __future__ import annotations

import inspect
from dataclasses import fields

import pytest
from sqlalchemy import select

from backend.app.container import ToolCallContext as CompatToolCallContext
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.agent import AgentPageDraftRequest
from backend.schemas.context_write import WriteDraftRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.agent_page_service import AgentPageService
from backend.services.context_write_service import ContextWriteService
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.page_service import PageService
from backend.services.retrieval.protocol import Retriever as CompatRetriever
from backend.services.search_service import SearchService
from backend.services.tree_service import TreeService
from backend.spi import (
    SPI_VERSION,
    DatabaseProvider,
    Indexer,
    Retriever,
    ToolCallContext,
)
from backend.testing import ResultRef, SearchContractCase, assert_search_contract

pytestmark = pytest.mark.integration

MARKER = "SPIKillerMarker"


def _public_callables(contract) -> set[str]:
    return {
        name
        for name, value in vars(contract).items()
        if not name.startswith("_") and callable(value)
    }


def _public_properties(contract) -> set[str]:
    return {
        name
        for name, value in vars(contract).items()
        if not name.startswith("_") and isinstance(value, property)
    }


def _actor(role: str) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=f"spi-{role}",
        token_kind="session",
        scopes=(),
        workspace_role=role,
        role=role,
    )


def test_spi_1_0_public_surface_and_signatures_are_frozen():
    assert SPI_VERSION == "1.0"
    assert CompatToolCallContext is ToolCallContext
    assert CompatRetriever is Retriever
    assert _public_callables(Retriever) == {"supported_modes", "retrieve"}
    assert _public_callables(Indexer) == {"upsert", "delete", "rebuild"}
    assert _public_callables(DatabaseProvider) == {
        "control_session_factory",
        "session_for",
    }
    assert tuple(field.name for field in fields(ToolCallContext)) == (
        "actor",
        "db",
        "page_service",
        "search_service",
        "conversation_token",
        "services",
        "workspace_id",
    )
    assert _public_properties(ToolCallContext) == {"session_factory"}
    assert isinstance(vars(ToolCallContext)["session_factory"], property)
    assert str(inspect.signature(Retriever.supported_modes)) == (
        "(self) -> 'set[str]'"
    )
    assert str(inspect.signature(Retriever.retrieve)) == (
        "(self, *, session: 'Session', workspace_id: 'UUID', query: 'str', "
        "scope: 'str', filters: 'SearchFilters', limit: 'int', "
        "actor: 'ActorContext | None') -> 'list[RankedResult]'"
    )
    assert str(inspect.signature(Indexer.upsert)) == (
        "(self, *, workspace_id: 'UUID', source_kind: 'str', "
        "source_id: 'UUID') -> 'None'"
    )
    assert inspect.signature(Indexer.delete) == inspect.signature(Indexer.upsert)
    assert str(inspect.signature(Indexer.rebuild)) == (
        "(self, *, workspace_id: 'UUID', source_kind: 'str | None' = None, "
        "source_id: 'UUID | None' = None) -> 'dict[str, int]'"
    )
    assert str(inspect.signature(ToolCallContext)) == (
        "(actor: 'Any', db: 'DatabaseProvider', page_service: 'Any', "
        "search_service: 'Any | None' = None, conversation_token: "
        "'str | None' = None, services: 'Any | None' = None, workspace_id: "
        "'UUID | None' = None) -> None"
    )
    assert str(inspect.signature(DatabaseProvider.session_for)) == (
        "(self, workspace_id: 'UUID') -> 'sessionmaker[Session]'"
    )
    assert str(inspect.signature(DatabaseProvider.control_session_factory)) == (
        "(self) -> 'sessionmaker[Session]'"
    )
    assert str(
        inspect.signature(vars(ToolCallContext)["session_factory"].fget)
    ) == "(self) -> 'sessionmaker[Session]'"


def test_keyword_backend_enforces_workspace_and_visibility_contracts(
    session_factory, media_installer_factory
):
    page_service = PageService(session_factory)
    with session_factory.begin() as session:
        workspace_a = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        workspace_b = seed_default_workspace(
            session,
            slug="spi-other",
            title="SPI Other",
        )
        workspace_a_id = workspace_a.id
        workspace_b_id = workspace_b.id

    published = page_service.write_page(
        workspace_slug="default",
        slug="spi-published",
        title="SPI Published",
        body_md=f"# SPI Published\n\n{MARKER} published.",
        actor_kind="human",
        actor_id="spi-test",
        actor_provenance="browser_session",
    )
    cross_workspace = page_service.write_page(
        workspace_slug="spi-other",
        slug="spi-cross-workspace",
        title="SPI Cross Workspace",
        body_md=f"# SPI Cross Workspace\n\n{MARKER} cross tenant.",
        actor_kind="human",
        actor_id="spi-test",
        actor_provenance="browser_session",
    )
    archived = page_service.write_page(
        workspace_slug="default",
        slug="spi-archived",
        title="SPI Archived",
        body_md=f"# SPI Archived\n\n{MARKER} archived.",
        actor_kind="human",
        actor_id="spi-test",
        actor_provenance="browser_session",
    )

    with session_factory.begin() as session:
        draft = ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace_a_id,
            request=WriteDraftRequest(
                title="SPI Private Draft",
                page_type="concept",
                body_markdown=f"# SPI Private Draft\n\n{MARKER} draft.",
                reason="SPI conformance",
            ),
            actor_kind="human",
            actor_id="spi-test",
            actor_role="owner",
        )
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_a_id,
            canonical_uri="https://example.com/spi-restricted",
            title="SPI Restricted Source",
            trust_level="restricted",
            raw_text=f"# SPI Restricted Source\n\n{MARKER} restricted.",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        sensitive = AgentPageService().write_draft(
            session=session,
            workspace_id=workspace_a_id,
            request=AgentPageDraftRequest(
                category="memory",
                title="SPI Sensitive Memory",
                slug="spi-sensitive-memory",
                body_markdown=f"# SPI Sensitive Memory\n\n{MARKER} sensitive.",
                reason="SPI conformance",
            ),
            actor_kind="human",
            actor_id="spi-test",
            actor_provenance="browser_session",
        )
        archived_node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_a_id,
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == archived.id,
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        )
        assert archived_node is not None
        # Overlay CI consumes this conformance case: archived entities are
        # unreturnable even when their old index text still matches.
        TreeService().archive_node(
            session=session,
            workspace_id=workspace_a_id,
            node_id=archived_node.id,
            actor_kind="human",
            actor_id="spi-test",
        )

    published_ref = ResultRef("context_page", published.id)
    draft_ref = ResultRef("context_page", draft.page_id)
    artifact_ref = ResultRef("library_artifact", artifact.id)
    sensitive_ref = ResultRef("agent_page", sensitive.agent_page_id)
    cross_ref = ResultRef("context_page", cross_workspace.id)
    archived_ref = ResultRef("context_page", archived.id)
    service = SearchService()

    with session_factory() as session:
        assert_search_contract(
            lambda **kwargs: service.search(session=session, **kwargs),
            [
                SearchContractCase(
                    name="viewer",
                    workspace_id=workspace_a_id,
                    query=MARKER,
                    actor=_actor("viewer"),
                    expected=frozenset({published_ref}),
                    forbidden=frozenset(
                        {
                            draft_ref,
                            artifact_ref,
                            sensitive_ref,
                            cross_ref,
                            archived_ref,
                        }
                    ),
                ),
                SearchContractCase(
                    name="editor",
                    workspace_id=workspace_a_id,
                    query=MARKER,
                    actor=_actor("editor"),
                    expected=frozenset({published_ref, draft_ref}),
                    forbidden=frozenset(
                        {artifact_ref, sensitive_ref, cross_ref, archived_ref}
                    ),
                ),
                SearchContractCase(
                    name="owner",
                    workspace_id=workspace_a_id,
                    query=MARKER,
                    actor=_actor("owner"),
                    expected=frozenset(
                        {published_ref, draft_ref, artifact_ref, sensitive_ref}
                    ),
                    forbidden=frozenset({cross_ref, archived_ref}),
                ),
                SearchContractCase(
                    name="reverse-workspace-viewer",
                    workspace_id=workspace_b_id,
                    query=MARKER,
                    actor=_actor("viewer"),
                    expected=frozenset({cross_ref}),
                    forbidden=frozenset(
                        {
                            published_ref,
                            draft_ref,
                            artifact_ref,
                            sensitive_ref,
                            archived_ref,
                        }
                    ),
                ),
            ],
        )

    assert workspace_a_id != workspace_b_id
