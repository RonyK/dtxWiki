"""Bounded, version-owned Context titles for reference read projections."""

from sqlalchemy import func

from backend.models.context import ContextPageVersion
from backend.repositories.workspace_scoped import select_in_workspace
from backend.utils.page_frontmatter import (
    parse_page_frontmatter,
    title_from_markdown_heading,
)

# Older versions have no title snapshot. Read a small prefix from their
# visible body, never every full cited document. A missing provable heading
# falls back to the stable slug instead of mutable private page metadata.
TITLE_BODY_PREFIX_LENGTH = 16_384


def _metadata_titles(session, workspace_id, visible_versions):
    result = {}
    version_ids = list(visible_versions.values())
    for start in range(0, len(version_ids), 400):
        rows = session.execute(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                conditions=(
                    ContextPageVersion.id.in_(version_ids[start : start + 400]),
                ),
            ).with_only_columns(
                ContextPageVersion.id,
                ContextPageVersion.page_id,
                ContextPageVersion.page_metadata,
            )
        )
        for version_id, page_id, metadata in rows:
            if visible_versions.get(page_id) != version_id:
                continue
            title = (metadata or {}).get("title")
            if isinstance(title, str) and title.strip():
                result[version_id] = title
    return result


def context_reference_titles(
    session, *, workspace_id, pages, visible_versions, public=False
):
    """Return only titles belonging to the caller's selected visible versions.

    Public reads keep their established body-heading policy. Authenticated
    readers also see a selected draft's exact staged metadata. No title or
    permission result is cached across readers or transactions.
    """
    titles = {}
    if not public:
        titles = _metadata_titles(session, workspace_id, visible_versions)
    missing = [value for value in visible_versions.values() if value not in titles]
    verified = set()
    for start in range(0, len(missing), 400):
        conditions = (ContextPageVersion.id.in_(missing[start : start + 400]),)
        if public:
            conditions += (ContextPageVersion.version_status == "published_snapshot",)
        rows = session.execute(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=workspace_id,
                conditions=conditions,
            ).with_only_columns(
                ContextPageVersion.id,
                ContextPageVersion.page_id,
                func.substr(
                    ContextPageVersion.body_md, 1, TITLE_BODY_PREFIX_LENGTH + 1
                ).label("title_prefix"),
            )
        )
        for version_id, page_id, prefix in rows:
            if visible_versions.get(page_id) != version_id:
                continue
            verified.add(page_id)
            truncated = len(prefix) > TITLE_BODY_PREFIX_LENGTH
            prefix = (
                prefix[:TITLE_BODY_PREFIX_LENGTH]
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
            if truncated:
                newline = prefix.rfind("\n")
                prefix = prefix[:newline] if newline >= 0 else ""
            parsed = parse_page_frontmatter(prefix)
            if (
                truncated
                and prefix.partition("\n")[0].strip() == "---"
                and not parsed.has_frontmatter
            ):
                continue
            title = (
                parsed.title if not public else None
            ) or title_from_markdown_heading(parsed.body)
            if title:
                titles[version_id] = title
    return {
        page_id: titles.get(version_id) or pages[page_id].slug
        for page_id, version_id in visible_versions.items()
        if not public or page_id in verified
    }
