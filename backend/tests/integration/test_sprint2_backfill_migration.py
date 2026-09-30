from datetime import UTC, datetime, timedelta
from importlib import import_module

from sqlalchemy import delete, select

from backend.models.context import (
    ContextPage,
    ContextPageVersion,
    PageLink,
    PageNameIndex,
)
from backend.models.workspace import Workspace

migration = import_module(
    "backend.migrations.versions.0002_epic1"
)


def _page_with_versions(
    session, workspace_id, *, slug, title, aliases, draft_body=None
):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=title,
        page_type="concept",
        aliases=aliases,
    )
    session.add(page)
    session.flush()
    published = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=f"# {title}\n\nPublished body.",
        content_hash=f"{slug}-published",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot",
    )
    session.add(published)
    session.flush()
    page.current_published_version_id = published.id
    page.current_version_id = published.id
    draft = None
    if draft_body is not None:
        draft = ContextPageVersion(
            workspace_id=workspace_id,
            page_id=page.id,
            version_no=2,
            body_md=draft_body,
            content_hash=f"{slug}-draft",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
        )
        session.add(draft)
        session.flush()
        page.latest_draft_version_id = draft.id
    session.flush()
    return page, published, draft


def test_backfill_projects_existing_live_heads_without_writes(session_factory):
    """MIG-2: upgrading an existing installation must populate
    page_name_index from the live published/draft heads so the entity lane
    and read_concept resolve pre-existing pages before any page write."""

    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page, published, draft = _page_with_versions(
            session,
            workspace.id,
            slug="legacy-page",
            title="Legacy Page",
            aliases=["옛문서", "Legacy Alias"],
            draft_body="# Legacy Page\n\nPending draft body.",
        )
        # Simulate the pre-backfill state of an upgraded installation.
        session.execute(delete(PageNameIndex).where(PageNameIndex.page_id == page.id))

        migration._backfill_page_name_index(session.connection())

        rows = session.execute(
            select(
                PageNameIndex.visibility_scope,
                PageNameIndex.name_norm,
                PageNameIndex.name_kind,
                PageNameIndex.source_version_id,
            ).where(PageNameIndex.page_id == page.id)
        ).all()

    by_scope: dict[str, set[tuple[str, str]]] = {"published": set(), "draft": set()}
    version_by_scope: dict[str, set] = {"published": set(), "draft": set()}
    for scope, norm, kind, version_id in rows:
        by_scope[scope].add((norm, kind))
        version_by_scope[scope].add(version_id)

    expected_names = {
        ("legacy-page", "slug"),
        ("legacy page", "title"),
        ("옛문서", "alias"),
        ("legacy alias", "alias"),
    }
    assert by_scope["published"] == expected_names
    assert by_scope["draft"] == expected_names
    assert version_by_scope["published"] == {published.id}
    assert version_by_scope["draft"] == {draft.id}


def test_downgrade_dedupes_version_scoped_page_links(session_factory):
    """MIG-1: restoring the pre-0003 (source_page_id, target_slug) unique key
    must deterministically keep one row — the published head's link wins."""

    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page, published, draft = _page_with_versions(
            session,
            workspace.id,
            slug="dedupe-source",
            title="Dedupe Source",
            aliases=[],
            draft_body="# Dedupe Source\n\nDraft [[dedupe-target]].",
        )
        base_time = datetime.now(UTC)
        published_link = PageLink(
            workspace_id=workspace.id,
            source_page_id=page.id,
            source_version_id=published.id,
            target_slug="dedupe-target",
            occurrence_count=1,
            first_snippet="published link",
        )
        draft_link = PageLink(
            workspace_id=workspace.id,
            source_page_id=page.id,
            source_version_id=draft.id,
            target_slug="dedupe-target",
            occurrence_count=1,
            first_snippet="draft link",
        )
        session.add_all([published_link, draft_link])
        session.flush()
        # The draft row is newer; the published head must still win.
        published_link.created_at = base_time - timedelta(days=1)
        draft_link.created_at = base_time
        session.flush()

        migration._dedupe_page_links_for_downgrade(session.connection())

        remaining = session.scalars(
            select(PageLink).where(PageLink.source_page_id == page.id)
        ).all()

    assert len(remaining) == 1
    assert remaining[0].source_version_id == published.id
    assert remaining[0].first_snippet == "published link"
