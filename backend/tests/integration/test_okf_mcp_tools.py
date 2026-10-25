from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.container import DatabaseProvider, ServiceRegistry, ToolCallContext
from backend.mcp.read_tools import get_index, read_concept
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.okf import project_context_frontmatter, render_frontmatter
from backend.services.page_service import sync_page_projections

pytestmark = pytest.mark.integration


def _ctx(actor, session_factory) -> ToolCallContext:
    return ToolCallContext(
        actor=actor,
        db=DatabaseProvider(None, session_factory=session_factory),
        page_service=None,
        services=ServiceRegistry(),
        workspace_id=actor.workspace_id,
    )


def _seed_pages(session, workspace_id):
    pub = ContextPage(
        workspace_id=workspace_id, slug="pub", title="Pub", page_type="concept"
    )
    session.add(pub)
    session.flush()
    pv = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=pub.id,
        body_md="Published body.",
        content_hash="h1",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="t",
        version_status="published_snapshot",
    )
    session.add(pv)
    session.flush()
    pub.current_published_version_id = pv.id

    draft = ContextPage(
        workspace_id=workspace_id, slug="wip", title="Wip", page_type="concept"
    )
    session.add(draft)
    session.flush()
    dv = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=draft.id,
        body_md="Draft body.",
        content_hash="h2",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="t",
        version_status="draft",
    )
    session.add(dv)
    session.flush()
    draft.latest_draft_version_id = dv.id
    session.flush()
    # Production writes sync the name projection; read_concept resolves via
    # page_name_index only (no slug fallback since the 0003 backfill).
    sync_page_projections(session, page=pub, version=pv)
    sync_page_projections(session, page=draft, version=dv)


def _actor(workspace_id, role):
    return ActorContext(
        actor_kind="mcp_client",
        actor_id="agent",
        token_kind="mcp_client",
        scopes=("mcp:read",),
        workspace_id=workspace_id,
        workspace_role=role,
    )


def _default_workspace_id(session_factory):
    with session_factory.begin() as session:
        wsid = session.scalar(select(Workspace).where(Workspace.slug == "default")).id
        _seed_pages(session, wsid)
    return wsid


def test_get_index_tool_filters_drafts_by_role(session_factory):
    wsid = _default_workspace_id(session_factory)
    payload = {"workspace_id": str(wsid)}
    editor = get_index(payload, _ctx(_actor(wsid, "editor"), session_factory))
    viewer = get_index(payload, _ctx(_actor(wsid, "viewer"), session_factory))

    assert editor.envelope.result_status == "succeeded"
    assert f"[Pub](/p/{wsid}/" in editor.index
    assert f"[Wip](/p/{wsid}/" in editor.index  # editor sees the draft-only concept
    assert f"[Pub](/p/{wsid}/" in viewer.index
    assert "[Wip](" not in viewer.index  # viewer does not


def test_read_concept_tool_returns_frontmatter_and_body(session_factory):
    wsid = _default_workspace_id(session_factory)
    ok = read_concept(
        {"workspace_id": str(wsid), "path": "/context/pub.md"},
        _ctx(_actor(wsid, "viewer"), session_factory),
    )
    assert ok.envelope.result_status == "succeeded"
    assert "Published body." in ok.content
    assert ok.frontmatter["type"] == "concept"
    assert ok.version_status == "published_snapshot"
    assert ok.frontmatter["version_status"] == "published_snapshot"

    draft = read_concept(
        {"workspace_id": str(wsid), "path": "/context/wip.md"},
        _ctx(_actor(wsid, "editor"), session_factory),
    )
    assert draft.envelope.result_status == "succeeded"
    assert draft.version_status == "draft"
    assert draft.frontmatter["version_status"] == "draft"

    # Draft-only concept is invisible to a viewer.
    nf = read_concept(
        {"workspace_id": str(wsid), "path": "/context/wip.md"},
        _ctx(_actor(wsid, "viewer"), session_factory),
    )
    assert nf.envelope.error_code == "not_found"

    # Library concepts are not served here (use the `read` tool).
    lib = read_concept(
        {"workspace_id": str(wsid), "path": "/library/x.md"},
        _ctx(_actor(wsid, "viewer"), session_factory),
    )
    assert lib.envelope.error_code == "not_found"


def test_okf_frontmatter_never_drops_version_status_when_version_is_absent():
    page = ContextPage(
        workspace_id=uuid4(),
        slug="no-version",
        title="No version",
        page_type="concept",
        tags=[],
        aliases=[],
    )
    frontmatter = project_context_frontmatter(
        page,
        None,
        workspace_slug="default",
    )
    assert frontmatter["version_status"] == "unavailable"
    assert "version_status: unavailable" in render_frontmatter(frontmatter)


def test_get_index_validation_failed_on_bad_payload(session_factory):
    out = get_index(
        {"unexpected": "x"}, _ctx(_actor(uuid4(), "viewer"), session_factory)
    )
    assert out.envelope.error_code == "validation_failed"
