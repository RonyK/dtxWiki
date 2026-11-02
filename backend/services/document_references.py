"""Bibliography commands, final publication and actor-filtered projections.

The authored metadata and inline markers have separate owners. Publication
materializes their union once; reads never backfill stored relationships.
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import Text, cast, or_
from sqlalchemy.orm import selectinload

from backend.domain.document_reference import DocumentRef, ResolvedDocumentTarget
from backend.models.agent import AgentPage
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.models.document_reference import (
    ContextCitationPublication,
    ContextDocumentReference,
    ContextPublishedCitation,
    target_columns,
)
from backend.models.library import (
    LibraryArtifact,
    LibraryItem,
    LibraryItemTombstone,
    LibraryItemVersion,
)
from backend.models.workspace import Workspace
from backend.repositories.document_reference import (
    copy_document_references as copy_document_references,
)
from backend.repositories.document_reference import (
    document_reference_schema_available,
)
from backend.repositories.workspace_scoped import get_in_workspace, select_in_workspace
from backend.schemas.document_reference import ReferenceView
from backend.services.actor_visible_read import (
    actor_can_read_library_trust_level,
    agent_page_visible,
    context_page_visible,
    library_item_visible,
)
from backend.services.citation_parser import (
    CitationSyntaxError,
    extract_inline_citations,
)
from backend.services.context_links import (
    classify,
    configured_self_origins,
    format_stable_url,
)
from backend.services.context_visibility import visible_context_version_id
from backend.services.document_reference_locations import reference_location_warnings
from backend.services.document_reference_titles import context_reference_titles
from backend.services.index_refs import parse_index_ref
from backend.services.public_page_exposure import valid_public_exposed_version_id
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError
from backend.services.tree_locations import visible_target_locations
from backend.services.workspace_urls import (
    agent_page_url,
    library_item_url,
    public_context_page_url,
)
from backend.utils.page_frontmatter import parse_page_frontmatter

MAX_DOCUMENT_REFERENCES = 500


def _read_reference_locations(session, workspace_id, references, actor):
    targets = {
        reference.key
        for reference in references
        if reference.target_kind in {"context_page", "agent_page"}
    }
    paths = visible_target_locations(session, workspace_id, targets, actor)
    return {key: " / ".join(path) for key, path in paths.items() if path}


def _public_reference_offsets(references, body):
    """Project stored Unicode offsets into the exact anonymous body payload."""
    parsed = parse_page_frontmatter(body)
    if not parsed.has_frontmatter:
        return references
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    prefix_length = len(normalized) - len(parsed.body)
    crlf_ends = [match.end() for match in re.finditer("\r\n", body)]

    def project(offset):
        return offset - bisect_right(crlf_ends, offset) - prefix_length

    return [
        reference.model_copy(
            update={
                "occurrences": [
                    occurrence.model_copy(
                        update={
                            "start": project(occurrence.start),
                            "end": project(occurrence.end),
                        }
                    )
                    for occurrence in reference.occurrences
                ]
            }
        )
        for reference in references
    ]


def _publication_record(session, version):
    if not document_reference_schema_available(session):
        return None
    return session.scalar(
        select_in_workspace(
            ContextCitationPublication,
            workspace_id=version.workspace_id,
            conditions=(ContextCitationPublication.version_id == version.id,),
        )
    )


def _ref(value) -> DocumentRef:
    return DocumentRef(value.target_kind, value.target_id)


def document_ref_from_href(
    href: str, workspace_id: UUID, *, self_origins: frozenset[str] | None = None
) -> DocumentRef:
    """Use the ordinary link grammars, never title/hash inference."""
    from backend.services.context_link_targets import (
        _parse_workspace_navigation_reference,
    )

    origins = configured_self_origins() if self_origins is None else self_origins
    locator = classify(href, origins)
    if locator.workspace_id is not None and locator.workspace_id != workspace_id:
        raise ReadNotFoundError("citation document not found")
    if locator.page_id is not None and locator.kind in {
        "stable_full",
        "stable_short",
        "canonical_route",
    }:
        return DocumentRef("context_page", locator.page_id)
    index = parse_index_ref(href)
    if index and index.kind in {"context", "library", "agent"}:
        return DocumentRef(
            {
                "context": "context_page",
                "library": "library_item",
                "agent": "agent_page",
            }[index.kind],
            index.target_id,
        )
    route = _parse_workspace_navigation_reference(href, self_origins=origins)
    if route:
        section, kind, target_id, target_workspace = route
        if (
            target_workspace == workspace_id
            and target_id
            and (section, kind) in {("library", "item"), ("agent", "page")}
        ):
            return DocumentRef(
                "library_item" if section == "library" else "agent_page", target_id
            )
    raise ReadNotFoundError(
        "citation document not found; use its permanent document address"
    )


def resolve_document_targets(session, workspace_id, refs, actor, *, public=False):
    """Batch identities with the shared reader predicates and URL builders."""
    unique = {_ref(ref).key: _ref(ref) for ref in refs}
    models = {
        "context_page": ContextPage,
        "library_item": LibraryItem,
        "agent_page": AgentPage,
    }
    rows = {}
    for kind, model in models.items():
        ids = [rid for rkind, rid in unique if rkind == kind]
        for start in range(0, len(ids), 400):
            statement = select_in_workspace(
                model,
                workspace_id=workspace_id,
                conditions=(model.id.in_(ids[start : start + 400]),),
            )
            if model is ContextPage:
                # The shared visibility predicate inspects private draft
                # ownership. Load just those attributes in bounded batches,
                # never one full draft body per referenced page.
                columns = (
                    ContextPageVersion.id,
                    ContextPageVersion.workspace_id,
                    ContextPageVersion.page_id,
                    ContextPageVersion.parent_version_id,
                    ContextPageVersion.version_status,
                    ContextPageVersion.created_by_actor_kind,
                    ContextPageVersion.created_by_actor_id,
                )
                statement = statement.options(
                    selectinload(ContextPage.current_version).load_only(*columns),
                    selectinload(ContextPage.latest_draft_version).load_only(*columns),
                )
            for row in session.scalars(statement):
                rows[kind, row.id] = row
    workspace = session.get(Workspace, workspace_id) if public else None
    context_pages = {
        row.id: row
        for (kind, _target_id), row in rows.items()
        if kind == "context_page"
    }
    selected_versions = {}
    for page_id, page in context_pages.items():
        if public:
            version_id = (
                valid_public_exposed_version_id(page, workspace) if workspace else None
            )
        elif context_page_visible(page, actor, workspace_id=workspace_id):
            version_id = visible_context_version_id(page, actor)[0]
        else:
            version_id = None
        if version_id is not None:
            selected_versions[page_id] = version_id
    context_titles = context_reference_titles(
        session,
        workspace_id=workspace_id,
        pages=context_pages,
        visible_versions=selected_versions,
        public=public,
    )
    result = {}
    for key, reference in unique.items():
        row = rows.get(key)
        title = href = location = None
        state, eligible = "unavailable", False
        if reference.target_kind == "context_page":
            visible = row is not None and (
                row.id in context_titles
                if public
                else context_page_visible(row, actor, workspace_id=workspace_id)
            )
            if visible:
                title = context_titles.get(row.id, row.slug)
                href = (
                    public_context_page_url(workspace.slug, row.slug)
                    if public
                    else format_stable_url(workspace_id, row.id)
                )
                eligible = (
                    row.current_published_version_id is not None
                    and row.lifecycle_status != "archived"
                )
                state = "available" if eligible else "unpublished"
        elif reference.target_kind == "library_item":
            if not public and library_item_visible(
                row, actor, workspace_id=workspace_id
            ):
                title, href, location = (
                    row.title,
                    library_item_url(workspace_id, row.id),
                    row.uri,
                )
                state, eligible = "available", True
        elif reference.target_kind == "agent_page":
            if not public and agent_page_visible(row, actor, workspace_id=workspace_id):
                title, href = row.title, agent_page_url(workspace_id, row.id)
                eligible = row.current_active_version_id is not None
                state = "available" if eligible else "unpublished"
        result[key] = ResolvedDocumentTarget(
            reference, title, href, state, location, eligible
        )
    return result


def authored_document_references(session, version):
    if not document_reference_schema_available(session):
        return []
    return list(
        session.scalars(
            select_in_workspace(
                ContextDocumentReference,
                workspace_id=version.workspace_id,
                conditions=(ContextDocumentReference.owner_version_id == version.id,),
            ).order_by(ContextDocumentReference.sort_order, ContextDocumentReference.id)
        )
    )


def legacy_citation_owner_condition(session, version, *, page=None):
    """The version-owned evidence set, including eligible old page owners."""
    claim_ids = select_in_workspace(
        ContextClaim,
        workspace_id=version.workspace_id,
        conditions=(ContextClaim.page_version_id == version.id,),
    ).with_only_columns(ContextClaim.id)
    condition = or_(
        (ContextCitation.target_kind == "context_claim")
        & ContextCitation.target_id.in_(claim_ids),
        (ContextCitation.target_kind != "context_claim")
        & (ContextCitation.target_id == version.id),
    )
    if page is None:
        page = get_in_workspace(
            session,
            ContextPage,
            workspace_id=version.workspace_id,
            resource_id=version.page_id,
        )
    if (
        page
        and page.current_published_version_id == version.id
        and _publication_record(session, version) is None
    ):
        condition = or_(
            condition,
            (ContextCitation.target_kind == "context_page")
            & (ContextCitation.target_id == page.id),
        )
    return condition


def legacy_document_references(session, version):
    """Return original evidence rows and their document identity, without writes."""
    condition = legacy_citation_owner_condition(session, version)
    rows = list(
        session.scalars(
            select_in_workspace(
                ContextCitation,
                workspace_id=version.workspace_id,
                conditions=(condition,),
            ).order_by(ContextCitation.created_at, ContextCitation.id)
        )
    )
    return _legacy_reference_targets(session, version.workspace_id, rows)


def _legacy_reference_targets(session, workspace_id, rows):
    """Decode already owner-scoped legacy evidence in bounded target batches."""
    context_ids = {
        row.cited_context_page_version_id
        for row in rows
        if row.cited_context_page_version_id
    }
    library_ids = {
        row.library_item_version_id for row in rows if row.library_item_version_id
    }
    artifact_ids = {row.library_artifact_id for row in rows if row.library_artifact_id}
    context = (
        dict(
            session.execute(
                select_in_workspace(
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    conditions=(ContextPageVersion.id.in_(context_ids),),
                ).with_only_columns(ContextPageVersion.id, ContextPageVersion.page_id)
            ).all()
        )
        if context_ids
        else {}
    )
    artifacts = (
        dict(
            session.execute(
                select_in_workspace(
                    LibraryArtifact,
                    workspace_id=workspace_id,
                    conditions=(LibraryArtifact.id.in_(artifact_ids),),
                ).with_only_columns(
                    LibraryArtifact.id, LibraryArtifact.library_item_version_id
                )
            ).all()
        )
        if artifact_ids
        else {}
    )
    library_ids.update(artifacts.values())
    library = (
        dict(
            session.execute(
                select_in_workspace(
                    LibraryItemVersion,
                    workspace_id=workspace_id,
                    conditions=(LibraryItemVersion.id.in_(library_ids),),
                ).with_only_columns(
                    LibraryItemVersion.id, LibraryItemVersion.library_item_id
                )
            ).all()
        )
        if library_ids
        else {}
    )
    result = []
    for row in rows:
        if (target := context.get(row.cited_context_page_version_id)) is not None:
            result.append((row, DocumentRef("context_page", target)))
        elif (
            target := library.get(
                row.library_item_version_id or artifacts.get(row.library_artifact_id)
            )
        ) is not None:
            result.append((row, DocumentRef("library_item", target)))
    return result


def deleted_document_references(session, version):
    """Read source-version-owned deletion snapshots without exposing their target."""
    rows = session.scalars(
        select_in_workspace(
            LibraryItemTombstone,
            workspace_id=version.workspace_id,
            conditions=(
                cast(LibraryItemTombstone.citations, Text).contains(str(version.id)),
            ),
        )
    )
    result = {}
    for tombstone in rows:
        for entry in tombstone.citations or []:
            if entry.get("citation_type") != "document_reference" or entry.get(
                "target_id"
            ) != str(version.id):
                continue
            key = ("library_item", tombstone.library_item_id)
            old = result.get(key)
            if old is None or entry.get("registration_origin") == "published":
                result[key] = (tombstone, entry)
    return result


def _reference_digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def document_references_hash(session, version) -> str:
    if version is None:
        return _reference_digest({"references": [], "legacy": [], "deleted": []})
    values = [
        (row.target_kind, str(row.target_id), row.note, row.sort_order)
        for row in authored_document_references(session, version)
    ]
    legacy = [
        (
            row.citation_type,
            ref.target_kind,
            str(ref.target_id),
            row.location,
            row.snippet,
            row.verification_status,
        )
        for row, ref in legacy_document_references(session, version)
    ]
    deleted = sorted(
        (str(stone.library_item_id), value.get("note"), value.get("sort_order", 0))
        for stone, value in deleted_document_references(session, version).values()
        if value.get("metadata_present", value.get("registration_origin") == "metadata")
    )
    legacy.sort(key=lambda value: json.dumps(value, sort_keys=True))
    return _reference_digest(
        {"references": values, "legacy": legacy, "deleted": deleted}
    )


def replace_authored_document_references(
    session, *, version, rows, self_origins=None
) -> None:
    """Replace one editing version's authored set after a semantic merge.

    Values are live metadata rows or ``(tombstone, selected_snapshot)`` pairs
    from another version in this workspace. Inline and legacy evidence retain
    their separate ownership; unchanged live registration IDs remain stable.
    """
    if version.version_status not in {"editing", "draft"}:
        raise ValueError("Published bibliography metadata is immutable")
    desired = {}
    for value in rows:
        source = value if isinstance(value, ContextDocumentReference) else value[0]
        if source.workspace_id != version.workspace_id:
            raise ValueError("References can only be copied within their workspace")
        key = (
            value.reference.key
            if isinstance(value, ContextDocumentReference)
            else ("library_item", source.library_item_id)
        )
        if key in desired:
            raise ValueError("A document can only appear once in the bibliography")
        desired[key] = value

    if len(desired) > MAX_DOCUMENT_REFERENCES:
        raise ValueError("A document supports at most 500 references")
    current = {
        row.reference.key: row
        for row in authored_document_references(session, version)
    }
    deleted = deleted_document_references(session, version)
    try:
        occurrences = extract_inline_citations(version.body_md or "")
    except CitationSyntaxError:
        occurrences = ()
    inline = defaultdict(list)
    for occurrence in occurrences:
        try:
            key = document_ref_from_href(
                occurrence.href, version.workspace_id, self_origins=self_origins
            ).key
        except ReadNotFoundError:
            continue
        inline[key].append(asdict(occurrence))

    for key, row in current.items():
        if key not in desired or not isinstance(desired[key], ContextDocumentReference):
            session.delete(row)
    for order, (key, value) in enumerate(desired.items()):
        if not isinstance(value, ContextDocumentReference):
            continue
        target = current.get(key)
        if target is None:
            target = ContextDocumentReference(
                workspace_id=version.workspace_id,
                owner_version_id=version.id,
                **target_columns(value.reference),
                created_by_actor_kind=value.created_by_actor_kind,
                created_by_actor_id=value.created_by_actor_id,
            )
            session.add(target)
        target.note, target.sort_order = value.note, order

    snapshots = dict(deleted)
    snapshots.update(
        {key: value for key, value in desired.items() if isinstance(value, tuple)}
    )
    positions = {key: order for order, key in enumerate(desired)}
    for key, (stone, snapshot) in snapshots.items():
        selected = desired.get(key)
        metadata_present = isinstance(selected, tuple)
        keep_inline = bool(inline.get(key))
        retained = [
            item
            for item in stone.citations or []
            if not (
                item.get("citation_type") == "document_reference"
                and item.get("target_id") == str(version.id)
            )
        ]
        if metadata_present or keep_inline:
            previous = deleted.get(key)
            old_id = (
                previous[1].get("reference_id") or previous[1].get("citation_id")
                if previous is not None
                else None
            )
            reference_id = old_id or str(uuid4())
            retained.append(
                {
                    **snapshot,
                    "target_id": str(version.id),
                    "reference_id": reference_id,
                    "citation_id": reference_id,
                    "registration_origin": "metadata" if metadata_present else "inline",
                    "metadata_present": metadata_present,
                    "note": snapshot.get("note") if metadata_present else None,
                    "sort_order": positions.get(key, snapshot.get("sort_order", 0)),
                    "occurrences": inline.get(key, []),
                    "inline_count": len(inline.get(key, [])),
                }
            )
        stone.citations = retained
    session.flush()


def _input_union(session, version, *, strict, self_origins=None):
    entries = {}
    try:
        occurrences = extract_inline_citations(version.body_md or "")
    except CitationSyntaxError:
        if strict:
            raise
        occurrences = ()
    for occurrence in occurrences:
        try:
            ref = document_ref_from_href(
                occurrence.href, version.workspace_id, self_origins=self_origins
            )
        except ReadNotFoundError:
            if strict:
                raise
            continue
        entry = entries.setdefault(
            ref.key,
            {
                "ref": ref,
                "occurrences": [],
                "metadata_present": False,
                "legacy_count": 0,
                "note": None,
            },
        )
        entry["occurrences"].append(asdict(occurrence))
    for row in authored_document_references(session, version):
        entry = entries.setdefault(
            row.reference.key,
            {
                "ref": row.reference,
                "occurrences": [],
                "metadata_present": False,
                "legacy_count": 0,
                "note": None,
            },
        )
        entry.update(metadata_present=True, note=row.note, metadata_id=row.id)
    for legacy_row, ref in legacy_document_references(session, version):
        entry = entries.setdefault(
            ref.key,
            {
                "ref": ref,
                "occurrences": [],
                "metadata_present": False,
                "legacy_count": 0,
                "note": None,
            },
        )
        entry["legacy_count"] += 1
        entry.setdefault("metadata_id", legacy_row.id)
    for key, (_stone, value) in deleted_document_references(session, version).items():
        reference = DocumentRef(key[0], key[1])
        if (
            not value.get(
                "metadata_present", value.get("registration_origin") == "metadata"
            )
            and key not in entries
        ):
            continue
        entry = entries.setdefault(
            key,
            {
                "ref": reference,
                "occurrences": [],
                "metadata_present": False,
                "legacy_count": 0,
                "note": None,
            },
        )
        if value.get(
            "metadata_present", value.get("registration_origin") == "metadata"
        ):
            entry.update(
                metadata_present=True,
                note=value.get("note"),
                metadata_id=value.get("reference_id") or value.get("citation_id"),
            )
    if len(entries) > MAX_DOCUMENT_REFERENCES:
        raise CitationSyntaxError("A document supports at most 500 unique references")
    return entries


def _require_reference_policy(session, version, references):
    from backend.services.citation_policy import normalized_citation_policy

    workspace = session.get(Workspace, version.workspace_id)
    if workspace is None:
        raise ReadNotFoundError("workspace not found")
    allowed = normalized_citation_policy(workspace.citation_policy)[
        "accepted_document_kinds"
    ]
    if any(reference.target_kind not in allowed for reference in references):
        raise RejectedByPolicyError(
            "This document kind is not allowed in the workspace bibliography"
        )


def _ancestor_registration_keys(session, *, version, reference_ids, current_keys):
    """Map pre-edit opaque IDs to registrations copied into this editing row.

    Opening an edit assigns fresh registration IDs. A removal selected on the
    published References panel may still carry the source ID, including when
    its target is hidden. Prove source ownership through the actual parent
    chain and only remove the matching registration already owned by this
    edit. Neither a sibling branch nor another page can grant this authority.
    """
    if not reference_ids or not current_keys:
        return {}
    parents = dict(
        session.execute(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=version.workspace_id,
                conditions=(ContextPageVersion.page_id == version.page_id,),
            ).with_only_columns(
                ContextPageVersion.id, ContextPageVersion.parent_version_id
            )
        ).all()
    )
    ancestors = set()
    cursor = version.parent_version_id
    while cursor is not None:
        if cursor in ancestors or cursor not in parents:
            raise ReadNotFoundError("reference registration not found")
        ancestors.add(cursor)
        cursor = parents[cursor]
    if not ancestors:
        return {}
    remaining = set(reference_ids)
    result = {}
    pending = list(reference_ids)
    for start in range(0, len(pending), 400):
        ids = pending[start : start + 400]
        authored = session.scalars(
            select_in_workspace(
                ContextDocumentReference,
                workspace_id=version.workspace_id,
                conditions=(ContextDocumentReference.id.in_(ids),),
            )
        )
        for row in authored:
            if row.owner_version_id in ancestors and row.reference.key in current_keys:
                result[row.id] = row.reference.key
                remaining.discard(row.id)
    legacy = []
    pending = list(remaining)
    for start in range(0, len(pending), 400):
        legacy.extend(
            session.scalars(
                select_in_workspace(
                    ContextCitation,
                    workspace_id=version.workspace_id,
                    conditions=(ContextCitation.id.in_(pending[start : start + 400]),),
                )
            )
        )
    claim_ids = {row.target_id for row in legacy if row.target_kind == "context_claim"}
    claim_owners = (
        dict(
            session.execute(
                select_in_workspace(
                    ContextClaim,
                    workspace_id=version.workspace_id,
                    conditions=(ContextClaim.id.in_(claim_ids),),
                ).with_only_columns(ContextClaim.id, ContextClaim.page_version_id)
            ).all()
        )
        if claim_ids
        else {}
    )
    page = get_in_workspace(
        session,
        ContextPage,
        workspace_id=version.workspace_id,
        resource_id=version.page_id,
    )
    legacy = [
        row
        for row in legacy
        if (
            claim_owners.get(row.target_id) in ancestors
            if row.target_kind == "context_claim"
            else row.target_id in ancestors
            or (
                row.target_kind == "context_page"
                and row.target_id == version.page_id
                and page is not None
                and page.current_published_version_id in ancestors
            )
        )
    ]
    decoded = _legacy_reference_targets(session, version.workspace_id, legacy)
    for row, reference in decoded:
        if reference.key in current_keys:
            result[row.id] = reference.key
            remaining.discard(row.id)
    ancestor_ids = {str(value) for value in ancestors}
    pending = list(remaining)
    for start in range(0, len(pending), 400):
        tombstones = session.scalars(
            select_in_workspace(
                LibraryItemTombstone,
                workspace_id=version.workspace_id,
                conditions=(
                    or_(
                        *(
                            cast(LibraryItemTombstone.citations, Text).contains(
                                str(value)
                            )
                            for value in pending[start : start + 400]
                        )
                    ),
                ),
            )
        )
        for stone in tombstones:
            key = ("library_item", stone.library_item_id)
            if key not in current_keys:
                continue
            for entry in stone.citations or []:
                if entry.get("citation_type") != "document_reference" or entry.get(
                    "target_id"
                ) not in ancestor_ids:
                    continue
                try:
                    opaque_id = UUID(
                        str(entry.get("reference_id") or entry.get("citation_id"))
                    )
                except (ValueError, TypeError):
                    continue
                if opaque_id in remaining:
                    result[opaque_id] = key
    return result


def apply_reference_delta(
    session, *, version, add, remove, actor, self_origins=None
):
    _require_reference_policy(session, version, add)
    additions = {_ref(value).key: value for value in add}
    existing_rows = authored_document_references(session, version)
    existing = {row.reference.key: row for row in existing_rows}
    legacy_rows = legacy_document_references(session, version)
    owned_ids = {row.id: row.reference.key for row in existing_rows}
    owned_ids.update({row.id: ref.key for row, ref in legacy_rows})
    deleted = deleted_document_references(session, version)
    for key, (_stone, value) in deleted.items():
        reference_id = value.get("reference_id") or value.get("citation_id")
        if reference_id is not None:
            owned_ids[UUID(str(reference_id))] = key
    missing_ids = {
        value.reference_id
        for value in remove
        if getattr(value, "reference_id", None) is not None
        and value.reference_id not in owned_ids
    }
    owned_ids.update(
        _ancestor_registration_keys(
            session,
            version=version,
            reference_ids=missing_ids,
            current_keys=set(owned_ids.values()),
        )
    )
    removals = {}
    for value in remove:
        reference_id = getattr(value, "reference_id", None)
        if reference_id is not None:
            key = owned_ids.get(reference_id)
            if key is None:
                raise ReadNotFoundError("reference registration not found")
            removals[key] = {"reference_id": reference_id}
        else:
            key = _ref(value).key
            removals[key] = {"target_kind": key[0], "target_id": key[1]}
    if additions.keys() & removals.keys():
        raise ValueError("A document cannot be added and removed in the same request")
    resolved = resolve_document_targets(session, version.workspace_id, add, actor)
    if any(value.state == "unavailable" for value in resolved.values()):
        raise ReadNotFoundError("citation document not found")
    if (
        len((existing.keys() - removals.keys()) | additions.keys())
        > MAX_DOCUMENT_REFERENCES
    ):
        raise ValueError("A document supports at most 500 references")
    added, removed, unchanged = [], [], []
    inline_by_key = defaultdict(list)
    try:
        input_occurrences = extract_inline_citations(version.body_md or "")
    except CitationSyntaxError:
        input_occurrences = ()
    for occurrence in input_occurrences:
        try:
            reference = document_ref_from_href(
                occurrence.href, version.workspace_id, self_origins=self_origins
            )
        except ReadNotFoundError:
            continue
        inline_by_key[reference.key].append(asdict(occurrence))
    for key, output in removals.items():
        row = existing.get(key)
        found = False
        if row:
            session.delete(row)
            found = True
        for legacy, reference in legacy_rows:
            if reference.key == key:
                session.delete(legacy)
                found = True
        if key in deleted:
            stone, _entry = deleted[key]
            if not _entry.get(
                "metadata_present", _entry.get("registration_origin") == "metadata"
            ):
                (removed if found else unchanged).append(output)
                continue
            retained = [
                item
                for item in stone.citations or []
                if not (
                    item.get("citation_type") == "document_reference"
                    and item.get("target_id") == str(version.id)
                )
            ]
            if inline_by_key.get(key):
                retained.append(
                    {
                        **_entry,
                        "metadata_present": False,
                        "note": None,
                        "registration_origin": "inline",
                        "occurrences": inline_by_key[key],
                    }
                )
            stone.citations = retained
            found = True
        (removed if found else unchanged).append(output)
    order = max((row.sort_order for row in existing_rows), default=-1) + 1
    for key, value in additions.items():
        row = existing.get(key)
        note = getattr(value, "note", None)
        if row is not None and row.note == note:
            unchanged.append({"target_kind": key[0], "target_id": key[1], "note": note})
            continue
        if row is None:
            row = ContextDocumentReference(
                workspace_id=version.workspace_id,
                owner_version_id=version.id,
                **target_columns(_ref(value)),
                sort_order=order,
                created_by_actor_kind=getattr(actor, "actor_kind", None),
                created_by_actor_id=getattr(actor, "actor_id", None),
            )
            order += 1
            session.add(row)
        row.note = note
        added.append({"target_kind": key[0], "target_id": key[1], "note": note})
    session.flush()
    counts = {
        f"{kind}:{target_id}": len(values)
        for (kind, target_id), values in inline_by_key.items()
    }
    private_counts = {}
    for key, output in removals.items():
        if "reference_id" in output:
            raw_key = f"{key[0]}:{key[1]}"
            private_counts[f"reference_id:{output['reference_id']}"] = counts.pop(
                raw_key, 0
            )
    counts.update(private_counts)
    return {
        "changed": bool(added or removed),
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
        "remaining_inline_occurrences": dict(counts),
    }


def publish_document_references(session, *, version, actor, self_origins=None) -> None:
    """Materialize once inside the caller's final publish transaction."""
    if not document_reference_schema_available(session):
        # Old migration guards write ordinary links through today's writer.
        # New citation syntax must never be silently published on that schema.
        if extract_inline_citations(version.body_md or ""):
            raise RejectedByPolicyError(
                "Upgrade the database before publishing inline citations"
            )
        return
    if _publication_record(session, version) is not None:
        return
    entries = _input_union(session, version, strict=True, self_origins=self_origins)
    _require_reference_policy(
        session,
        version,
        [
            entry["ref"]
            for entry in entries.values()
            if entry["metadata_present"] or entry["occurrences"]
        ],
    )
    resolved = resolve_document_targets(
        session,
        version.workspace_id,
        [entry["ref"] for entry in entries.values()],
        actor,
    )
    deleted = deleted_document_references(session, version)
    for key, (stone, _snapshot) in list(deleted.items()):
        if key not in entries:
            stone.citations = [
                value
                for value in stone.citations or []
                if not (
                    value.get("citation_type") == "document_reference"
                    and value.get("target_id") == str(version.id)
                )
            ]
            del deleted[key]
    for key, entry in entries.items():
        if key in deleted:
            continue
        if (entry["metadata_present"] or entry["occurrences"]) and resolved[
            key
        ].state == "unavailable":
            raise ReadNotFoundError("citation document not found")
    fingerprint = hashlib.sha256(
        (
            (version.body_md or "")
            + "\0"
            + document_references_hash(session, version)
            + "\0v1"
        ).encode()
    ).hexdigest()
    session.add(
        ContextCitationPublication(
            version_id=version.id,
            workspace_id=version.workspace_id,
            fingerprint=fingerprint,
            format_revision=1,
        )
    )
    for order, entry in enumerate(entries.values()):
        if entry["ref"].key in deleted:
            continue
        session.add(
            ContextPublishedCitation(
                workspace_id=version.workspace_id,
                source_version_id=version.id,
                **target_columns(entry["ref"]),
                sort_order=order,
                metadata_present=entry["metadata_present"],
                inline_count=len(entry["occurrences"]),
                legacy_count=entry["legacy_count"],
                occurrences=entry["occurrences"],
                note=entry["note"],
            )
        )
    session.flush()


