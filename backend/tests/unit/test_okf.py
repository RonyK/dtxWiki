from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.index_refs import format_index_ref
from backend.services.okf import (
    build_workspace_index,
    parse_concept_path,
    project_context_frontmatter,
    render_context_concept,
    render_frontmatter,
    resolve_context_concept,
)
from backend.services.page_service import sync_page_projections
from backend.utils.page_frontmatter import (
    derive_nav_description,
    upsert_page_frontmatter,
)

pytestmark = pytest.mark.unit


def _actor(role="editor", scopes=()):
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=tuple(scopes),
        workspace_role=role,
    )


def _workspace_id(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def _add_context_page(
    session, workspace_id, *, slug, title, body, page_type="concept", published=True
):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=title,
        page_type=page_type,
        tags=["alpha", "beta"],
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        body_md=body,
        content_hash=f"hash-{slug}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot" if published else "draft",
    )
    session.add(version)
    session.flush()
    if published:
        page.current_published_version_id = version.id
    else:
        page.latest_draft_version_id = version.id
    session.flush()
    # Every production write path syncs the name projection; concept
    # resolution reads page_name_index only (no slug fallback since T3).
    sync_page_projections(session, page=page, version=version)
    return page


def _add_library_item(session, workspace_id, *, uri, title, trust_level="unknown"):
    item = LibraryItem(
        workspace_id=workspace_id,
        connector_type="manual_upload",
        uri=uri,
        title=title,
        trust_level=trust_level,
        lifecycle_status="ready",
    )
    session.add(item)
    session.flush()
    return item


# --- pure functions ---------------------------------------------------------


def test_derive_nav_description_skips_headings_and_truncates():
    assert derive_nav_description("# Title\n\nFirst para.\n\nSecond.") == "First para."
    # "" (not None) when there is no prose: a stored NULL means a legacy row.
    assert derive_nav_description("") == ""
    assert derive_nav_description("# only heading") == ""
    assert derive_nav_description("  \n ") == ""
    long = "word " * 100
    out = derive_nav_description(long, max_length=20)
    assert len(out) <= 20 and out.endswith("…")
    assert (
        derive_nav_description(
            "# Title\n\n- 상태: draft\n  wrapped metadata\n- 담당자: owner\n\n"
            "1. checklist\n2) another item\n\n"
            "**Status**: approved\nUpdated: today\n\n"
            "Actual subject paragraph.\n\nLater prose."
        )
        == "Actual subject paragraph."
    )
    assert derive_nav_description("# Title\n\n- list only\n- no prose") == ""
    # Index readers get text only: an image-only paragraph is not prose,
    # while an image inside prose stays as written.
    image = "![구조도](/api/media/1)"
    assert derive_nav_description(f"{image}\n\n본문 설명.") == "본문 설명."
    assert derive_nav_description(f"# T\n\n{image}") == ""
    assert derive_nav_description(f"흐름은 {image} 참고.") == f"흐름은 {image} 참고."
    # A definition after a reference image is not prose either, an image-only
    # paragraph must not join prose across a fence, and code and raw HTML are
    # content. Definitions elsewhere in the body resolve reference images and
    # reference links around images.
    assert derive_nav_description("![구조도][d]\n\n[d]: https://x/d.png") == ""
    linked = "[![a](b)][d]\n\n[d]: /target\n\n본문."
    assert derive_nav_description(linked) == "본문."
    assert derive_nav_description("    ![a](b)\n\n본문.") == "![a](b)"
    assert derive_nav_description(f"{image}\n```\ncode\n```\n본문.") == "본문."
    assert derive_nav_description("`![a](b)` 문법.") == "`![a](b)` 문법."
    html = "<div>보이는 글</div>"
    assert derive_nav_description(f"{html}\n\n본문.") == html


def test_derive_nav_description_is_frontmatter_aware():
    """A-1: an editor-saved body (upserted frontmatter, no blank line after
    the closing ``---``) must yield the FIRST paragraph, and a
    single-paragraph page must not come back empty."""

    body = upsert_page_frontmatter(
        "First para.\n\nSecond para.",
        title="T",
        slug="t",
        page_type="concept",
        tags=[],
        aliases=[],
    )
    assert derive_nav_description(body) == "First para."
    single = upsert_page_frontmatter(
        "Only one paragraph.",
        title="T",
        slug="t",
        page_type="concept",
        tags=[],
        aliases=[],
    )
    assert derive_nav_description(single) == "Only one paragraph."


def test_render_frontmatter_skips_empty_and_renders_lists():
    block = render_frontmatter(
        {"type": "concept", "title": "T", "description": "", "tags": ["a", "b"]}
    )
    assert block.startswith("---") and block.endswith("---")
    assert "type: concept" in block
    assert "description:" not in block  # empty skipped
    assert "tags: [a, b]" in block


