"""Actor-filtered link projections for authenticated and public page readers."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.models.workspace import Workspace
from backend.repositories.workspace_scoped import get_in_workspace, select_in_workspace
from backend.schemas.read import ContextLinkTarget, WikilinkTarget
from backend.services.context_links import (
    LinkContext,
    classify,
    configured_self_origins,
    extract_links,
    resolve,
)
from backend.services.public_page_exposure import valid_public_exposed_version_id

if TYPE_CHECKING:
    from backend.policy.auth import ActorContext


def page_link_target_condition(page: ContextPage):
    """Only unresolved slug locators may follow a target created afterwards."""
    slug_locator = or_(
        *(
            PageLink.locator_key.startswith(kind + ":")
            for kind in ("wikilink", "concept_path", "slug", "resource_uri")
        )
    )
    return (PageLink.target_page_id == page.id) | (
        PageLink.target_page_id.is_(None)
        & (PageLink.target_slug == page.slug)
        & slug_locator
    )


def context_link_targets(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
    actor: ActorContext | None = None,
    public: bool = False,
    workspace: Workspace | None = None,
    self_origins: frozenset[str] | None = None,
) -> list[ContextLinkTarget]:
    context = LinkContext(
        session=session,
        workspace_id=page.workspace_id,
        actor=None if public else actor,
        source_page_id=page.id,
        source_version_id=version.id,
        source_is_published=version.id == page.current_published_version_id,
        self_origins=(
            configured_self_origins() if self_origins is None else self_origins
        ),
    )
    targets: list[ContextLinkTarget] = []
    seen: set[str] = set()
    for occurrence in extract_links(version.body_md):
        if occurrence.href in seen:
            continue
        seen.add(occurrence.href)
        locator = classify(occurrence.href, context.self_origins)
        resolution = resolve(locator, context)
        if resolution.state == "ignored":
            continue
        # Authenticated foreign full links navigate directly without probing
        # another Workspace. Public readers cannot inherit that authority.
        if (
            resolution.state == "foreign"
            and not public
            and locator.kind in {"stable_full", "canonical_route"}
        ):
            continue
        target_id = resolution.page_id if resolution.state == "ok" else None
        href = resolution.href if target_id is not None else None
        if public and target_id is not None:
            target = get_in_workspace(
                session,
                ContextPage,
                workspace_id=page.workspace_id,
                resource_id=target_id,
            )
            exposed_id = (
                valid_public_exposed_version_id(target, workspace)
                if target is not None and workspace is not None
                else None
            )
            exposed = (
                session.scalar(
                    select_in_workspace(
                        ContextPageVersion,
                        workspace_id=page.workspace_id,
                        conditions=(
                            ContextPageVersion.page_id == target_id,
                            ContextPageVersion.id == exposed_id,
                            ContextPageVersion.version_status == "published_snapshot",
                        ),
                    )
                )
                if exposed_id is not None
                else None
            )
            href = (
                f"/w/{quote(workspace.slug, safe='')}/pages/"
                f"{quote(target.slug, safe='')}"
                + (f"#{locator.fragment}" if locator.fragment else "")
                if exposed is not None
                else None
            )
        targets.append(
            ContextLinkTarget(
                locator=occurrence.href,
                state="ok" if href is not None else "unavailable",
                href=href,
                target_page_id=target_id if href is not None and not public else None,
            )
        )
    return targets


def legacy_wikilink_targets(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
    actor: ActorContext | None,
) -> list[WikilinkTarget]:
    """Keep the legacy reader projection without bypassing target visibility."""
    context = LinkContext(
        session=session,
        workspace_id=page.workspace_id,
        actor=actor,
        source_page_id=page.id,
        source_version_id=version.id,
    )
    links = session.scalars(
        select_in_workspace(
            PageLink,
            workspace_id=page.workspace_id,
            conditions=(
                PageLink.source_page_id == page.id,
                PageLink.source_version_id == version.id,
                PageLink.locator_key.startswith("wikilink:"),
                PageLink.target_slug.is_not(None),
            ),
        ).order_by(PageLink.target_slug)
    )
    result = []
    for link in links:
        resolution = resolve(classify(f"[[{link.target_slug}]]"), context)
        visible = resolution.state == "ok"
        result.append(
            WikilinkTarget(
                target_slug=link.target_slug,
                target_page_id=resolution.page_id if visible else None,
                current_slug=resolution.target_slug if visible else None,
            )
        )
    return result
