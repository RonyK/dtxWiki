from sqlalchemy import select

from backend.models.context import ContextPage, PageLink
from backend.models.workspace import Workspace
from backend.services.domain_read import DomainReadService


def _write_page(page_service, *, slug: str, title: str, body_md: str):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=title,
        body_md=body_md,
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )


def _backlink_count(page_service, target_id) -> int:
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        detail = DomainReadService().read_context_page(
            session=session,
            workspace_id=workspace_id,
            page_id=target_id,
            include_warnings=False,
        )
        return detail.backlink_count


def test_backlink_consumers_merge_link_syntaxes_by_visible_source(page_service):
    target = _write_page(
        page_service,
        slug="mixed-backlink-target",
        title="Mixed Backlink Target",
        body_md="# Mixed Backlink Target",
    )
    target_url = f"/context?target={target.id}"
    source = _write_page(
        page_service,
        slug="mixed-backlink-source",
        title="Mixed Backlink Source",
        body_md=(
            "# Mixed Backlink Source\n\n"
            f"AAA [first]({target_url}), [second]({target_url}), and "
            f"<https://wiki.example{target_url}>.\n\n"
            "ZZZ [[mixed-backlink-target]] and [[mixed-backlink-target]]."
        ),
    )
    archived_source = _write_page(
        page_service,
        slug="archived-mixed-backlink-source",
        title="Archived Mixed Backlink Source",
        body_md=f"# Archived\n\n[[mixed-backlink-target]] and [url]({target_url}).",
    )
    with page_service.session_factory.begin() as session:
        links = session.scalars(
            select(PageLink)
            .where(PageLink.source_page_id == source.id)
            .order_by(PageLink.link_syntax)
        ).all()
        session.get(ContextPage, archived_source.id).lifecycle_status = "archived"

    assert [(link.link_syntax, link.occurrence_count) for link in links] == [
        ("context_url", 3),
        ("wikilink", 2),
    ]
    assert links[0].first_snippet < links[1].first_snippet

    backlinks = page_service.list_backlinks(page_id=target.id)

    assert [(item.slug, item.occurrence_count) for item in backlinks] == [
        ("mixed-backlink-source", 5)
    ]
    assert backlinks[0].snippet == links[1].first_snippet
    assert _backlink_count(page_service, target.id) == 1


def test_backlink_slug_fallback_applies_only_to_wikilinks(page_service):
    target = _write_page(
        page_service,
        slug="fallback-backlink-target",
        title="Fallback Backlink Target",
        body_md="# Fallback Backlink Target",
    )
    url_source = _write_page(
        page_service,
        slug="url-fallback-source",
        title="URL Fallback Source",
        body_md="# URL Fallback Source",
    )
    wikilink_source = _write_page(
        page_service,
        slug="wikilink-fallback-source",
        title="Wikilink Fallback Source",
        body_md="# Wikilink Fallback Source",
    )
    with page_service.session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        session.add_all(
            [
                PageLink(
                    workspace_id=workspace_id,
                    source_page_id=url_source.id,
                    source_version_id=url_source.version_id,
                    link_syntax="context_url",
                    target_slug=target.slug,
                    target_page_id=None,
                    occurrence_count=7,
                    first_snippet="URL rows cannot be recovered by slug.",
                ),
                PageLink(
                    workspace_id=workspace_id,
                    source_page_id=wikilink_source.id,
                    source_version_id=wikilink_source.version_id,
                    link_syntax="wikilink",
                    target_slug=target.slug,
                    target_page_id=None,
                    occurrence_count=2,
                    first_snippet="Wikilinks retain slug fallback.",
                ),
            ]
        )

    backlinks = page_service.list_backlinks(page_id=target.id)

    assert [(item.slug, item.occurrence_count) for item in backlinks] == [
        ("wikilink-fallback-source", 2)
    ]
    assert _backlink_count(page_service, target.id) == 1


def test_wikilink_target_hides_archived_target_identity(page_service):
    target = _write_page(
        page_service,
        slug="archived-wikilink-target",
        title="Archived Wikilink Target",
        body_md="# Archived Wikilink Target",
    )
    source = _write_page(
        page_service,
        slug="archived-wikilink-source",
        title="Archived Wikilink Source",
        body_md="# Source\n\nSee [[archived-wikilink-target]].",
    )
    with page_service.session_factory.begin() as session:
        session.get(ContextPage, target.id).lifecycle_status = "archived"

    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        detail = DomainReadService().read_context_page(
            session=session,
            workspace_id=workspace_id,
            page_id=source.id,
            include_warnings=False,
        )

    assert len(detail.wikilink_targets) == 1
    assert detail.wikilink_targets[0].target_slug == "archived-wikilink-target"
    assert detail.wikilink_targets[0].target_page_id is None
    assert detail.wikilink_targets[0].current_slug is None
