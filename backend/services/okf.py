"""OKF (Open Knowledge Format v0.1) projection — Epic-1 Sprint-1 Phase 1 (T1).

Projects dtxWiki content into an OKF-compatible navigational catalog
(``index.md``) and per-concept Markdown (YAML frontmatter + body). This is the
Tier-1 "navigational / index-first" retrieval surface: an agent reads the
index, selects concepts, and READs them — no embeddings required.

Visibility is filtered per-actor here (the catalog must not leak restricted
Library items or unreviewed drafts), reusing the single policy predicates from
``backend.policy.trust_boundaries`` so T1 and the future T2 vector search agree.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.context import (
    CONTEXT_PAGE_TYPES,
    ContextPage,
    ContextPageVersion,
)
from backend.models.library import LibraryItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import (
    actor_can_read_restricted_library,
)
from backend.services.context_visibility import visible_context_version_id
from backend.services.entity_ref import (  # canonical (G0-1)
    context_concept_path,
    library_concept_path,
)
from backend.utils.page_frontmatter import (
    derive_nav_description,
    parse_page_frontmatter,
)
from backend.utils.page_frontmatter import (
    render_frontmatter as _render_frontmatter,
)

OKF_SPEC_VERSION = "okf.v0.1"
RESTRICTED_TRUST_LEVELS = {"restricted"}
EXCLUDED_LIBRARY_LIFECYCLE_STATUSES = {"archived", "hidden"}
_DESCRIPTION_MAX = 200

logger = logging.getLogger("dtxwiki.okf")


# --- paths (a file path is the concept identity in OKF) ---------------------
# context_concept_path / library_concept_path are re-exported from
# backend.services.entity_ref (canonical since G0-1).


def parse_concept_path(path: str) -> tuple[str, str] | None:
    """Parse an OKF concept path into (section, identifier), or None."""
    cleaned = path.strip().strip("/")
    if not cleaned.endswith(".md"):
        return None
    cleaned = cleaned[: -len(".md")]
    section, _, identifier = cleaned.partition("/")
    if not identifier or section not in {"context", "library"}:
        return None
    return section, identifier


def resolve_workspace_slug(session: Session, workspace_id: UUID) -> str | None:
    """Return a workspace's slug from the CONTROL plane, or None if absent.

    Named for OKF resource paths, where it started, but it is the shared
    control-plane slug lookup: legacy upload authorization uses it too, to
    build the page route a relative reference must resolve against without
    joining the control-plane ``workspace`` table into a workspace query.
    """
    return session.scalar(select(Workspace.slug).where(Workspace.id == workspace_id))


# --- frontmatter projection -------------------------------------------------


def _nav_description_for(version: ContextPageVersion | None) -> str:
    if version is None:
        return ""
    if version.nav_description is not None:
        return version.nav_description
    return derive_nav_description(version.body_md) or ""


def project_context_frontmatter(
    page: ContextPage,
    version: ContextPageVersion | None,
    *,
    workspace_slug: str,
) -> dict[str, object]:
    """Map a Context page + version to OKF frontmatter (``type`` is required)."""
    return {
        "type": page.page_type,
        "title": page.title,
        "description": _nav_description_for(version),
        "resource": f"/w/{workspace_slug}/context/{page.slug}",
        "tags": list(page.tags or []),
        "aliases": list(page.aliases or []),
        # Never omit this signal. render_frontmatter drops None/empty values,
        # while callers may legitimately project a page before it has a version.
        "version_status": (
            version.version_status if version is not None else "unavailable"
        ),
        # the VISIBLE snapshot's instant, not page.updated_at: a viewer's
        # rendered concept (and thus the bundle hash) must not move when an
        # editor saves an invisible draft (3.3 — no draft-activity channel)
        "timestamp": version.created_at if version is not None else page.updated_at,
    }


_YAML_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~"}
_YAML_NUMERIC = re.compile(r"^[+-]?(\d[\d_]*\.?[\d_]*|\.\d[\d_]*)([eE][+-]?\d+)?$")
_YAML_UNSAFE_CHARS = set(":#,[]{}&*!|>'\"%@`\n\t")


def _yaml_scalar(text: str) -> str:
    """Render *text* as a YAML scalar valid in both block-mapping and
    flow-sequence context: emitted bare when unambiguous, double-quoted (with
    escaping) otherwise. Simple values stay unquoted to preserve the existing
    OKF frontmatter shape; values with ``:``, commas, brackets, hashes, leading
    indicators, etc. are quoted so realistic titles/tags produce valid YAML
    instead of a scanner error in downstream consumers (Codex F6 / merged I9)."""
    needs_quote = (
        text == ""
        or text != text.strip()
        or text[0] in "-?:"
        or text.lower() in _YAML_RESERVED
        or bool(_YAML_NUMERIC.match(text))
        or any(ch in _YAML_UNSAFE_CHARS for ch in text)
    )
    if not needs_quote:
        return text
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def render_frontmatter(fields: dict[str, object]) -> str:
    """Compatibility export for the shared page-frontmatter codec."""
    return _render_frontmatter(fields)


def render_context_concept(
    page: ContextPage,
    version: ContextPageVersion | None,
    *,
    workspace_slug: str,
) -> str:
    """Full OKF concept document: YAML frontmatter + Markdown body."""
    frontmatter = render_frontmatter(
        project_context_frontmatter(page, version, workspace_slug=workspace_slug)
    )
    # A-2: editor-saved bodies carry an embedded frontmatter block (the page
    # editor upserts one); strip it so the rendered concept has exactly ONE
    # frontmatter block — the OKF projection above.
    raw_body = version.body_md if version and version.body_md else ""
    body = parse_page_frontmatter(raw_body).body if raw_body else ""
    return f"{frontmatter}\n\n{body}".rstrip() + "\n"


# --- visibility (per-actor, reuses the single policy predicates) ------------


def _visible_context_version_id(
    page: ContextPage, actor: ActorContext | None
) -> tuple[UUID | None, bool]:
    """Return ``(version_id, is_draft)`` for the version the actor may see, or
    ``(None, False)`` when the actor may not see the page at all. Draft-capable actors
    see the draft head before the published version (Epic-1 §7.4)."""
    return visible_context_version_id(page, actor)


# --- index.md (progressive-disclosure catalog) ------------------------------


def build_workspace_index(
    session: Session,
    workspace_id: UUID,
    *,
    actor: ActorContext | None,
    workspace_slug: str,
    sections: tuple[str, ...] = ("context", "library"),
    node: str | None = None,
) -> str:
    """Render the workspace index, auto-selecting flat vs routing mode.

    Small workspaces (visible content nodes <= GET_INDEX_FLAT_MAX) keep the
    historical single-call flat catalog; larger ones (or an explicit
    ``node``) render fanout-bounded routing nodes (design §5.1). Raises
    IndexNodeNotFoundError for an unknown/invisible ``node``.
    """
    from backend.services.okf_index import (
        GET_INDEX_FLAT_MAX,
        build_routing_index,
        count_content_nodes,
    )
    from backend.services.tree_projection import TreeProjectionService

    projection = TreeProjectionService().load(
        session, workspace_id, actor, sections=sections
    )
    if node is None and count_content_nodes(projection) <= GET_INDEX_FLAT_MAX:
        return render_export_manifest(
            session,
            workspace_id,
            actor=actor,
            workspace_slug=workspace_slug,
            sections=sections,
        )
    return build_routing_index(
        session,
        workspace_id,
        actor=actor,
        workspace_slug=workspace_slug,
        sections=sections,
        node=node,
        projection=projection,
    )


def render_export_manifest(
    session: Session,
    workspace_id: UUID,
    *,
    actor: ActorContext | None,
    workspace_slug: str,
    sections: tuple[str, ...] = ("context", "library"),
) -> str:
    """Complete flat catalog — the export/bundle contract (never truncated).

    Navigation may fanout-cap its render; an export that drops entries is
    data loss, so bundles always use this renderer (B-6 contract split).
    """
    blocks: list[str] = [f"# {workspace_slug} — knowledge index", ""]
    if "context" in sections:
        blocks.append("## Context")
        blocks.extend(
            _context_index_lines(session, workspace_id, actor) or ["_(none)_"]
        )
        blocks.append("")
    if "library" in sections:
        blocks.append("## Library")
        blocks.extend(
            _library_index_lines(session, workspace_id, actor) or ["_(none)_"]
        )
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


def _context_index_lines(
    session: Session, workspace_id: UUID, actor: ActorContext | None
) -> list[str]:
    pages = session.scalars(
        select(ContextPage)
        .where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.lifecycle_status != "archived",
        )
        .order_by(ContextPage.title.asc())
    ).all()
    chosen: list[tuple[ContextPage, UUID, bool]] = []
    for page in pages:
        version_id, is_draft = _visible_context_version_id(page, actor)
        if version_id is not None:
            chosen.append((page, version_id, is_draft))
    version_ids = [vid for _, vid, _ in chosen]
    descriptions: dict[UUID, str | None] = {}
    null_rows: list[UUID] = []
    if version_ids:
        # Column-only read: bodies are loaded only for legacy NULL rows.
        for vid, nav_description in session.execute(
            select(ContextPageVersion.id, ContextPageVersion.nav_description).where(
                ContextPageVersion.id.in_(version_ids)
            )
        ).all():
            descriptions[vid] = nav_description
            if nav_description is None:
                null_rows.append(vid)
    if null_rows:
        for version in session.scalars(
            select(ContextPageVersion).where(ContextPageVersion.id.in_(null_rows))
        ).all():
            descriptions[version.id] = derive_nav_description(version.body_md)
    lines: list[str] = []
    for page, version_id, is_draft in chosen:
        description = descriptions.get(version_id) or ""
        suffix = " *(draft)*" if is_draft else ""
        path = context_concept_path(page.slug)
        if description:
            lines.append(f"* [{page.title}]({path}) — {description}{suffix}")
        else:
            lines.append(f"* [{page.title}]({path}){suffix}")
    return lines


def _library_index_lines(
    session: Session, workspace_id: UUID, actor: ActorContext | None
) -> list[str]:
    allow_restricted = actor_can_read_restricted_library(actor)
    items = session.scalars(
        select(LibraryItem)
        .where(LibraryItem.workspace_id == workspace_id)
        .order_by(LibraryItem.title.asc())
    ).all()
    lines: list[str] = []
    for item in items:
        if item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
            continue
        if item.trust_level in RESTRICTED_TRUST_LEVELS and not allow_restricted:
            continue
        title = item.title or item.uri
        lines.append(f"* [{title}]({library_concept_path(item.id)}) — {item.uri}")
    return lines


# --- read_concept resolution ------------------------------------------------


def resolve_context_concept(
    session: Session,
    workspace_id: UUID,
    *,
    slug: str,
    actor: ActorContext | None,
) -> tuple[ContextPage, ContextPageVersion] | None:
    """Resolve an OKF context path slug using the legacy tuple contract."""

    from backend.services.concept_resolution import (
        resolve_context_concept as resolve,
    )

    result = resolve(session, workspace_id, identifier=slug, actor=actor)
    if result.page is None or result.version is None:
        return None
    return result.page, result.version


# --- OKF bundle export (producer) -------------------------------------------


def export_workspace_bundle(
    session: Session,
    workspace_id: UUID,
    *,
    actor: ActorContext | None,
    workspace_slug: str,
) -> dict[str, str]:
    """Serialize a workspace's visible Context concepts as an OKF bundle
    (``path -> markdown``): an ``index.md`` catalog plus one
    ``context/<slug>.md`` per concept (frontmatter + body). Reuses the same
    per-actor visibility as the catalog, so an export never leaks restricted
    or unreviewed content. Round-trippable with a consumer (import)."""
    bundle: dict[str, str] = {
        "index.md": render_export_manifest(
            session,
            workspace_id,
            actor=actor,
            workspace_slug=workspace_slug,
            sections=("context",),
        )
    }
    pages = session.scalars(
        select(ContextPage)
        .where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.lifecycle_status != "archived",
        )
        .order_by(ContextPage.slug.asc())
    ).all()
    for page in pages:
        version_id, _is_draft = _visible_context_version_id(page, actor)
        if version_id is None:
            continue
        version = session.get(ContextPageVersion, version_id)
        bundle[f"context/{page.slug}.md"] = render_context_concept(
            page, version, workspace_slug=workspace_slug
        )
    return bundle


def resolve_library_concept(
    session: Session,
    workspace_id: UUID,
    *,
    identifier: str,
    actor: ActorContext | None,
) -> tuple[LibraryItem | None, str]:
    """Resolve a ``/library/<id>.md`` concept to its LibraryItem (A-8).

    Every gate returns ``not_found`` — absence must not disclose existence
    (design §3.3): archived/hidden lifecycle and restricted trust read the
    same as a missing id to unauthorized actors.
    """
    from backend.utils.uuid import parse_uuid_or_none

    item_id = parse_uuid_or_none(identifier)
    if item_id is None:
        return None, "not_found"
    item = session.get(LibraryItem, item_id)
    if item is None or item.workspace_id != workspace_id:
        return None, "not_found"
    if item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
        return None, "not_found"
    if item.trust_level in RESTRICTED_TRUST_LEVELS and not (
        actor_can_read_restricted_library(actor)
    ):
        return None, "not_found"
    return item, "found"


def render_library_concept(item: LibraryItem) -> tuple[str, dict[str, object]]:
    """OKF document for a Library source: extracted artifact text as body."""
    artifacts = []
    version = item.current_version
    if version is not None:
        artifacts = sorted(
            (a for a in version.artifacts if a.stage == "conversion"),
            key=lambda a: a.created_at,
        )
    body = "\n\n".join(
        artifact.conversion_detail.index_text
        for artifact in artifacts
        if artifact.conversion_detail.index_text
    )
    if not body:
        body = "_(no extracted text)_"
    frontmatter: dict[str, object] = {
        "type": "source",
        "title": item.title or item.uri,
        "resource": item.uri,
        "timestamp": item.updated_at,
    }
    rendered = render_frontmatter(frontmatter)
    return f"{rendered}\n\n{body}".rstrip() + "\n", frontmatter


def render_workspace_bundle_markdown(
    session: Session,
    workspace_id: UUID,
    *,
    actor: ActorContext | None,
    workspace_slug: str,
) -> tuple[str, str, datetime | None]:
    """Whole-workspace CAG bundle: index.md then each concept slug-asc,
    separated by file markers. Returns (markdown, content_hash,
    last_modified) — the hash is the client cache-invalidation key and
    what_changed is the delta signal (design 17.3/21.3).
    """
    import hashlib

    bundle = export_workspace_bundle(
        session, workspace_id, actor=actor, workspace_slug=workspace_slug
    )
    parts: list[str] = [bundle["index.md"]]
    for path in sorted(bundle):
        if path == "index.md":
            continue
        parts.append(f"<!-- dtxwiki:file {path} -->\n{bundle[path]}")
    markdown = "\n\n".join(parts)
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return (
        markdown,
        content_hash,
        workspace_published_last_modified(session, workspace_id),
    )


def workspace_published_last_modified(
    session: Session,
    workspace_id: UUID,
) -> datetime | None:
    """Latest PUBLISHED head instant of the workspace's live pages.

    Deliberately published-only (not actor/draft-scoped): it pairs with the
    what_changed delta signal, which reports published changes only, so a
    client reconciling "newer lastModified" with what_changed never loops on
    a draft edit that produced no delta. content_hash remains the precise,
    draft-inclusive cache key for editors. One aggregate query, no bodies.
    """
    return session.scalar(
        select(func.max(ContextPageVersion.created_at))
        .join(
            ContextPage,
            ContextPage.current_published_version_id == ContextPageVersion.id,
        )
        .where(ContextPage.workspace_id == workspace_id)
        .where(ContextPage.lifecycle_status != "archived")
    )


# --- OKF bundle import (consumer) -------------------------------------------


def _page_exists(page_service, workspace_slug: str, slug: str) -> bool:
    with page_service.session_factory() as session:
        return (
            session.scalar(
                select(ContextPage.id)
                .join(Workspace, ContextPage.workspace_id == Workspace.id)
                .where(Workspace.slug == workspace_slug, ContextPage.slug == slug)
                .limit(1)
            )
            is not None
        )


def import_okf_bundle(
    page_service,
    *,
    workspace_slug: str,
    bundle: dict[str, str],
    actor_id: str,
    actor_provenance: str,
    actor_kind: str = "human",
    default_page_type: str = "concept",
) -> list[str]:
    """Consume an OKF bundle into a workspace: each ``context/<slug>.md`` concept
    becomes a Context page via ``page_service.write_page``. Liberal consumption —
    an unknown OKF ``type`` falls back to ``default_page_type`` instead of
    rejecting the bundle. ``page_service`` is injected to avoid a service-import
    cycle. Returns the slugs that were newly created.

    Slugs that already exist are **skipped** (non-destructive, idempotent
    re-import — local edits are never clobbered). Each entry is written
    independently: one bad entry is recorded and skipped, never aborting the
    whole bundle or leaving it half-applied (each ``write_page`` is its own
    transaction).

    Authorization precondition: the caller MUST have already verified that
    ``actor_id`` may write (editor role) to ``workspace_slug``. This is a trusted
    bulk-import primitive and does not itself enforce workspace RBAC — wire it
    behind an endpoint that runs ``require_workspace_role`` (as the REST routes
    do) before invoking it.
    """
    imported: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for path in sorted(bundle):
        if path == "index.md" or not path.startswith("context/"):
            continue
        if not path.endswith(".md"):
            continue
        slug = path[len("context/") : -len(".md")]
        if not slug or "/" in slug:
            continue
        try:
            if _page_exists(page_service, workspace_slug, slug):
                skipped.append(slug)
                continue
            parsed = parse_page_frontmatter(bundle[path])
            title = parsed.title or slug
            page_type = parsed.page_type
            if page_type not in CONTEXT_PAGE_TYPES:
                page_type = default_page_type
            page_service.write_page(
                workspace_slug=workspace_slug,
                slug=slug,
                title=title,
                body_md=parsed.body,
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                page_type=page_type,
                tags=parsed.tags,
                aliases=parsed.aliases,
            )
            imported.append(slug)
        except Exception as exc:  # record and continue
            failed.append(slug)
            logger.warning("okf import: failed to import %r: %s", slug, exc)
    if skipped or failed:
        logger.info(
            "okf import into %s: %d imported, %d skipped (already exist), %d failed",
            workspace_slug,
            len(imported),
            len(skipped),
            len(failed),
        )
    return imported
