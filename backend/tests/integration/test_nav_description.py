"""nav_description write-time derivation (Sprint-3 C1-T2, fixes A-1)."""
import pytest
from sqlalchemy import select

from backend.app.container import DatabaseProvider
from backend.models.context import ContextPageVersion
from backend.models.workspace import Workspace
from backend.repositories.context_page_repo import insert_version
from backend.schemas.context_write import WriteDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.services.indexing import Fts5Indexer, reindex_workspace
from backend.services.okf import build_workspace_index
from backend.services.page_service import PageService
from backend.tests.utils.prepared_body import prepare_context_body

pytestmark = pytest.mark.integration

BODY = "# Heading\n\nNavigation summary paragraph.\n\nSecond paragraph."


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def test_rest_write_page_stores_nav_description(page_service: PageService):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="nav-desc-rest",
        title="Nav Desc REST",
        body_md=BODY,
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, payload.version_id)
        assert version.nav_description == "Navigation summary paragraph."


def test_mcp_write_draft_stores_nav_description(session_factory):
    write = ContextWriteService()
    with session_factory.begin() as session:
        result = write.write_draft(
            session=session,
            workspace_id=_workspace_id(session),
            request=WriteDraftRequest(
                title="Nav Desc MCP",
                page_type="concept",
                body_markdown=BODY,
                reason="seed",
            ),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
        )
        version = session.get(ContextPageVersion, result.draft_version_id)
        assert version.nav_description == "Navigation summary paragraph."


def test_index_reads_stored_column_not_body(page_service: PageService):
    """Column-only proof: the index must render the stored value even when it
    diverges from the body (reverting to per-render body derivation fails)."""

    payload = page_service.write_page(
        workspace_slug="default",
        slug="nav-desc-sentinel",
        title="Nav Desc Sentinel",
        body_md=BODY,
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with page_service.session_factory.begin() as session:
        version = session.get(ContextPageVersion, payload.version_id)
        version.nav_description = "SENTINEL-COLUMN-VALUE"
    with page_service.session_factory() as session:
        index = build_workspace_index(
            session,
            _workspace_id(session),
            actor=None,
            workspace_slug="default",
        )
    assert "SENTINEL-COLUMN-VALUE" in index
    assert "Navigation summary paragraph." not in index


def test_reindex_backfills_null_nav_description(page_service: PageService):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="nav-desc-backfill",
        title="Nav Desc Backfill",
        body_md=BODY,
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with page_service.session_factory.begin() as session:
        session.get(ContextPageVersion, payload.version_id).nav_description = None
    with page_service.session_factory() as session:
        workspace_id = _workspace_id(session)
    provider = DatabaseProvider(
        None,
        session_factory=page_service.session_factory,
    )
    reindex_workspace(provider, Fts5Indexer(provider), workspace_id)
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, payload.version_id)
        assert version.nav_description == "Navigation summary paragraph."


def test_null_column_falls_back_to_read_time_derivation(
    page_service: PageService,
):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="nav-desc-null",
        title="Nav Desc Null",
        body_md=BODY,
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with page_service.session_factory.begin() as session:
        session.get(ContextPageVersion, payload.version_id).nav_description = None
    with page_service.session_factory() as session:
        index = build_workspace_index(
            session,
            _workspace_id(session),
            actor=None,
            workspace_slug="default",
        )
    assert "Navigation summary paragraph." in index


def test_insert_version_derives_from_frontmatter_stripped_body(session_factory):
    """The single-seam guarantee: any caller of insert_version gets the
    derivation, including bodies with embedded frontmatter (A-1)."""

    from backend.utils.page_frontmatter import upsert_page_frontmatter

    body = upsert_page_frontmatter(
        "Lead paragraph.\n\nTail.",
        title="Seam",
        slug="seam",
        page_type="concept",
        tags=[],
        aliases=[],
    )
    with session_factory.begin() as session:
        from backend.repositories.context_page_repo import insert_page

        page = insert_page(
            session,
            workspace_id=_workspace_id(session),
            slug="nav-desc-seam",
            title="Seam",
        )
        version = insert_version(
            session,
            page_id=page.id,
            parent_version_id=None,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=body,
                actor_kind="human",
                actor_id="tester",
            ),
            actor_kind="human",
            actor_id="tester",
            version_status="draft",
        )
        assert version.nav_description == "Lead paragraph."
