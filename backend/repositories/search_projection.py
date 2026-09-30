"""Workspace-bound row access for the rebuildable SQLite FTS5 projections.

The virtual tables are intentionally absent from SQLAlchemy metadata, so their
row operations use fixed SQL statements.  Keeping those statements behind this
workspace-bound repository prevents callers from omitting the tenant predicate
or accepting projection rows owned by a different workspace.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import Uuid, bindparam, text
from sqlalchemy.orm import Session

from backend.utils.uuid import parse_uuid_or_none

_DELETE_DOCUMENT_ENTITY = text(
    "DELETE FROM search_doc_fts "
    "WHERE workspace_id = :workspace_id AND source_kind = :source_kind "
    "AND entity_id = :entity_id"
)
_DELETE_DOCUMENT_VERSIONS = text(
    "DELETE FROM search_doc_fts "
    "WHERE workspace_id = :workspace_id AND source_kind = :source_kind "
    "AND version_id IN :version_ids"
).bindparams(bindparam("version_ids", expanding=True))
_INSERT_DOCUMENT = text(
    "INSERT INTO search_doc_fts "
    "(context_header, body, workspace_id, source_kind, entity_id, version_id, "
    "visibility_scope, category) VALUES "
    "(:context_header, :body, :workspace_id, :source_kind, :entity_id, "
    ":version_id, :visibility_scope, :category)"
)
_DELETE_PAGE_NAMES = text(
    "DELETE FROM page_name_trgm "
    "WHERE workspace_id = :workspace_id AND page_id = :page_id"
)
_INSERT_PAGE_NAME = text(
    "INSERT INTO page_name_trgm "
    "(name, workspace_id, page_id, source_version_id, visibility_scope, "
    "name_kind, name_norm) VALUES "
    "(:name, :workspace_id, :page_id, :source_version_id, :visibility_scope, "
    ":name_kind, :name_norm)"
)
_DELETE_ORPHAN_CLAIM_DOCUMENTS = text(
    "DELETE FROM search_doc_fts "
    "WHERE replace(search_doc_fts.workspace_id, '-', '') = :workspace_id "
    "AND source_kind = 'context_claim' AND NOT EXISTS ("
    "SELECT 1 FROM context_page_version "
    "WHERE context_page_version.workspace_id = :workspace_id "
    "AND CAST(context_page_version.id AS TEXT) = "
    "replace(search_doc_fts.version_id, '-', '')"
    ")"
).bindparams(bindparam("workspace_id", type_=Uuid(as_uuid=True)))
_DELETE_ORPHAN_ARTIFACT_DOCUMENTS = text(
    "DELETE FROM search_doc_fts "
    "WHERE replace(search_doc_fts.workspace_id, '-', '') = :workspace_id "
    "AND source_kind = 'library_artifact' AND NOT EXISTS ("
    "SELECT 1 FROM library_artifact "
    "WHERE library_artifact.workspace_id = :workspace_id "
    "AND CAST(library_artifact.id AS TEXT) = "
    "replace(search_doc_fts.entity_id, '-', '')"
    ")"
).bindparams(bindparam("workspace_id", type_=Uuid(as_uuid=True)))
_PURGE_DOCUMENTS = text("DELETE FROM search_doc_fts WHERE workspace_id = :workspace_id")
_PURGE_PAGE_NAMES = text(
    "DELETE FROM page_name_trgm WHERE workspace_id = :workspace_id"
)
_SEARCH_DOCUMENTS = text(
    """
    SELECT rowid, source_kind, entity_id, version_id, visibility_scope,
           category, bm25(search_doc_fts, 5.0, 1.0) AS rank,
           snippet(search_doc_fts, 1, '', '', ' ... ', 40) AS snippet
      FROM search_doc_fts
     WHERE search_doc_fts MATCH :match_query
       AND workspace_id = :workspace_id
       AND source_kind IN :source_kinds
       AND visibility_scope IN :visibility_scopes
       AND (
            :allow_sensitive = 1
            OR source_kind != 'agent_page'
            OR category NOT IN :sensitive_categories
       )
     ORDER BY rank ASC, rowid ASC
     LIMIT :take OFFSET :offset
    """
).bindparams(
    bindparam("source_kinds", expanding=True),
    bindparam("visibility_scopes", expanding=True),
    bindparam("sensitive_categories", expanding=True),
)
_SEARCH_PAGE_NAMES = text(
    """
    SELECT rowid, page_id, source_version_id, visibility_scope,
           name_kind, name_norm, name, bm25(page_name_trgm) AS rank
      FROM page_name_trgm
     WHERE page_name_trgm MATCH :match_query
       AND workspace_id = :workspace_id
       AND visibility_scope IN :visibility_scopes
     ORDER BY rank ASC, rowid ASC
     LIMIT :take
    """
).bindparams(bindparam("visibility_scopes", expanding=True))

# M-10 (260908 UI audit): FTS5's snippet() function extracts a verbatim
# substring of the indexed body -- Context/Library markdown syntax (heading
# `#`, list `-`, wikilink `[[...]]`, tables) leaked straight into the search
# result card, next to an unrelated result rendered as a plain sentence. These
# run BEFORE the whitespace/newline collapse below, while line starts are
# still real line starts, so the heading/list/blockquote patterns stay
# unambiguous (a run of collapsed spaces makes "start of list item" and "a
# hyphen in prose" indistinguishable).
_MD_TABLE_ROW_SEPARATOR = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$",
    re.MULTILINE,
)
_MD_HEADING_MARKER = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_MD_LIST_MARKER = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+", re.MULTILINE)
_MD_BLOCKQUOTE_MARKER = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_MD_WIKILINK = re.compile(
    r"\[\[\s*([^\]|#\n]+?)(?:#[^\]|\n]+)?(?:\|([^\]\n]+))?\s*\]\]"
)
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\([^)\n]*\)")
_MD_EMPHASIS = re.compile(r"(\*\*\*|\*\*|\*|___|__|_)([^\s*_](?:[^\n]*?[^\s*_])?)\1")
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def _plain_text_snippet(raw: str) -> str:
    """Reduce an FTS5 snippet fragment to prose, for display only."""
    text_value = _MD_TABLE_ROW_SEPARATOR.sub(" ", raw)
    text_value = _MD_HEADING_MARKER.sub("", text_value)
    text_value = _MD_LIST_MARKER.sub("", text_value)
    text_value = _MD_BLOCKQUOTE_MARKER.sub("", text_value)
    text_value = _MD_WIKILINK.sub(
        lambda m: (m.group(2) or m.group(1)).strip(), text_value
    )
    text_value = _MD_LINK.sub(lambda m: m.group(1), text_value)
    text_value = _MD_EMPHASIS.sub(lambda m: m.group(2), text_value)
    text_value = _MD_INLINE_CODE.sub(lambda m: m.group(1), text_value)
    return text_value.replace("|", " ")


class _DocumentInput(Protocol):
    workspace_id: UUID
    source_kind: str
    entity_id: UUID
    version_id: UUID | None
    visibility_scope: str
    category: str
    context_header: str
    body: str


class _PageNameInput(Protocol):
    workspace_id: UUID
    page_id: UUID
    source_version_id: UUID
    visibility_scope: str
    name_kind: str
    name_norm: str
    name: str


@dataclass(frozen=True, slots=True)
class FtsDocumentHit:
    rowid: int
    source_kind: str
    entity_id: UUID
    version_id: UUID | None
    visibility_scope: str
    category: str
    rank: float
    snippet: str


@dataclass(frozen=True, slots=True)
class PageNameTrigramHit:
    rowid: int
    page_id: UUID
    source_version_id: UUID
    name_kind: str
    name_norm: str
    name: str
    rank: float


class WorkspaceFts5ProjectionRepository:
    """Fixed FTS5 row operations bound to one session and workspace."""

    __slots__ = ("_session", "_workspace_id")

    def __init__(self, session: Session, workspace_id: UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id

    def delete_document_entity(self, *, source_kind: str, entity_id: UUID) -> None:
        self._session.execute(
            _DELETE_DOCUMENT_ENTITY,
            {
                "workspace_id": str(self._workspace_id),
                "source_kind": source_kind,
                "entity_id": str(entity_id),
            },
        )

    def delete_document_versions(
        self,
        *,
        source_kind: str,
        version_ids: Iterable[UUID],
    ) -> None:
        resolved_ids = [str(version_id) for version_id in version_ids]
        if not resolved_ids:
            return
        self._session.execute(
            _DELETE_DOCUMENT_VERSIONS,
            {
                "workspace_id": str(self._workspace_id),
                "source_kind": source_kind,
                "version_ids": resolved_ids,
            },
        )

    def insert_documents(self, documents: Iterable[_DocumentInput]) -> None:
        resolved_documents = list(documents)
        for document in resolved_documents:
            if document.workspace_id != self._workspace_id:
                raise ValueError("FTS document belongs to a different workspace")
        if not resolved_documents:
            return
        self._session.execute(
            _INSERT_DOCUMENT,
            [
                {
                    "context_header": document.context_header,
                    "body": document.body,
                    "workspace_id": str(self._workspace_id),
                    "source_kind": document.source_kind,
                    "entity_id": str(document.entity_id),
                    "version_id": str(document.version_id or ""),
                    "visibility_scope": document.visibility_scope,
                    "category": document.category,
                }
                for document in resolved_documents
            ],
        )

    def replace_page_names(
        self,
        *,
        page_id: UUID,
        rows: Iterable[_PageNameInput],
    ) -> int:
        resolved_rows = list(rows)
        for row in resolved_rows:
            if row.workspace_id != self._workspace_id:
                raise ValueError("page-name row belongs to a different workspace")
            if row.page_id != page_id:
                raise ValueError("page-name row belongs to a different page")
        self._session.execute(
            _DELETE_PAGE_NAMES,
            {"workspace_id": str(self._workspace_id), "page_id": str(page_id)},
        )
        if resolved_rows:
            self._session.execute(
                _INSERT_PAGE_NAME,
                [
                    {
                        "name": row.name,
                        "workspace_id": str(self._workspace_id),
                        "page_id": str(page_id),
                        "source_version_id": str(row.source_version_id),
                        "visibility_scope": row.visibility_scope,
                        "name_kind": row.name_kind,
                        "name_norm": row.name_norm,
                    }
                    for row in resolved_rows
                ],
            )
        return len(resolved_rows)

    def delete_orphan_claim_documents(self) -> None:
        self._session.execute(
            _DELETE_ORPHAN_CLAIM_DOCUMENTS,
            {"workspace_id": self._workspace_id},
        )

    def delete_orphan_artifact_documents(self) -> None:
        self._session.execute(
            _DELETE_ORPHAN_ARTIFACT_DOCUMENTS,
            {"workspace_id": self._workspace_id},
        )

    def purge_all(self) -> None:
        parameters = {"workspace_id": str(self._workspace_id)}
        self._session.execute(_PURGE_DOCUMENTS, parameters)
        self._session.execute(_PURGE_PAGE_NAMES, parameters)

    def search_documents(
        self,
        *,
        match_query: str,
        source_kinds: Sequence[str],
        visibility_scopes: Sequence[str],
        allow_sensitive: bool,
        sensitive_categories: Sequence[str],
        take: int,
        offset: int,
    ) -> tuple[list[FtsDocumentHit], int]:
        rows = (
            self._session.execute(
                _SEARCH_DOCUMENTS,
                {
                    "match_query": match_query,
                    "workspace_id": str(self._workspace_id),
                    "source_kinds": list(source_kinds),
                    "visibility_scopes": list(visibility_scopes),
                    "allow_sensitive": int(allow_sensitive),
                    "sensitive_categories": list(sensitive_categories),
                    "take": take,
                    "offset": offset,
                },
            )
            .mappings()
            .all()
        )
        hits: list[FtsDocumentHit] = []
        for row in rows:
            entity_id = parse_uuid_or_none(str(row["entity_id"]))
            if entity_id is None:
                continue
            hits.append(
                FtsDocumentHit(
                    rowid=int(row["rowid"]),
                    source_kind=str(row["source_kind"]),
                    entity_id=entity_id,
                    version_id=parse_uuid_or_none(str(row["version_id"] or "")),
                    visibility_scope=str(row["visibility_scope"]),
                    category=str(row["category"] or ""),
                    rank=float(row["rank"]),
                    snippet=" ".join(
                        _plain_text_snippet(str(row["snippet"] or "")).split()
                    )[:240],
                )
            )
        return hits, len(rows)

    def search_page_names(
        self,
        *,
        match_query: str,
        visibility_scopes: Sequence[str],
        take: int,
    ) -> list[PageNameTrigramHit]:
        rows = self._session.execute(
            _SEARCH_PAGE_NAMES,
            {
                "match_query": match_query,
                "workspace_id": str(self._workspace_id),
                "visibility_scopes": list(visibility_scopes),
                "take": take,
            },
        ).mappings()
        hits: list[PageNameTrigramHit] = []
        for row in rows:
            page_id = parse_uuid_or_none(str(row["page_id"]))
            version_id = parse_uuid_or_none(str(row["source_version_id"]))
            if page_id is None or version_id is None:
                continue
            hits.append(
                PageNameTrigramHit(
                    rowid=int(row["rowid"]),
                    page_id=page_id,
                    source_version_id=version_id,
                    name_kind=str(row["name_kind"]),
                    name_norm=str(row["name_norm"]),
                    name=str(row["name"]),
                    rank=float(row["rank"]),
                )
            )
        return hits


__all__ = [
    "FtsDocumentHit",
    "PageNameTrigramHit",
    "WorkspaceFts5ProjectionRepository",
]
