"""llms.txt projection (design §17.3, N2.5).

Renders the public navigational index for the single public workspace.
Kept out of the API layer so backend/api never imports models directly;
structurally RBAC-isolated like the public read path — no ActorContext
ever enters. Only complete ``indexed_web`` grants bound to the current
published snapshot are listed; unlisted non-public shares never enter it.
"""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.container import DatabaseProvider
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.services.context_media_legacy_cutover import (
    LegacyMediaCutoverConflictError,
    require_no_visible_legacy_media,
)
from backend.services.page_service import title_from_markdown_heading
from backend.services.public_page_exposure import INDEXED_WEB
from backend.utils.page_frontmatter import (
    derive_nav_description,
    parse_page_frontmatter,
)


def render_llms_txt(session: Session, *, base_url: str) -> str | None:
    """Return the llms.txt markdown, or None unless EXACTLY ONE workspace is
    public (uniform 404 upstream — no existence enumeration)."""
    public_workspaces = session.scalars(
        select(Workspace).where(Workspace.visibility == "public")
    ).all()
    if len(public_workspaces) != 1:
        return None
    return _render_public_workspace_llms_txt(
        session,
        workspace=public_workspaces[0],
        base_url=base_url,
    )


def render_scoped_llms_txt(
    db: DatabaseProvider,
    *,
    workspace_slug: str,
    base_url: str,
) -> str | None:
    """Same projection as ``render_llms_txt``, scoped to one public workspace.

    ``Workspace`` (slug + ``visibility``) is identity-plane data and is
    resolved on the control session; ``ContextPage``/``ContextPageVersion``
    (and the legacy-cutover marker) are workspace-plane data and are read afterward on
    ``db.session_for(workspace.id)``, once the target workspace is known.
    This mirrors ``ContextMediaAccessPolicy`` in
    ``backend/services/context_media_access.py``. On an unsharded deployment
    both sessions come from the same factory.
    """

    with db.control_session_factory()() as session:
        workspace = session.scalar(
            select(Workspace).where(
                Workspace.slug == workspace_slug,
                Workspace.visibility == "public",
            )
        )
    if workspace is None:
        return None
    with db.session_for(workspace.id)() as workspace_session:
        return _render_public_workspace_llms_txt(
            workspace_session,
            workspace=workspace,
            base_url=base_url,
        )


def _render_public_workspace_llms_txt(
    session: Session,
    *,
    workspace: Workspace,
    base_url: str,
) -> str:
    rows = session.execute(
        select(ContextPage, ContextPageVersion)
        .join(
            ContextPageVersion,
            ContextPage.public_exposed_version_id == ContextPageVersion.id,
        )
        .where(ContextPage.workspace_id == workspace.id)
        .where(ContextPage.is_public.is_(True))
        .where(ContextPage.public_exposure_scope == INDEXED_WEB)
        .where(
            ContextPage.public_exposed_version_id
            == ContextPage.current_published_version_id
        )
        .where(ContextPageVersion.page_id == ContextPage.id)
        .where(ContextPage.lifecycle_status != "archived")
        .where(ContextPageVersion.version_status == "published_snapshot")
    ).all()
    projections = []
    for page, version in rows:
        try:
            # llms.txt is a public read surface, so it must fail closed for
            # every candidate snapshot. Skipping only the unsafe page
            # preserves a useful index for the remaining clean public pages
            # without exposing a legacy URL.
            require_no_visible_legacy_media(
                workspace_id=workspace.id,
                body_markdown=version.body_md,
            )
        except LegacyMediaCutoverConflictError:
            continue
        published_body = parse_page_frontmatter(version.body_md).body
        published_title = title_from_markdown_heading(published_body) or page.slug
        projections.append((published_title, page, version))
    projections.sort(key=lambda item: (item[0].casefold(), item[1].slug))
    lines = [f"# {workspace.title or workspace.slug}", ""]
    if workspace.goal:
        lines.extend([f"> {workspace.goal}", ""])
    lines.append("## Pages")
    lines.append("")
    for published_title, page, version in projections:
        encoded_slug = quote(page.slug, safe="")
        href = f"{base_url}/api/v1/public/w/{workspace.slug}/pages/{encoded_slug}"
        description = (
            version.nav_description
            if version.nav_description is not None
            else derive_nav_description(version.body_md)
        )
        if description:
            lines.append(f"- [{published_title}]({href}): {description}")
        else:
            lines.append(f"- [{published_title}]({href})")
    return "\n".join(lines) + "\n"
