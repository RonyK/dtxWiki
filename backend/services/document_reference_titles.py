"""Bounded, version-owned Context titles for reference read projections."""

import json

from sqlalchemy import and_, func, or_

from backend.models.change import ChangeOperation
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
    owners = {
        f"context_page_version:{version_id}": (page_id, version_id)
        for page_id, version_id in visible_versions.items()
    }
    snapshots, patches = {}, {}
    refs = list(owners)
    for start in range(0, len(refs), 400):
        rows = session.execute(
            select_in_workspace(
                ChangeOperation,
                workspace_id=workspace_id,
                conditions=(
                    ChangeOperation.diff_ref.in_(refs[start : start + 400]),
                    or_(
                        and_(
                            ChangeOperation.target_type == "context_page_metadata",
                            ChangeOperation.operation == "metadata_snapshot",
                        ),
                        and_(
                            ChangeOperation.target_type == "context_page",
                            ChangeOperation.operation == "metadata_patch",
                        ),
                    ),
                ),
            )
            .with_only_columns(
                ChangeOperation.diff_ref,
                ChangeOperation.target_id,
                ChangeOperation.operation,
                ChangeOperation.diff_summary,
            )
            .order_by(ChangeOperation.created_at, ChangeOperation.id)
        )
        for diff_ref, page_id, operation, summary in rows:
            owner = owners.get(diff_ref)
            if owner is None or owner[0] != page_id:
                continue
            try:
                value = json.loads(summary or "")
            except ValueError:
                continue
            title = value.get("title") if isinstance(value, dict) else None
            if isinstance(title, str) and title.strip():
                target = snapshots if operation == "metadata_snapshot" else patches
                target[owner[1]] = title
    return {**snapshots, **patches}


def _sole_editing_titles(session, workspace_id, pages, visible_versions):
    """Use a page title only when its sole version owns all private metadata.

    First browser creation has no change-set title ledger. Prove that the
    actor-visible root editing version is the entire page history before
    using its page metadata. Even a discarded or foreign private sibling
    invalidates that proof; inherited and published rows never use it.
    """
    roots = {}
    for page_id, version_id in visible_versions.items():
        page = pages[page_id]
        current = getattr(page, "current_version", None)
        if (
            getattr(page, "current_published_version_id", None) is None
            and getattr(page, "current_version_id", None) == version_id
            and current is not None
            and current.id == version_id
            and current.page_id == page_id
            and current.workspace_id == workspace_id
            and current.version_status == "editing"
            and current.parent_version_id is None
        ):
            roots[version_id] = page_id
    page_ids = list(roots.values())
    counts = {}
    for start in range(0, len(page_ids), 400):
        counts.update(
            session.execute(
                select_in_workspace(
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    conditions=(
                        ContextPageVersion.page_id.in_(page_ids[start : start + 400]),
                    ),
                )
                .with_only_columns(ContextPageVersion.page_id, func.count())
                .group_by(ContextPageVersion.page_id)
            ).all()
        )
    return {
        version_id: pages[page_id].title
        for version_id, page_id in roots.items()
        if counts.get(page_id) == 1
    }


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
        titles = {
            **_sole_editing_titles(session, workspace_id, pages, visible_versions),
            **_metadata_titles(session, workspace_id, visible_versions),
        }
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
