from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.okf import export_workspace_bundle

pytestmark = pytest.mark.integration


def _actor(role="editor"):
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role=role,
    )


def _add_page(session, workspace_id, slug, title, body, published=True):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=title,
        page_type="concept",
        tags=["t"],
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=body,
        content_hash=f"h{uuid4().hex[:8]}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="t",
        version_status="published_snapshot" if published else "draft",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    if published:
        page.current_published_version_id = version.id
    else:
        page.latest_draft_version_id = version.id
    session.flush()
    return page


def test_export_workspace_bundle_respects_visibility(session_factory):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).id
        _add_page(session, workspace_id, "alpha", "Alpha", "# Alpha\n\nAlpha body.")
        _add_page(session, workspace_id, "wip", "Wip", "Draft only.", published=False)

    with session_factory() as session:
        viewer_bundle = export_workspace_bundle(
            session, workspace_id, actor=_actor("viewer"), workspace_slug="default"
        )
        editor_bundle = export_workspace_bundle(
            session, workspace_id, actor=_actor("editor"), workspace_slug="default"
        )

    assert "index.md" in viewer_bundle
    assert "context/alpha.md" in viewer_bundle
    concept = viewer_bundle["context/alpha.md"]
    assert concept.startswith("---\n")
    assert "type: concept" in concept
    assert "Alpha body." in concept
    # Draft-only concept is excluded for a viewer, included for an editor.
    assert "context/wip.md" not in viewer_bundle
    assert "context/wip.md" in editor_bundle


def test_import_okf_bundle_is_liberal(session_factory):
    from backend.services.okf import import_okf_bundle
    from backend.services.page_service import PageService

    page_service = PageService(session_factory)
    bundle = {
        "index.md": "# index (skipped)",
        "context/imported.md": (
            "---\ntype: concept\ntitle: Imported Title\ntags: [a, b]\n---\n\n"
            "Imported body content."
        ),
        "context/weird.md": (
            "---\ntype: BigQuery Table\ntitle: Weird\n---\n\nWeird body."
        ),
    }
    slugs = import_okf_bundle(
        page_service,
        workspace_slug="default",
        bundle=bundle,
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    assert set(slugs) == {"imported", "weird"}
    with session_factory() as session:
        imported = session.scalar(
            select(ContextPage).where(ContextPage.slug == "imported")
        )
        weird = session.scalar(
            select(ContextPage).where(ContextPage.slug == "weird")
        )
    assert imported.title == "Imported Title"
    assert imported.page_type == "concept"
    # An unknown OKF type is accepted (liberal consumption) and mapped to default.
    assert weird.page_type == "concept"


def test_import_okf_bundle_skips_existing_slugs(session_factory):
    """M2: re-importing must not crash on an existing slug (write_page's update
    path needs a base hash); existing pages are skipped, non-destructively."""
    from backend.services.okf import import_okf_bundle
    from backend.services.page_service import PageService

    page_service = PageService(session_factory)
    bundle = {
        "context/dup.md": "---\ntype: concept\ntitle: Dup\n---\n\nBody one.",
    }
    first = import_okf_bundle(
        page_service,
        workspace_slug="default",
        bundle=bundle,
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    assert first == ["dup"]

    # Re-import the same bundle: the existing slug is skipped (no crash, no
    # partial import), so nothing new is reported.
    second = import_okf_bundle(
        page_service,
        workspace_slug="default",
        bundle=bundle,
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    assert second == []


def test_rendered_concept_has_exactly_one_frontmatter_block(page_service):
    """A-2: editor-saved bodies carry an upserted embedded frontmatter block;
    the rendered OKF concept must strip it (one block, no title: in body)."""

    from backend.utils.page_frontmatter import upsert_page_frontmatter

    body = upsert_page_frontmatter(
        "Real first paragraph.\n\nSecond.",
        title="A2 Page",
        slug="a2-page",
        page_type="concept",
        tags=["t1"],
        aliases=["A2 Alias"],
    )
    page_service.write_page(
        workspace_slug="default",
        slug="a2-page",
        title="A2 Page",
        body_md=body,
        actor_kind="human",
        actor_id="tester",
        aliases=["A2 Alias"],
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        bundle = export_workspace_bundle(
            session, workspace_id, actor=None, workspace_slug="default"
        )
    doc = bundle["context/a2-page.md"]
    assert doc.count("---\n") + doc.count("\n---") >= 2  # opening+closing exist
    # exactly one frontmatter block: after the first closing '---', no more
    head, _, rest = doc.partition("\n---\n")
    assert rest.count("\n---\n") == 0
    # body region carries prose, not a second title: key
    assert rest.strip().startswith("Real first paragraph.")
    # aliases projected into OKF frontmatter
    assert "aliases:" in head or "aliases:" in doc.split("\n---\n")[0]


def test_export_import_roundtrip_preserves_aliases(page_service, session_factory):
    from backend.services.okf import import_okf_bundle

    page_service.write_page(
        workspace_slug="default",
        slug="roundtrip-src",
        title="Roundtrip Source",
        body_md="# Roundtrip Source\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        page_type="concept",
        tags=["tag-a"],
        aliases=["별칭 하나", "Alias Two"],
        actor_provenance="browser_session",
    )
    with session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        bundle = export_workspace_bundle(
            session, workspace_id, actor=None, workspace_slug="default"
        )
    renamed = {
        "index.md": bundle["index.md"],
        "context/roundtrip-dst.md": bundle["context/roundtrip-src.md"],
    }
    imported = import_okf_bundle(
        page_service,
        workspace_slug="default",
        bundle=renamed,
        actor_id="tester",
        actor_provenance="browser_session",
    )
    assert imported == ["roundtrip-dst"]
    with session_factory() as session:
        page = session.scalar(
            select(ContextPage).where(ContextPage.slug == "roundtrip-dst")
        )
        assert page.title == "Roundtrip Source"
        assert page.page_type == "concept"
        assert set(page.aliases) == {"별칭 하나", "Alias Two"}
        assert page.tags == ["tag-a"]