def read_document_references(
    session, *, version, actor=None, public=False, self_origins=None
) -> list[ReferenceView]:
    marker = _publication_record(session, version)
    if marker is not None:
        rows = session.scalars(
            select_in_workspace(
                ContextPublishedCitation,
                workspace_id=version.workspace_id,
                conditions=(ContextPublishedCitation.source_version_id == version.id,),
            ).order_by(ContextPublishedCitation.sort_order)
        ).all()
        registration_ids = {}
        if not public and any(row.metadata_present or row.legacy_count for row in rows):
            registration_ids.update(
                (reference.key, row.id)
                for row, reference in legacy_document_references(session, version)
            )
            registration_ids.update(
                (row.reference.key, row.id)
                for row in authored_document_references(session, version)
            )
        entries = [
            {
                "ref": row.reference,
                "occurrences": row.occurrences,
                "metadata_present": row.metadata_present,
                "legacy_count": row.legacy_count,
                "metadata_id": registration_ids.get(row.reference.key),
                "note": row.note,
                "sort_order": row.sort_order,
            }
            for row in rows
        ]
    elif version.version_status in {"published_snapshot", "superseded_snapshot"}:
        # Legacy reads preserve only evidence already stored in that version.
        # Parsing old published bodies here would silently invent relations.
        entries = []
        seen = set()
        for legacy, ref in legacy_document_references(session, version):
            if ref.key not in seen:
                entries.append(
                    {
                        "ref": ref,
                        "occurrences": [],
                        "metadata_present": True,
                        "metadata_id": legacy.id if not public else None,
                        "note": None,
                    }
                )
                seen.add(ref.key)
    else:
        try:
            entries = list(
                _input_union(
                    session, version, strict=False, self_origins=self_origins
                ).values()
            )
        except CitationSyntaxError:
            entries = []
    deleted = deleted_document_references(session, version)
    known = {entry["ref"].key: entry for entry in entries}
    for key, (_stone, value) in deleted.items():
        if (
            version.version_status in {"editing", "draft"}
            and not value.get("metadata_present", False)
            and key not in known
        ):
            continue
        entry = known.get(key)
        if entry is None:
            entry = {
                "ref": DocumentRef(key[0], key[1]),
                "occurrences": value.get("occurrences") or [],
                "metadata_present": value.get("metadata_present", False),
                "note": value.get("note"),
                "sort_order": value.get("sort_order", len(entries)),
            }
            entries.append(entry)
            known[key] = entry
        if not public:
            entry["metadata_id"] = value.get("reference_id") or value.get("citation_id")
    if marker is not None:
        entries.sort(key=lambda entry: entry.get("sort_order", len(entries)))
    resolved = resolve_document_targets(
        session,
        version.workspace_id,
        [entry["ref"] for entry in entries],
        actor,
        public=public,
    )
    location_warnings = reference_location_warnings(
        session,
        workspace_id=version.workspace_id,
        entries=entries,
        resolved=resolved,
        actor=actor,
    )
    locations = (
        {}
        if public
        else _read_reference_locations(
            session,
            version.workspace_id,
            [
                target.reference
                for target in resolved.values()
                if target.state != "unavailable"
            ],
            actor,
        )
    )
    result = []
    for number, entry in enumerate(entries, 1):
        target = resolved[entry["ref"].key]
        if entry["ref"].key in deleted:
            stone, _snapshot = deleted[entry["ref"].key]
            if not public and actor_can_read_library_trust_level(
                stone.trust_level_snapshot, actor
            ):
                target = ResolvedDocumentTarget(
                    entry["ref"], stone.title, None, "deleted"
                )
        disclosed = target.state in {"available", "unpublished"}
        href = target.href
        if href and entry["occurrences"]:
            fragment = urlsplit(entry["occurrences"][0]["href"] or "").fragment
            if fragment:
                href += "#" + fragment
        result.append(
            ReferenceView(
                target_kind=entry["ref"].target_kind,
                target_id=(entry["ref"].target_id if disclosed else None),
                metadata_id=entry.get("metadata_id") if not public else None,
                number=number,
                title=target.title,
                href=href,
                state=target.state,
                note=entry["note"] if target.state != "unavailable" else None,
                inline_count=len(entry["occurrences"]),
                metadata_present=(
                    entry["metadata_present"] or bool(entry.get("legacy_count"))
                ),
                location=locations.get(entry["ref"].key, target.location),
                location_warning=location_warnings.get(entry["ref"].key),
                occurrences=(
                    entry["occurrences"]
                    if disclosed
                    else [
                        {**occurrence, "href": None}
                        for occurrence in entry["occurrences"]
                    ]
                ),
            )
        )
    if public:
        return _public_reference_offsets(result, version.body_md or "")
    return result


