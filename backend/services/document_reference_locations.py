"""Batch, read-only location checks for already-authorized document references."""

from collections import defaultdict
from urllib.parse import urlsplit

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryItem
from backend.repositories.context_visibility import visible_context_version_ids
from backend.repositories.workspace_scoped import select_in_workspace_by_ids
from backend.services.library_canonical import effective_library_markdown_many
from backend.utils.markdown_anchors import markdown_fragment_statuses


def reference_location_warnings(session, *, workspace_id, entries, resolved, actor):
    fragments = defaultdict(set)
    for entry in entries:
        key = entry["ref"].key
        if resolved[key].state not in {"available", "unpublished"}:
            continue
        for occurrence in entry["occurrences"]:
            fragment = urlsplit(occurrence["href"] or "").fragment
            if fragment:
                fragments[key].add(fragment)
    if not fragments:
        return {}

    def load(model, ids, *, options=()):
        ordered, result = list(ids), {}
        for start in range(0, len(ordered), 400):
            statement = select_in_workspace_by_ids(
                model,
                workspace_id=workspace_id,
                resource_ids=ordered[start : start + 400],
            )
            result.update(
                (row.id, row) for row in session.scalars(statement.options(*options))
            )
        return result

    requested = {
        kind: {rid for rkind, rid in fragments if rkind == kind}
        for kind in ("context_page", "library_item", "agent_page")
    }
    pages = load(ContextPage, requested["context_page"])
    defaults = visible_context_version_ids(
        session, workspace_id=workspace_id, pages=pages.values(), actor=actor
    )
    heads = {page_id: selected[0] for page_id, selected in defaults.items()}
    versions = load(
        ContextPageVersion, {head for head in heads.values() if head is not None}
    )
    bodies = {
        ("context_page", page_id): versions[head].body_md
        for page_id, head in heads.items()
        if head in versions
    }
    agents = load(AgentPage, requested["agent_page"])
    agent_heads = {
        page.id: page.current_active_version_id or page.latest_draft_version_id
        for page in agents.values()
    }
    agent_versions = load(
        AgentPageVersion, {head for head in agent_heads.values() if head is not None}
    )
    bodies.update(
        {
            ("agent_page", page_id): agent_versions[head].body_markdown
            for page_id, head in agent_heads.items()
            if head in agent_versions
        }
    )
    library_items = load(LibraryItem, requested["library_item"])
    bodies.update(
        {
            ("library_item", item_id): body
            for item_id, body in effective_library_markdown_many(
                session, workspace_id=workspace_id, items=list(library_items.values())
            ).items()
        }
    )
    warnings = {}
    for key, requested_fragments in fragments.items():
        body = bodies.get(key)
        if body is None:
            continue
        statuses = markdown_fragment_statuses(body, requested_fragments)
        if any(status is False for status in statuses.values()):
            warnings[key] = "fragment_unavailable"
    return warnings
