"""get_index v2 — fanout-bounded tree-projection index (design §5.1, B-6).

Two automatic modes:

- **flat** (workspace content nodes <= GET_INDEX_FLAT_MAX and no ``node``):
  the historical single-call full catalog — optimal for small workspaces.
- **routing** (larger workspaces, or an explicit ``node``): renders one tree
  node per call — folders first with discriminative summaries and visible
  subtree counts, then pages, capped at GET_INDEX_NODE_FANOUT entries with an
  honest ``…and N more`` tail. depth 3 x fanout 50 covers ~125k pages with
  <=3 index reads per query.

Truncation is a *navigation* render policy: exports never route through this
module (see ``render_export_manifest`` in okf.py).
"""

from __future__ import annotations

import math
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.tree import WorkspaceTreeNode
from backend.policy.auth import ActorContext
from backend.services.context_links import link_markdown
from backend.services.context_visibility import visible_context_version_id
from backend.services.entity_ref import (
    library_concept_path,
)
from backend.services.tree_projection import TreeProjection, TreeProjectionService
from backend.utils.page_frontmatter import derive_nav_description
from backend.utils.uuid import parse_uuid_or_none

GET_INDEX_FLAT_MAX = 200
GET_INDEX_NODE_FANOUT = 50
GET_INDEX_NODE_TOKEN_BUDGET = 7_000
_ENTRY_SUMMARY_MAX = 160
_HEURISTIC_TITLE_ROLLUP = 3
_TOKEN_BUDGET_TAIL_RESERVE = 32


class IndexNodeNotFoundError(LookupError):
    """The requested index node does not exist or is not visible."""


def count_content_nodes(projection: TreeProjection) -> int:
    return sum(
        1
        for node in projection.nodes.values()
        if projection.is_visible(node.id) and node.node_kind != "folder"
    )