def test_render_frontmatter_quotes_yaml_special_values():
    """I9 (Codex F6): values with colons/commas/brackets/hashes must produce
    valid YAML, not a scanner error in downstream consumers."""
    import yaml

    block = render_frontmatter(
        {
            "title": "Incident: Alpha",
            "description": "see #ref, and [bracket]",
            "tags": ["one,two", "[bracket]", "key: value"],
            "resource": "/w/x/context/y",
        }
    )
    # The whole frontmatter (minus the --- fences) must parse as valid YAML.
    inner = block.split("---\n", 1)[1].rsplit("\n---", 1)[0]
    parsed = yaml.safe_load(inner)
    assert parsed["title"] == "Incident: Alpha"
    assert parsed["tags"] == ["one,two", "[bracket]", "key: value"]
    assert parsed["resource"] == "/w/x/context/y"  # simple value stays unquoted


def test_parse_concept_path():
    assert parse_concept_path("/context/my-page.md") == ("context", "my-page")
    assert parse_concept_path("library/abc.md") == ("library", "abc")
    assert parse_concept_path("/context/no-extension") is None
    assert parse_concept_path("/agent/x.md") is None


def test_render_context_concept_has_frontmatter_and_body(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page = _add_context_page(
            session, workspace_id, slug="api", title="API", body="# API\n\nThe body."
        )
        version = session.get(ContextPageVersion, page.current_published_version_id)
        doc = render_context_concept(page, version, workspace_slug="default")
    assert doc.startswith("---\n")
    assert "type: concept" in doc
    assert "title: API" in doc
    assert "The body." in doc
    fm = project_context_frontmatter(page, version, workspace_slug="default")
    assert fm["type"] == "concept"
    assert fm["resource"] == f"/p/{page.workspace_id}/{page.id}"


# --- index per-actor visibility ---------------------------------------------


def test_index_published_visible_to_all_drafts_only_to_editors(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        pub = _add_context_page(
            session, workspace_id, slug="pub", title="Published", body="Pub body."
        )
        draft = _add_context_page(
            session,
            workspace_id,
            slug="draft",
            title="Draftive",
            body="Draft body.",
            published=False,
        )

    with session_factory() as session:
        editor_index = build_workspace_index(
            session,
            workspace_id,
            actor=_actor("editor"),
            workspace_slug="default",
        )
        viewer_index = build_workspace_index(
            session,
            workspace_id,
            actor=_actor("viewer"),
            workspace_slug="default",
        )

    pub_line = f"- [Published]({format_index_ref('context', pub.id)})"
    assert pub_line in editor_index
    assert (
        f"- [Draftive]({format_index_ref('context', draft.id)}) (draft)"
        in editor_index
    )
    # Viewer sees the published concept but not the draft-only one.
    assert pub_line in viewer_index
    assert str(draft.id) not in viewer_index


def test_index_restricted_library_filtered_by_actor(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _add_library_item(session, workspace_id, uri="https://x/open", title="Open Doc")
        _add_library_item(
            session,
            workspace_id,
            uri="https://x/secret",
            title="Secret Doc",
            trust_level="restricted",
        )

    with session_factory() as session:
        editor_index = build_workspace_index(
            session, workspace_id, actor=_actor("editor"), workspace_slug="default"
        )
        owner_index = build_workspace_index(
            session, workspace_id, actor=_actor("owner"), workspace_slug="default"
        )

    assert "Open Doc" in editor_index
    assert "Secret Doc" not in editor_index  # editor cannot read restricted
    assert "Secret Doc" in owner_index  # owner can


def test_resolve_context_concept_honors_draft_visibility(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _add_context_page(
            session,
            workspace_id,
            slug="wip",
            title="WIP",
            body="Work in progress.",
            published=False,
        )

    with session_factory() as session:
        assert (
            resolve_context_concept(
                session, workspace_id, slug="wip", actor=_actor("editor")
            )
            is not None
        )
        assert (
            resolve_context_concept(
                session, workspace_id, slug="wip", actor=_actor("viewer")
            )
            is None
        )
        assert (
            resolve_context_concept(
                session, workspace_id, slug="missing", actor=_actor("owner")
            )
            is None
        )


def test_draft_capable_actor_reads_draft_head_over_published(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page = _add_context_page(
            session,
            workspace_id,
            slug="dual-head",
            title="Dual Head",
            body="Published body.",
        )
        draft = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=page.id,
            version_no=2,
            body_md="Draft body.",
            content_hash="hash-dual-head-draft",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
        )
        session.add(draft)
        session.flush()
        page.latest_draft_version_id = draft.id
        # Production draft writes resync the name projection (draft scope
        # rows), which concept resolution requires for the draft head.
        sync_page_projections(session, page=page, version=draft)

    with session_factory() as session:
        _page, editor_version = resolve_context_concept(
            session, workspace_id, slug="dual-head", actor=_actor("editor")
        )
        _page, viewer_version = resolve_context_concept(
            session, workspace_id, slug="dual-head", actor=_actor("viewer")
        )
        editor_index = build_workspace_index(
            session, workspace_id, actor=_actor("editor"), workspace_slug="default"
        )

    assert editor_version.body_md == "Draft body."
    assert viewer_version.body_md == "Published body."
    assert (
        f"- [Dual Head]({format_index_ref('context', page.id)}) (draft)"
        in editor_index
    )
