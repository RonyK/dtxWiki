"""Canonical document-source composition for symbolic search projections."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.context import ContextClaim, ContextPage, ContextPageVersion
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.tree import WorkspaceTreeNode
from backend.services.entity_ref import context_concept_path, library_concept_path
from backend.services.library_canonical import (
    get_canonical_library_version,
    get_effective_library_markdown,
    list_canonical_library_artifacts,
)
from backend.services.page_service import title_from_markdown_heading
from backend.utils.page_frontmatter import (
    derive_nav_description,
    parse_page_frontmatter,
)


@dataclass(frozen=True, slots=True)
class DocSource:
    workspace_id: UUID
    source_kind: str
    entity_id: UUID
    version_id: UUID | None
    visibility_scope: str
    category: str
    context_header: str
    body: str


def compose_context_doc_source(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
    visibility_scope: str,
) -> DocSource:
    parsed = parse_page_frontmatter(version.body_md)
    breadcrumb = _visible_breadcrumb(
        session,
        workspace_id=page.workspace_id,
        target_kind="context_page",
        target_id=page.id,
    )
    header = _header(
        title=page.title,
        aliases=page.aliases or [],
        path=context_concept_path(page.slug),
        breadcrumb=breadcrumb,
        nav_description=(
            version.nav_description or derive_nav_description(version.body_md)
        ),
        first_heading=title_from_markdown_heading(parsed.body),
    )
    return DocSource(
        workspace_id=page.workspace_id,
        source_kind="context_page",
        entity_id=page.id,
        version_id=version.id,
        visibility_scope=visibility_scope,
        category=page.page_type,
        context_header=header,
        body=parsed.body,
    )


def compose_claim_doc_source(
    session: Session,
    *,
    claim: ContextClaim,
    page: ContextPage,
    version: ContextPageVersion,
    visibility_scope: str,
) -> DocSource:
    page_source = compose_context_doc_source(
        session,
        page=page,
        version=version,
        visibility_scope=visibility_scope,
    )
    return DocSource(
        workspace_id=page.workspace_id,
        source_kind="context_claim",
        entity_id=claim.id,
        version_id=version.id,
        visibility_scope=visibility_scope,
        category=claim.claim_type,
        context_header=(
            f"{page_source.context_header}\nClaim type: {claim.claim_type}"
        ),
        body=claim.claim_text,
    )


def compose_context_page_doc_sources(
    session: Session, page: ContextPage
) -> list[DocSource]:
    documents: list[DocSource] = []
    for version_id, scope in _context_live_versions(session, page):
        version = session.get(ContextPageVersion, version_id)
        if version is None:
            continue
        documents.append(
            compose_context_doc_source(
                session,
                page=page,
                version=version,
                visibility_scope=scope,
            )
        )
        claims = session.scalars(
            select(ContextClaim).where(ContextClaim.page_version_id == version.id)
        ).all()
        documents.extend(
            compose_claim_doc_source(
                session,
                claim=claim,
                page=page,
                version=version,
                visibility_scope=scope,
            )
            for claim in claims
        )
    return documents


def compose_library_item_doc_source(
    *, item: LibraryItem, version: LibraryItemVersion | None
) -> DocSource:
    title = item.title or item.uri
    return DocSource(
        workspace_id=item.workspace_id,
        source_kind="library_item",
        entity_id=item.id,
        version_id=version.id if version is not None else None,
        visibility_scope="published",
        category=item.connector_type,
        context_header=_header(
            title=title,
            path=library_concept_path(item.id),
            first_heading=None,
            aliases=[],
            breadcrumb=[],
            nav_description=None,
        ),
        body=f"{item.uri}\n{title}",
    )


def compose_library_artifact_doc_source(
    *,
    item: LibraryItem,
    version: LibraryItemVersion,
    artifact: LibraryArtifact,
    body_override: str | None = None,
) -> DocSource:
    title = item.title or item.uri
    return DocSource(
        workspace_id=item.workspace_id,
        source_kind="library_artifact",
        entity_id=artifact.id,
        version_id=version.id,
        visibility_scope="published",
        category=artifact.kind,
        context_header=(
            f"Title: {title}\nPath: {library_concept_path(item.id)}\n"
            f"Artifact: {artifact.kind}"
        ),
        body=body_override
        if body_override is not None
        else artifact.conversion_detail.index_text or "",
    )


def compose_library_doc_sources(session: Session, item: LibraryItem) -> list[DocSource]:
    version = get_canonical_library_version(session, item)
    documents = [compose_library_item_doc_source(item=item, version=version)]
    if version is None:
        return documents
    artifacts = list_canonical_library_artifacts(session, version)
    effective = get_effective_library_markdown(session, version)
    for artifact in artifacts:
        body_override = (
            effective.body_markdown
            if effective is not None
            and effective.overlay is not None
            and effective.base_artifact.id == artifact.id
            else None
        )
        body = (
            body_override
            if body_override is not None
            else artifact.conversion_detail.index_text or ""
        )
        if not body.strip():
            continue
        documents.append(
            compose_library_artifact_doc_source(
                item=item,
                version=version,
                artifact=artifact,
                body_override=body_override,
            )
        )
    return documents


def compose_agent_doc_source(
    *,
    page: AgentPage,
    version: AgentPageVersion,
    visibility_scope: str,
) -> DocSource:
    return DocSource(
        workspace_id=page.workspace_id,
        source_kind="agent_page",
        entity_id=page.id,
        version_id=version.id,
        visibility_scope=visibility_scope,
        category=page.category,
        context_header=(
            f"Title: {page.title}\nSlug: {page.slug}\nCategory: {page.category}"
        ),
        body=version.body_markdown,
    )


def compose_agent_page_doc_sources(
    session: Session, page: AgentPage
) -> list[DocSource]:
    documents: list[DocSource] = []
    if page.current_active_version_id is not None:
        active = session.get(AgentPageVersion, page.current_active_version_id)
        if active is not None:
            documents.append(
                compose_agent_doc_source(
                    page=page,
                    version=active,
                    visibility_scope="published",
                )
            )
    if (
        page.latest_draft_version_id is not None
        and page.latest_draft_version_id != page.current_active_version_id
    ):
        draft = session.get(AgentPageVersion, page.latest_draft_version_id)
        if draft is not None:
            documents.append(
                compose_agent_doc_source(
                    page=page,
                    version=draft,
                    visibility_scope="draft",
                )
            )
    return documents


def _context_live_versions(
    session: Session, page: ContextPage
) -> list[tuple[UUID, str]]:
    """Which (version_id, "published"/"draft") pairs feed FTS5 doc sources.

    Same fix as `PageService._name_index_live_versions`: a page-first
    creation can have `current_version_id` name a private "editing" row
    directly, with `latest_draft_version_id` staying `None`,
    so the old id-comparison-only classification (still just an id
    comparison here, with no `version_status` check) would tag that row
    "draft" and feed its full body/claim text into the FTS index under that
    scope. `retrieval/keyword.py`'s `_hydrate_context_pages` currently
    cross-checks every FTS hit's `version_id` against the live, per-actor
    `visible_search_version_ids` (context_visibility.py) before surfacing
    it, so a misclassified row here has not been shown reachable as an
    actual search disclosure -- but that is a property of today's one
    consumer, not of this function, so it is fixed the same way rather than
    left to depend on that consumer never changing.
    """

    versions: list[tuple[UUID, str]] = []
    if page.current_published_version_id is not None:
        versions.append((page.current_published_version_id, "published"))
    draft_id = page.latest_draft_version_id
    if (
        draft_id is None
        and page.current_version_id is not None
        and page.current_version_id != page.current_published_version_id
    ):
        draft_id = page.current_version_id
    if draft_id is not None and draft_id != page.current_published_version_id:
        draft = session.get(ContextPageVersion, draft_id)
        if draft is not None and draft.version_status == "draft":
            versions.append((draft.id, "draft"))
    return versions


def _visible_breadcrumb(
    session: Session,
    *,
    workspace_id: UUID,
    target_kind: str,
    target_id: UUID,
) -> list[str]:
    node = session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.target_kind == target_kind,
            WorkspaceTreeNode.target_id == target_id,
            WorkspaceTreeNode.primary_placement.is_(True),
            WorkspaceTreeNode.visibility_status == "visible",
        )
    )
    if node is None:
        return []
    path = [node.title]
    current = node
    while current.parent_id is not None:
        parent = session.get(WorkspaceTreeNode, current.parent_id)
        if (
            parent is None
            or parent.workspace_id != workspace_id
            or parent.visibility_status != "visible"
        ):
            return []
        path.append(parent.title)
        current = parent
    return list(reversed(path))


def _header(
    *,
    title: str,
    aliases: list[str],
    path: str,
    breadcrumb: list[str],
    nav_description: str | None,
    first_heading: str | None,
) -> str:
    fields = [f"Title: {title}", f"Path: {path}"]
    if aliases:
        fields.append(f"Aliases: {', '.join(aliases)}")
    if breadcrumb:
        fields.append(f"Breadcrumb: {' / '.join(breadcrumb)}")
    if nav_description:
        fields.append(f"Summary: {nav_description}")
    if first_heading:
        fields.append(f"Heading: {first_heading}")
    return "\n".join(fields)