def build_routing_index(
    session: Session,
    workspace_id: UUID,
    *,
    actor: ActorContext | None,
    workspace_slug: str,
    sections: tuple[str, ...] = ("context", "library"),
    node: str | None = None,
    projection: TreeProjection | None = None,
) -> str:
    if projection is None:
        projection = TreeProjectionService().load(
            session, workspace_id, actor, sections=sections
        )
    if node is not None:
        target = _resolve_node_ref(session, workspace_id, projection, node)
        path = _node_path(projection, target)
        lines = [
            f"# {workspace_slug} — knowledge index (node: {path})",
            "",
            *_node_entry_lines(
                session,
                projection,
                target.id,
                actor,
                token_budget=GET_INDEX_NODE_TOKEN_BUDGET - 64,
            ),
        ]
        return "\n".join(lines).rstrip() + "\n"

    from backend.services.tree_service import section_root_node

    lines = [f"# {workspace_slug} — knowledge index", ""]
    section_budget = max(
        512,
        (GET_INDEX_NODE_TOKEN_BUDGET - 96) // max(len(sections), 1),
    )
    for section in sections:
        try:
            root = section_root_node(session, workspace_id, section)
        except Exception:  # a section without a root renders empty
            continue
        lines.append(f"## {section.capitalize()}")
        entries = _node_entry_lines(
            session,
            projection,
            root.id,
            actor,
            token_budget=section_budget,
        )
        lines.extend(entries or ["_(none)_"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _resolve_node_ref(
    session: Session,
    workspace_id: UUID,
    projection: TreeProjection,
    ref: str,
) -> WorkspaceTreeNode:
    """Resolve a node reference: UUID or a slash path of folder slugs rooted
    at the section root (e.g. ``/context/payments/gateway``)."""

    node_id = parse_uuid_or_none(ref)
    if node_id is not None:
        node = projection.nodes.get(node_id)
        if node is None or not projection.is_visible(node.id):
            raise IndexNodeNotFoundError(ref)
        return node

    parts = [part for part in ref.strip().strip("/").split("/") if part]
    if not parts:
        raise IndexNodeNotFoundError(ref)
    from backend.services.tree_service import section_root_node

    section = parts[0]
    try:
        current = section_root_node(session, workspace_id, section)
    except Exception as exc:
        raise IndexNodeNotFoundError(ref) from exc
    for slug in parts[1:]:
        match = next(
            (
                child
                for child in projection.visible_children(current.id)
                if child.node_kind == "folder" and child.slug == slug
            ),
            None,
        )
        if match is None:
            raise IndexNodeNotFoundError(ref)
        current = match
    return current


def _node_path(projection: TreeProjection, node: WorkspaceTreeNode) -> str:
    parts: list[str] = []
    current: WorkspaceTreeNode | None = node
    while current is not None:
        parts.append(current.slug or str(current.id))
        current = projection.nodes.get(current.parent_id)
    return "/" + "/".join(reversed(parts))


def _node_entry_lines(
    session: Session,
    projection: TreeProjection,
    node_id: UUID,
    actor: ActorContext | None,
    *,
    token_budget: int,
) -> list[str]:
    children = projection.visible_children(node_id, folders_first=True)
    shown = children[:GET_INDEX_NODE_FANOUT]

    folder_ids = [child.id for child in shown if child.node_kind == "folder"]
    counts = projection.subtree_content_counts(folder_ids)

    page_ids = [
        child.target_id
        for child in shown
        if child.node_kind != "folder" and child.target_kind == "context_page"
    ]
    pages: dict[UUID, ContextPage] = {}
    descriptions: dict[UUID, str] = {}
    drafts: dict[UUID, bool] = {}
    if page_ids:
        for page in session.scalars(
            select(ContextPage).where(ContextPage.id.in_(page_ids))
        ).all():
            pages[page.id] = page
            version_id, is_draft = visible_context_version_id(page, actor)
            drafts[page.id] = is_draft
            if version_id is not None:
                descriptions[page.id] = version_id  # placeholder: resolved below
        version_ids = [v for v in descriptions.values()]
        nav_by_version: dict[UUID, str | None] = {}
        null_versions: list[UUID] = []
        if version_ids:
            for vid, nav in session.execute(
                select(ContextPageVersion.id, ContextPageVersion.nav_description).where(
                    ContextPageVersion.id.in_(version_ids)
                )
            ).all():
                nav_by_version[vid] = nav
                if nav is None:
                    null_versions.append(vid)
        if null_versions:
            for version in session.scalars(
                select(ContextPageVersion).where(
                    ContextPageVersion.id.in_(null_versions)
                )
            ).all():
                nav_by_version[version.id] = derive_nav_description(version.body_md)
        descriptions = {
            page_id: (nav_by_version.get(version_id) or "")
            for page_id, version_id in descriptions.items()
        }

    lines: list[str] = []
    rendered = 0
    for child in shown:
        if child.node_kind == "folder":
            summary = child.nav_summary or _heuristic_folder_summary(
                session, projection, child
            )
            count = counts.get(child.id, 0)
            path = _node_path(projection, child)
            line = f"* \U0001f4c1 [{child.title}/](node:{path}) ({count})"
            if summary:
                line += f" \u2014 {_clip(summary)}"
        elif child.target_kind == "context_page":
            page = pages.get(child.target_id)
            if page is None:
                continue
            description = descriptions.get(page.id) or ""
            suffix = " *(draft)*" if drafts.get(page.id) else ""
            line = "* " + link_markdown(page.workspace_id, page.id, page.title)
            if description:
                line += f" \u2014 {_clip(description)}"
            line += suffix
        elif child.target_kind == "library_item":
            path = library_concept_path(child.target_id)
            line = f"* [{child.title}]({path})"
        else:
            line = f"* {child.title}"
        candidate_tokens = estimate_index_tokens("\n".join([*lines, line]))
        if candidate_tokens + _TOKEN_BUDGET_TAIL_RESERVE > token_budget:
            break
        lines.append(line)
        rendered += 1
    overflow = len(children) - rendered
    if overflow > 0:
        lines.append(
            f"\u2026and {overflow} more \u2014 narrow via a child folder, or use search"
        )
    return lines


def estimate_index_tokens(text: str) -> int:
    """Conservative dependency-free estimate for mixed ASCII/CJK navigation."""

    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return math.ceil(ascii_chars / 4) + non_ascii_chars


def heuristic_summary_text(titles: list[str], type_counts: dict[str, int]) -> str:
    """Shared fallback wording — the C-6 folder card shows the SAME text the
    index renders, so what editors see matches what agents see."""
    if not titles:
        return ""
    type_note = ""
    if type_counts:
        dominant = max(type_counts, key=type_counts.get)
        type_note = f"; mostly {dominant}"
    return f"includes {', '.join(titles)}{type_note}"


def _heuristic_folder_summary(
    session: Session,
    projection: TreeProjection,
    folder: WorkspaceTreeNode,
) -> str:
    """Render-time fallback for folders without an authored nav_summary.

    Never stored (NULL = fallback is the contract): child top-title rollup
    plus the dominant page_type mix.
    """
    children = projection.visible_children(folder.id, folders_first=True)
    titles = [child.title for child in children[:_HEURISTIC_TITLE_ROLLUP]]
    if not titles:
        return ""
    page_ids = [
        child.target_id
        for child in children
        if child.target_kind == "context_page" and child.target_id is not None
    ]
    type_counts: dict[str, int] = {}
    if page_ids:
        for (page_type,) in session.execute(
            select(ContextPage.page_type).where(ContextPage.id.in_(page_ids))
        ).all():
            type_counts[page_type] = type_counts.get(page_type, 0) + 1
    return heuristic_summary_text(titles, type_counts)


def _clip(text: str, limit: int = _ENTRY_SUMMARY_MAX) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"
