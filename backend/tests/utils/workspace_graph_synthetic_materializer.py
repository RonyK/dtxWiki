"""Chunked persistence for large deterministic Workspace Graph corpora."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam
from sqlalchemy.orm import Session

from backend.models.context import (
    ContextCitation,
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
)
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.tests.utils.workspace_graph_fixture_identity import (
    fixture_time,
    fixture_uuid,
)
from backend.tests.utils.workspace_graph_synthetic import (
    PlannedRelation,
    SyntheticCorpusPlan,
    context_title,
)
from backend.utils.content_hash import content_hash as calculate_content_hash

_INSERT_CHUNK_SIZE = 2_000


@dataclass(frozen=True, slots=True)
class MaterializedSyntheticCorpus:
    """Identifiers and display metadata emitted by the bulk materializer."""

    name: str
    workspace_id: UUID
    context_ids: tuple[UUID, ...]
    context_version_ids: tuple[UUID, ...]
    library_ids: tuple[UUID, ...]
    library_version_ids: tuple[UUID, ...]
    folder_ids: tuple[UUID, ...]
    relation_ids: tuple[UUID, ...]
    restricted_library_ids: tuple[UUID, ...]
    context_slugs: dict[UUID, str]
    library_uris: dict[UUID, str]
    primary_folder_labels: dict[UUID, str]


class SyntheticCorpusMaterializer:
    """Persist one scale corpus without retaining ORM rows or flushing per row."""

    def __init__(self, session: Session, plan: SyntheticCorpusPlan) -> None:
        self.session = session
        self.plan = plan
        key = plan.corpus_key
        self.workspace_id = fixture_uuid(key, "workspace")
        self.context_ids = tuple(
            fixture_uuid(key, "context", index)
            for index in range(plan.context_count)
        )
        self.context_version_ids = tuple(
            fixture_uuid(key, "context-version", index)
            for index in range(plan.context_count)
        )
        self.library_ids = tuple(
            fixture_uuid(key, "library", index)
            for index in range(plan.library_count)
        )
        self.library_version_ids = tuple(
            fixture_uuid(key, "library-version", index)
            for index in range(plan.library_count)
        )
        self.context_folder_ids = tuple(
            fixture_uuid(key, "folder", "context", index)
            for index in range(plan.context_folder_count)
        )
        self.library_folder_ids = tuple(
            fixture_uuid(key, "folder", "library", index)
            for index in range(plan.library_folder_count)
        )

    def materialize(self) -> MaterializedSyntheticCorpus:
        self._insert_workspace()
        self._insert_folders()
        self._insert_context_documents()
        self._insert_library_documents()
        self._insert_placements()
        self._insert_relations()

        context_slugs = {
            page_id: f"doc-{index:06d}"
            for index, page_id in enumerate(self.context_ids)
        }
        library_uris = {
            item_id: self._library_uri(index)
            for index, item_id in enumerate(self.library_ids)
        }
        context_folder_labels = tuple(
            f"Area {index:04d}" for index in range(self.plan.context_folder_count)
        )
        library_folder_labels = tuple(
            f"Sources {index:04d}" for index in range(self.plan.library_folder_count)
        )
        primary_folder_labels = {
            page_id: context_folder_labels[
                self.plan.context_folder_indices[index]
            ]
            for index, page_id in enumerate(self.context_ids)
        }
        primary_folder_labels.update(
            {
                item_id: library_folder_labels[
                    index % self.plan.library_folder_count
                ]
                for index, item_id in enumerate(self.library_ids)
            }
        )
        restricted_indices = set(self.plan.restricted_library_indices)
        return MaterializedSyntheticCorpus(
            name=self.plan.corpus_key,
            workspace_id=self.workspace_id,
            context_ids=self.context_ids,
            context_version_ids=self.context_version_ids,
            library_ids=self.library_ids,
            library_version_ids=self.library_version_ids,
            folder_ids=self.context_folder_ids + self.library_folder_ids,
            relation_ids=tuple(
                fixture_uuid(self.plan.corpus_key, "relation", index)
                for index in range(len(self.plan.relations))
            ),
            restricted_library_ids=tuple(
                item_id
                for index, item_id in enumerate(self.library_ids)
                if index in restricted_indices
            ),
            context_slugs=context_slugs,
            library_uris=library_uris,
            primary_folder_labels=primary_folder_labels,
        )

    def _insert_workspace(self) -> None:
        key = self.plan.corpus_key
        self.session.add(
            Workspace(
                id=self.workspace_id,
                slug=f"workspace-graph-{key}",
                title=self.plan.title,
                name=self.plan.title,
                description=f"Deterministic {key} acceptance corpus",
                visibility="private",
                join_policy="invite_only",
                created_by_actor_id=fixture_uuid(key, "creator"),
                owner_actor_id=fixture_uuid(key, "owner"),
                # Benchmarks measure a workspace whose owner switched the
                # Knowledge Map on in Settings.
                workspace_graph_enabled=True,
                created_at=fixture_time(),
                updated_at=fixture_time(),
            )
        )
        self.session.flush()

    def _insert_folders(self) -> None:
        key = self.plan.corpus_key
        rows = (
            {
                "id": folder_id,
                "workspace_id": self.workspace_id,
                "section": section,
                "node_kind": "folder",
                "parent_id": None,
                "sort_key": f"{index:04d}",
                "title": title,
                "slug": "-".join(title.casefold().split()),
                "target_kind": None,
                "target_id": None,
                "is_system_node": False,
                "can_have_children": True,
                "primary_placement": True,
                "visibility_status": "visible",
                "nav_summary": None,
                "created_by_actor_type": "human",
                "created_by_actor_id": fixture_uuid(key, "author"),
                "created_at": fixture_time(2_000 + index),
                "updated_at": fixture_time(2_000 + index),
            }
            for section, folder_ids, title_prefix in (
                ("context", self.context_folder_ids, "Area"),
                ("library", self.library_folder_ids, "Sources"),
            )
            for index, folder_id in enumerate(folder_ids)
            for title in (f"{title_prefix} {index:04d}",)
        )
        _execute_in_chunks(self.session, WorkspaceTreeNode.__table__.insert(), rows)

    def _insert_context_documents(self) -> None:
        key = self.plan.corpus_key
        links_by_source: dict[int, list[str]] = {}
        if self.plan.profile == "sparse":
            for link in self.plan.body_links:
                if link.syntax == "wikilink":
                    rendered = f"[[doc-{link.target_index:06d}]]"
                else:
                    target_id = self.context_ids[link.target_index]
                    rendered = f"[Related context](/context?target={target_id})"
                links_by_source.setdefault(link.source_index, []).append(rendered)

        page_rows = (
            {
                "id": page_id,
                "workspace_id": self.workspace_id,
                "slug": f"doc-{index:06d}",
                "title": context_title(index),
                "current_version_id": None,
                "page_type": "concept",
                "tags": [],
                "aliases": [],
                "lifecycle_status": "published",
                "current_published_version_id": None,
                "is_public": False,
                "public_exposure_scope": None,
                "public_exposed_version_id": None,
                "latest_draft_version_id": None,
                "stale_candidate": False,
                "created_from_ask_session_id": None,
                "placement_parent_node_id": None,
                "placement_reason": None,
                "placement_confidence": None,
                "created_by_actor_type": "human",
                "created_by_actor_id": fixture_uuid(key, "author"),
                "created_at": fixture_time(index),
                "updated_at": fixture_time(index),
            }
            for index, page_id in enumerate(self.context_ids)
        )
        _execute_in_chunks(self.session, ContextPage.__table__.insert(), page_rows)

        def version_rows() -> Iterable[dict[str, Any]]:
            for index, version_id in enumerate(self.context_version_ids):
                title = context_title(index)
                body = f"# {title}\n\nDeterministic workspace graph fixture."
                rendered_links = links_by_source.get(index)
                if rendered_links:
                    body = f"{body}\n\n" + "\n".join(rendered_links)
                yield {
                    "id": version_id,
                    "workspace_id": self.workspace_id,
                    "page_id": self.context_ids[index],
                    "parent_version_id": None,
                    "version_no": 1,
                    "body_md": body,
                    "rendering_profile": "gfm_mermaid",
                    "content_hash": calculate_content_hash(body),
                    "graph_status": "linear",
                    "actor_type": "human",
                    "actor_id": None,
                    "actor_display_name": None,
                    "change_set_id": None,
                    "version_status": "published_snapshot",
                    "editing_revision": 1,
                    "last_saved_at": fixture_time(index),
                    "nav_description": None,
                    "block_map": None,
                    "reconcile_manifest": None,
                    "merged_into_version_id": None,
                    "created_by_actor_kind": "human",
                    "created_by_actor_id": str(fixture_uuid(key, "author")),
                    "created_at": fixture_time(index),
                }

        _execute_in_chunks(
            self.session,
            ContextPageVersion.__table__.insert(),
            version_rows(),
        )
        head_update = (
            ContextPage.__table__.update()
            .where(ContextPage.__table__.c.id == bindparam("_page_id"))
            .values(
                current_version_id=bindparam("_version_id"),
                current_published_version_id=bindparam("_version_id"),
                updated_at=bindparam("_updated_at"),
            )
        )
        _execute_in_chunks(
            self.session,
            head_update,
            (
                {
                    "_page_id": page_id,
                    "_version_id": self.context_version_ids[index],
                    "_updated_at": fixture_time(index),
                }
                for index, page_id in enumerate(self.context_ids)
            ),
        )

    def _insert_library_documents(self) -> None:
        key = self.plan.corpus_key
        restricted_indices = set(self.plan.restricted_library_indices)
        item_rows = (
            {
                "id": item_id,
                "workspace_id": self.workspace_id,
                "connector_type": "url",
                "format_hint": "html",
                "uri": self._library_uri(index),
                "title": f"Source {index:06d}",
                "metadata": {},
                "trust_level": (
                    "restricted" if index in restricted_indices else "trusted"
                ),
                "lifecycle_status": "ready",
                "current_version_id": None,
                "current_ready_version_id": None,
                "current_preview_version_id": None,
                "created_by_actor_type": "human",
                "created_by_actor_id": fixture_uuid(key, "author"),
                "created_at": fixture_time(1_000 + index),
                "updated_at": fixture_time(1_000 + index),
            }
            for index, item_id in enumerate(self.library_ids)
        )
        _execute_in_chunks(self.session, LibraryItem.__table__.insert(), item_rows)

        version_rows = (
            {
                "id": version_id,
                "workspace_id": self.workspace_id,
                "library_item_id": self.library_ids[index],
                "version_no": 1,
                "content_hash": f"library-{key}-{index:04d}",
                "fetched_at": fixture_time(1_000 + index),
                "raw_storage_ref": f"fixture://{key}/library/{index}",
                "processing_status": "ready",
                "extraction_summary": "Deterministic fixture source",
                "preview_status": "unavailable",
                "updated_at": fixture_time(1_000 + index),
                "created_at": fixture_time(1_000 + index),
            }
            for index, version_id in enumerate(self.library_version_ids)
        )
        _execute_in_chunks(
            self.session,
            LibraryItemVersion.__table__.insert(),
            version_rows,
        )
        head_update = (
            LibraryItem.__table__.update()
            .where(LibraryItem.__table__.c.id == bindparam("_item_id"))
            .values(
                current_version_id=bindparam("_version_id"),
                updated_at=bindparam("_updated_at"),
            )
        )
        _execute_in_chunks(
            self.session,
            head_update,
            (
                {
                    "_item_id": item_id,
                    "_version_id": self.library_version_ids[index],
                    "_updated_at": fixture_time(1_000 + index),
                }
                for index, item_id in enumerate(self.library_ids)
            ),
        )

    def _insert_placements(self) -> None:
        key = self.plan.corpus_key
        context_rows = (
            {
                "id": fixture_uuid(
                    key,
                    "placement",
                    "context",
                    page_id,
                    "primary",
                    index,
                ),
                "workspace_id": self.workspace_id,
                "section": "context",
                "node_kind": "page_ref",
                "parent_id": self.context_folder_ids[
                    self.plan.context_folder_indices[index]
                ],
                "sort_key": f"{index:06d}",
                "title": context_title(index),
                "slug": f"doc-{index:06d}",
                "target_kind": "context_page",
                "target_id": page_id,
                "is_system_node": False,
                "can_have_children": False,
                "primary_placement": True,
                "visibility_status": "visible",
                "nav_summary": None,
                "created_by_actor_type": "human",
                "created_by_actor_id": fixture_uuid(key, "author"),
                "created_at": fixture_time(3_000 + index),
                "updated_at": fixture_time(3_000 + index),
            }
            for index, page_id in enumerate(self.context_ids)
        )
        _execute_in_chunks(
            self.session,
            WorkspaceTreeNode.__table__.insert(),
            context_rows,
        )

        library_rows = (
            {
                "id": fixture_uuid(
                    key,
                    "placement",
                    "library",
                    item_id,
                    "primary",
                    self.plan.context_count + index,
                ),
                "workspace_id": self.workspace_id,
                "section": "library",
                "node_kind": "library_item_ref",
                "parent_id": self.library_folder_ids[
                    index % self.plan.library_folder_count
                ],
                "sort_key": f"{self.plan.context_count + index:06d}",
                "title": f"Source {index:06d}",
                "slug": f"library-{str(item_id)[:8]}",
                "target_kind": "library_item",
                "target_id": item_id,
                "is_system_node": False,
                "can_have_children": False,
                "primary_placement": True,
                "visibility_status": "visible",
                "nav_summary": None,
                "created_by_actor_type": "human",
                "created_by_actor_id": fixture_uuid(key, "author"),
                "created_at": fixture_time(3_000 + self.plan.context_count + index),
                "updated_at": fixture_time(3_000 + self.plan.context_count + index),
            }
            for index, item_id in enumerate(self.library_ids)
        )
        _execute_in_chunks(
            self.session,
            WorkspaceTreeNode.__table__.insert(),
            library_rows,
        )

    def _insert_relations(self) -> None:
        _execute_in_chunks(
            self.session,
            PageLink.__table__.insert(),
            self._page_link_rows(),
        )
        _execute_in_chunks(
            self.session,
            ContextCitation.__table__.insert(),
            self._citation_rows(),
        )
        _execute_in_chunks(
            self.session,
            EdgeProjection.__table__.insert(),
            self._edge_rows(),
        )

    def _page_link_rows(self) -> Iterable[dict[str, Any]]:
        key = self.plan.corpus_key
        for index, relation in enumerate(self.plan.relations):
            if relation.edge_kind != "wikilink":
                continue
            timestamp = fixture_time(5_000 + index)
            yield {
                "id": fixture_uuid(key, "page-link", index),
                "workspace_id": self.workspace_id,
                "source_page_id": self.context_ids[relation.source_index],
                "source_version_id": self.context_version_ids[
                    relation.source_index
                ],
                "link_syntax": relation.link_syntax or "wikilink",
                "locator_key": (
                    f"page_id:{self.context_ids[relation.target_index]}"
                    if relation.link_syntax == "context_url"
                    else f"wikilink:doc-{relation.target_index:06d}"
                ),
                "target_slug": (
                    None
                    if relation.link_syntax == "context_url"
                    else f"doc-{relation.target_index:06d}"
                ),
                "target_page_id": self.context_ids[relation.target_index],
                "occurrence_count": 1,
                "first_snippet": (
                    "Deterministic fixture context URL"
                    if relation.link_syntax == "context_url"
                    else "Deterministic fixture wikilink"
                ),
                "created_at": timestamp,
            }

    def _citation_rows(self) -> Iterable[dict[str, Any]]:
        key = self.plan.corpus_key
        for index, relation in enumerate(self.plan.relations):
            if relation.edge_kind == "wikilink":
                continue
            timestamp = fixture_time(5_000 + index)
            is_source = relation.edge_kind == "citation_source"
            yield {
                "id": fixture_uuid(key, "citation", index),
                "workspace_id": self.workspace_id,
                "target_kind": "context_page_version",
                "target_id": self.context_version_ids[relation.source_index],
                "target_location": {},
                "library_item_version_id": (
                    self.library_version_ids[relation.target_index]
                    if is_source
                    else None
                ),
                "library_artifact_id": None,
                "cited_context_page_version_id": (
                    None
                    if is_source
                    else self.context_version_ids[relation.target_index]
                ),
                "location": {},
                "snippet": (
                    "Deterministic fixture citation"
                    if is_source
                    else "Deterministic fixture page citation"
                ),
                "citation_type": "raw_source" if is_source else "context_page",
                "verification_status": "verified",
                "created_via": "manual_input",
                "created_by_actor_type": "human",
                "created_at": timestamp,
            }

    def _edge_rows(self) -> Iterable[dict[str, Any]]:
        key = self.plan.corpus_key
        for index, relation in enumerate(self.plan.relations):
            timestamp = fixture_time(5_000 + index)
            yield {
                "id": fixture_uuid(key, "relation", index),
                "workspace_id": self.workspace_id,
                "source_kind": "context_page",
                "source_id": self.context_ids[relation.source_index],
                "target_kind": relation.target_kind,
                "target_id": self._relation_target_id(relation),
                "edge_kind": relation.edge_kind,
                "provenance": "curated",
                "weight": 1.0,
                "occurred_at": timestamp,
                "source_ref": {"fixture": key},
                "last_seen_at": timestamp,
                "created_at": timestamp,
            }

    def _relation_target_id(self, relation: PlannedRelation) -> UUID:
        if relation.target_kind == "context_page":
            return self.context_ids[relation.target_index]
        return self.library_version_ids[relation.target_index]

    def _library_uri(self, index: int) -> str:
        if self.plan.workspace_index == 0:
            return f"https://example.test/source/{index}"
        return (
            "https://example.test/source/"
            f"{self.plan.workspace_index}/{index}"
        )


def _execute_in_chunks(
    session: Session,
    statement: Any,
    rows: Iterable[dict[str, Any]],
) -> None:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) == _INSERT_CHUNK_SIZE:
            session.execute(statement, batch)
            batch.clear()
    if batch:
        session.execute(statement, batch)