def preview_document_references(
    session, *, workspace_id, body, actor, self_origins=None
):
    """Resolve only citations explicitly requested by the editor, without writes."""
    from backend.services.read_errors import ReadValidationError

    try:
        occurrences = extract_inline_citations(body)
    except CitationSyntaxError as error:
        raise ReadValidationError(str(error)) from error
    entries = {}
    for occurrence in occurrences:
        try:
            reference = document_ref_from_href(
                occurrence.href,
                workspace_id,
                self_origins=self_origins,
            )
        except ReadNotFoundError:
            continue
        entries.setdefault(reference.key, {"reference": reference, "occurrences": []})[
            "occurrences"
        ].append(asdict(occurrence))
    if len(entries) > MAX_DOCUMENT_REFERENCES:
        raise ReadValidationError("A reference preview supports at most 500 documents")
    resolved = resolve_document_targets(
        session,
        workspace_id,
        [value["reference"] for value in entries.values()],
        actor,
    )
    location_warnings = reference_location_warnings(
        session,
        workspace_id=workspace_id,
        entries=[{**value, "ref": value["reference"]} for value in entries.values()],
        resolved=resolved,
        actor=actor,
    )
    locations = _read_reference_locations(
        session,
        workspace_id,
        [
            target.reference
            for target in resolved.values()
            if target.state != "unavailable"
        ],
        actor,
    )
    result = []
    for number, (key, entry) in enumerate(entries.items(), 1):
        target = resolved[key]
        href = target.href
        if href and entry["occurrences"]:
            fragment = urlsplit(entry["occurrences"][0]["href"] or "").fragment
            if fragment:
                href += "#" + fragment
        result.append(
            ReferenceView(
                target_kind=key[0],
                target_id=key[1] if target.state != "unavailable" else None,
                number=number,
                title=target.title,
                href=href,
                state=target.state,
                inline_count=len(entry["occurrences"]),
                location=locations.get(key, target.location),
                location_warning=location_warnings.get(key),
                # Locator echo belongs to this request, never a hidden stored path.
                occurrences=entry["occurrences"],
            )
        )
    return result
