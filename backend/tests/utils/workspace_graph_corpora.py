"""Deterministic Workspace Knowledge Map acceptance corpora.

The helpers in this module intentionally build isolated workspaces with fixed
UUIDv5 identities and fixed UTC timestamps.  This makes the complete graph
scene wire payload comparable byte-for-byte: adding actor-invisible rows must
not perturb generated timestamps, revisions, ordering, facets, or aggregation.

Small correctness corpora use the same persisted document and projection
models consumed by :class:`WorkspaceGraphService`.  The helpers are test-only;
production services remain the authority for visibility and scene building.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
)
from backend.models.library import (
    LibraryArtifact,
    LibraryAttempt,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
)
from backend.models.media import (
    BlobIncarnationRow,
    StoredBlobRow,
    WorkspaceMediaWriteIntent,
)
from backend.models.tree import WorkspaceTreeNode
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.auth import ActorContext
from backend.tests.utils.workspace_graph_fixture_identity import (
    fixture_time,
    fixture_uuid,
)
from backend.tests.utils.workspace_graph_synthetic import (
    SyntheticCorpusPlan,
    SyntheticCorpusProfile,
    plan_synthetic_corpus,
)
from backend.utils.content_hash import content_hash as calculate_content_hash

WorkspaceGraphCorpusName = Literal[
    "knowledge-40",
    "folder-mismatch-80",
    "star-250-750",
]
BENCHMARK_PASSWORD = "graph-benchmark-only"
BENCHMARK_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$ZHR4V2lraUdyYXBoQmVuY2g$"
    "3kdO20BUH4ujhUx0HvNzfUy7ynMidPyBtutFfxcQyo8"
)


@dataclass(slots=True)
class WorkspaceGraphCorpus:
    name: str
    workspace_id: UUID
    context_ids: tuple[UUID, ...]
    context_version_ids: tuple[UUID, ...]
    library_ids: tuple[UUID, ...]
    library_version_ids: tuple[UUID, ...]
    folder_ids: tuple[UUID, ...]
    relation_ids: tuple[UUID, ...]
    restricted_library_ids: tuple[UUID, ...] = ()
    context_slugs: dict[UUID, str] = field(default_factory=dict)
    library_uris: dict[UUID, str] = field(default_factory=dict)
    primary_folder_labels: dict[UUID, str] = field(default_factory=dict)
    secondary_folder_labels: dict[UUID, tuple[str, ...]] = field(default_factory=dict)

    @property
    def document_ids(self) -> tuple[UUID, ...]:
        return self.context_ids + self.library_ids


@dataclass(frozen=True, slots=True)
class BenchmarkPrincipal:
    """One persisted benchmark actor and its visibility contract."""

    name: Literal["owner", "viewer", "restricted_reader"]
    user_id: UUID
    email: str
    role: Literal["owner", "viewer"]
    synthetic_scopes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceGraphRelationBranches:
    claim_source_page_id: UUID
    claim_library_item_id: UUID
    artifact_source_page_id: UUID
    artifact_library_item_id: UUID
    context_source_page_id: UUID
    context_target_page_id: UUID
    artifact_id: UUID

    @property
    def expected_relation_keys(
        self,
    ) -> frozenset[tuple[str, tuple[str, UUID], tuple[str, UUID]]]:
        return frozenset(
            {
                (
                    "citation_source",
                    ("context", self.claim_source_page_id),
                    ("library", self.claim_library_item_id),
                ),
                (
                    "citation_source",
                    ("context", self.artifact_source_page_id),
                    ("library", self.artifact_library_item_id),
                ),
                (
                    "citation_page",
                    ("context", self.context_source_page_id),
                    ("context", self.context_target_page_id),
                ),
            }
        )


def _add_library_artifact(
    session: Session,
    *,
    workspace_id: UUID,
    library_item_version_id: UUID,
    fixture_key: str,
) -> LibraryArtifact:
    """Persist an artifact whose complete ownership chain satisfies real FKs."""

    now = fixture_time()
    if session.get(WorkspaceMediaWriteIntent, workspace_id) is None:
        session.add(
            WorkspaceMediaWriteIntent(
                workspace_id=workspace_id,
                state="open",
                delete_generation=0,
                updated_at=now,
            )
        )
        session.flush()

    blob_id = fixture_uuid(fixture_key, "artifact-blob", library_item_version_id)
    digest = blob_id.hex + blob_id.hex
    session.add(
        StoredBlobRow(
            workspace_id=workspace_id,
            id=blob_id,
            sha256=digest,
            next_epoch=2,
        )
    )
    session.flush()
    session.add(
        BlobIncarnationRow(
            workspace_id=workspace_id,
            blob_id=blob_id,
            blob_epoch=1,
            byte_size=1,
            storage_key=f"w/{workspace_id.hex}/{fixture_key}/{blob_id.hex}",
            state="available",
            publish_generation=1,
            security_generation=0,
            gc_generation=0,
            available_at=now,
        )
    )
    session.flush()

    run = LibraryRun(
        workspace_id=workspace_id,
        library_item_version_id=library_item_version_id,
        stage="conversion",
        run_seq=1,
        reason="initial",
        status="completed",
        provider_chain_json=[],
        policy_revision=fixture_key,
        output_schema_version="1",
        idempotency_key=f"{fixture_key}:{library_item_version_id}",
    )
    session.add(run)
    session.flush()
    attempt = LibraryAttempt(
        workspace_id=workspace_id,
        run_id=run.id,
        library_item_version_id=library_item_version_id,
        stage="conversion",
        provider_id=f"fixture.{fixture_key}",
        provider_version="1",
        provider_spi_version="1",
        config_hash=fixture_key,
        input_sha256=digest,
        output_schema_version="1",
        status="completed",
        chain_index=0,
        transport_retry_count=0,
        execution_generation=1,
    )
    session.add(attempt)
    session.flush()
    artifact = LibraryArtifact(
        workspace_id=workspace_id,
        library_item_version_id=library_item_version_id,
        attempt_id=attempt.id,
        stage="conversion",
        blob_workspace_id=workspace_id,
        blob_id=blob_id,
        blob_epoch=1,
        role="markdown",
        kind="text",
        media_type="text/plain",
        content_sha256=digest,
        byte_size=1,
    )
    session.add(artifact)
    session.flush()
    return artifact


def add_knowledge_40_relation_branch_fixtures(
    session: Session,
    corpus: WorkspaceGraphCorpus,
    *,
    fixture_key: str,
) -> WorkspaceGraphRelationBranches:
    """Add claim, context-page, and artifact branches to a knowledge-40 corpus."""

    claim_source_head_id = corpus.context_version_ids[20]
    artifact_source_head_id = corpus.context_version_ids[21]
    context_source_head_id = corpus.context_version_ids[22]
    context_target_head_id = corpus.context_version_ids[31]
    claim_library_head_id = corpus.library_version_ids[0]
    artifact_library_head_id = corpus.library_version_ids[1]

    claim = ContextClaim(
        workspace_id=corpus.workspace_id,
        page_version_id=claim_source_head_id,
        claim_text="Claim-target relation branch",
        location_in_page={},
    )
    session.add(claim)
    session.flush()
    session.add(
        ContextCitation(
            workspace_id=corpus.workspace_id,
            target_kind="context_claim",
            target_id=claim.id,
            target_location={},
            library_item_version_id=claim_library_head_id,
            location={},
            citation_type="raw_source",
            verification_status="verified",
            created_via="manual_input",
            created_by_actor_type="human",
        )
    )

    artifact = _add_library_artifact(
        session,
        workspace_id=corpus.workspace_id,
        library_item_version_id=artifact_library_head_id,
        fixture_key=fixture_key,
    )
    session.add_all(
        [
            ContextCitation(
                workspace_id=corpus.workspace_id,
                target_kind="context_page_version",
                target_id=artifact_source_head_id,
                target_location={},
                library_artifact_id=artifact.id,
                location={},
                citation_type="derived_artifact",
                verification_status="verified",
                created_via="manual_input",
                created_by_actor_type="human",
            ),
            ContextCitation(
                workspace_id=corpus.workspace_id,
                target_kind="context_page_version",
                target_id=context_source_head_id,
                target_location={},
                cited_context_page_version_id=context_target_head_id,
                location={},
                citation_type="context_page",
                verification_status="verified",
                created_via="manual_input",
                created_by_actor_type="human",
            ),
        ]
    )
    return WorkspaceGraphRelationBranches(
        claim_source_page_id=corpus.context_ids[20],
        claim_library_item_id=corpus.library_ids[0],
        artifact_source_page_id=corpus.context_ids[21],
        artifact_library_item_id=corpus.library_ids[1],
        context_source_page_id=corpus.context_ids[22],
        context_target_page_id=corpus.context_ids[31],
        artifact_id=artifact.id,
    )


def benchmark_actor(
    corpus: WorkspaceGraphCorpus,
    principal: BenchmarkPrincipal,
) -> ActorContext:
    """Adapt a persisted benchmark principal to the measured request channel."""

    uses_synthetic_scope = bool(principal.synthetic_scopes)
    return ActorContext(
        actor_kind="human",
        actor_id=str(principal.user_id),
        user_id=principal.user_id,
        token_kind="api_token" if uses_synthetic_scope else "session",
        token_id=(
            fixture_uuid(corpus.name, "synthetic-benchmark-token", principal.name)
            if uses_synthetic_scope
            else None
        ),
        scopes=principal.synthetic_scopes,
        workspace_role=principal.role,
        role=principal.role,
        workspace_id=corpus.workspace_id,
        token_workspace_ids=(corpus.workspace_id,) if uses_synthetic_scope else (),
        provenance=(
            "personal_access_token" if uses_synthetic_scope else "browser_session"
        ),
    )


class WorkspaceGraphCorpusBuilder:
    """Build one isolated deterministic workspace corpus."""

    def __init__(
        self,
        session: Session,
        *,
        corpus_key: str,
        title: str,
        existing_corpus: WorkspaceGraphCorpus | None = None,
    ) -> None:
        self.session = session
        self.corpus_key = corpus_key
        if existing_corpus is None:
            self.workspace = Workspace(
                id=fixture_uuid(corpus_key, "workspace"),
                slug=f"workspace-graph-{corpus_key}",
                title=title,
                name=title,
                description=f"Deterministic {corpus_key} acceptance corpus",
                visibility="private",
                join_policy="invite_only",
                created_by_actor_id=fixture_uuid(corpus_key, "creator"),
                owner_actor_id=fixture_uuid(corpus_key, "owner"),
                # A graph corpus stands for a workspace whose owner has switched
                # the Knowledge Map on in Settings.
                workspace_graph_enabled=True,
                created_at=fixture_time(),
                updated_at=fixture_time(),
            )
            session.add(self.workspace)
            session.flush()
        else:
            workspace = session.get(Workspace, existing_corpus.workspace_id)
            if workspace is None:
                raise ValueError("existing corpus workspace is missing")
            self.workspace = workspace

        self.context_ids: list[UUID] = []
        self.context_version_ids: list[UUID] = []
        self.library_ids: list[UUID] = []
        self.library_version_ids: list[UUID] = []
        self.folder_ids: list[UUID] = []
        self.relation_ids: list[UUID] = []
        self.restricted_library_ids: list[UUID] = []
        self.primary_folder_labels: dict[UUID, str] = {}
        self.secondary_folder_labels: dict[UUID, list[str]] = {}
        self._context_slugs: dict[UUID, str] = {}
        self._context_versions: dict[UUID, UUID] = {}
        self._library_versions: dict[UUID, UUID] = {}
        self._library_uris: dict[UUID, str] = {}
        self._folder_labels: dict[UUID, str] = {}
        if existing_corpus is not None:
            # Do not guess head identities from the corpus namespace. Refuse
            # stale or foreign corpus rows before authoring current citations.
            for model, pointer, ids, versions, destination in (
                (
                    ContextPage,
                    ContextPage.current_published_version_id,
                    existing_corpus.context_ids,
                    existing_corpus.context_version_ids,
                    self._context_versions,
                ),
                (
                    LibraryItem,
                    LibraryItem.current_version_id,
                    existing_corpus.library_ids,
                    existing_corpus.library_version_ids,
                    self._library_versions,
                ),
            ):
                current_heads = dict(
                    session.execute(
                        select(model.id, pointer).where(
                            model.workspace_id == self.workspace.id,
                            model.id.in_(ids),
                        )
                    ).all()
                )
                if current_heads != dict(zip(ids, versions, strict=True)):
                    raise ValueError(
                        "existing corpus must match current workspace heads"
                    )
                destination.update(current_heads)
            self._folder_labels.update(
                session.execute(
                    select(WorkspaceTreeNode.id, WorkspaceTreeNode.title).where(
                        WorkspaceTreeNode.workspace_id == self.workspace.id,
                        WorkspaceTreeNode.id.in_(existing_corpus.folder_ids),
                        WorkspaceTreeNode.node_kind == "folder",
                    )
                ).all()
            )
            if set(self._folder_labels) != set(existing_corpus.folder_ids):
                raise ValueError("existing corpus folders must belong to its workspace")
            for field_name in (
                "context_ids",
                "context_version_ids",
                "library_ids",
                "library_version_ids",
                "folder_ids",
                "relation_ids",
                "restricted_library_ids",
            ):
                getattr(self, field_name).extend(getattr(existing_corpus, field_name))
            self._context_slugs.update(existing_corpus.context_slugs)
            self._library_uris.update(existing_corpus.library_uris)
            self.primary_folder_labels.update(existing_corpus.primary_folder_labels)
            self.secondary_folder_labels.update(
                (key, list(labels))
                for key, labels in existing_corpus.secondary_folder_labels.items()
            )

    def add_context(
        self,
        index: int,
        *,
        slug: str,
        title: str,
        body_md: str | None = None,
        published: bool = True,
        timestamp_offset: int | None = None,
    ) -> UUID:
        timestamp = fixture_time(
            timestamp_offset if timestamp_offset is not None else index
        )
        page_id = fixture_uuid(self.corpus_key, "context", index)
        version_id = fixture_uuid(self.corpus_key, "context-version", index)
        page = ContextPage(
            id=page_id,
            workspace_id=self.workspace.id,
            slug=slug,
            title=title,
            page_type="concept",
            lifecycle_status="published" if published else "draft",
            created_by_actor_type="human",
            created_by_actor_id=fixture_uuid(self.corpus_key, "author"),
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.session.add(page)
        self.session.flush()
        resolved_body_md = (
            body_md
            if body_md is not None
            else f"# {title}\n\nDeterministic workspace graph fixture."
        )
        version = ContextPageVersion(
            workspace_id=self.workspace.id,
            id=version_id,
            page_id=page_id,
            version_no=1,
            body_md=resolved_body_md,
            content_hash=calculate_content_hash(resolved_body_md),
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id=str(fixture_uuid(self.corpus_key, "author")),
            version_status="published_snapshot" if published else "draft",
            last_saved_at=timestamp,
            created_at=timestamp,
        )
        self.session.add(version)
        self.session.flush()
        page.current_version_id = version_id
        if published:
            page.current_published_version_id = version_id
        else:
            page.latest_draft_version_id = version_id
        # Assign explicitly so the model's on-update clock cannot introduce
        # wall-clock entropy when the head pointer is attached.
        page.updated_at = timestamp
        flag_modified(page, "updated_at")

        self.context_ids.append(page_id)
        self.context_version_ids.append(version_id)
        self._context_slugs[page_id] = slug
        self._context_versions[page_id] = version_id
        return page_id

    def add_library(
        self,
        index: int,
        *,
        title: str,
        uri: str,
        restricted: bool = False,
        timestamp_offset: int | None = None,
    ) -> tuple[UUID, UUID]:
        timestamp = fixture_time(
            timestamp_offset if timestamp_offset is not None else 1_000 + index
        )
        item_id = fixture_uuid(self.corpus_key, "library", index)
        version_id = fixture_uuid(self.corpus_key, "library-version", index)
        item = LibraryItem(
            id=item_id,
            workspace_id=self.workspace.id,
            connector_type="url",
            format_hint="html",
            uri=uri,
            title=title,
            metadata_json={},
            trust_level="restricted" if restricted else "trusted",
            lifecycle_status="ready",
            created_by_actor_type="human",
            created_by_actor_id=fixture_uuid(self.corpus_key, "author"),
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.session.add(item)
        self.session.flush()
        version = LibraryItemVersion(
            workspace_id=self.workspace.id,
            id=version_id,
            library_item_id=item_id,
            version_no=1,
            content_hash=f"library-{self.corpus_key}-{index:04d}",
            fetched_at=timestamp,
            raw_storage_ref=f"fixture://{self.corpus_key}/library/{index}",
            processing_status="ready",
            extraction_summary="Deterministic fixture source",
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.session.add(version)
        self.session.flush()
        item.current_version_id = version_id
        item.updated_at = timestamp
        flag_modified(item, "updated_at")

        self.library_ids.append(item_id)
        self.library_version_ids.append(version_id)
        self._library_versions[item_id] = version_id
        self._library_uris[item_id] = uri
        if restricted:
            self.restricted_library_ids.append(item_id)
        return item_id, version_id

    def add_folder(
        self,
        index: int,
        *,
        section: Literal["context", "library"],
        title: str,
    ) -> UUID:
        folder_id = fixture_uuid(self.corpus_key, "folder", section, index)
        slug = "-".join(title.casefold().split())
        self.session.add(
            WorkspaceTreeNode(
                id=folder_id,
                workspace_id=self.workspace.id,
                section=section,
                node_kind="folder",
                parent_id=None,
                sort_key=f"{index:04d}",
                title=title,
                slug=slug,
                target_kind=None,
                target_id=None,
                is_system_node=False,
                can_have_children=True,
                primary_placement=True,
                visibility_status="visible",
                created_by_actor_type="human",
                created_by_actor_id=fixture_uuid(self.corpus_key, "author"),
                created_at=fixture_time(2_000 + index),
                updated_at=fixture_time(2_000 + index),
            )
        )
        self.folder_ids.append(folder_id)
        self._folder_labels[folder_id] = title
        return folder_id

    def place(
        self,
        index: int,
        *,
        entity_kind: Literal["context", "library"],
        entity_id: UUID,
        title: str,
        folder_id: UUID,
        primary: bool = True,
    ) -> UUID:
        node_id = fixture_uuid(
            self.corpus_key,
            "placement",
            entity_kind,
            entity_id,
            "primary" if primary else "secondary",
            index,
        )
        target_kind = "context_page" if entity_kind == "context" else "library_item"
        node_kind = "page_ref" if entity_kind == "context" else "library_item_ref"
        slug = (
            self._context_slugs[entity_id]
            if entity_kind == "context"
            else f"library-{str(entity_id)[:8]}"
        )
        self.session.add(
            WorkspaceTreeNode(
                id=node_id,
                workspace_id=self.workspace.id,
                section=entity_kind,
                node_kind=node_kind,
                parent_id=folder_id,
                sort_key=f"{index:06d}",
                title=title,
                slug=slug,
                target_kind=target_kind,
                target_id=entity_id,
                is_system_node=False,
                can_have_children=False,
                primary_placement=primary,
                visibility_status="visible",
                created_by_actor_type="human",
                created_by_actor_id=fixture_uuid(self.corpus_key, "author"),
                created_at=fixture_time(3_000 + index),
                updated_at=fixture_time(3_000 + index),
            )
        )
        folder_label = self._folder_labels[folder_id]
        if primary:
            self.primary_folder_labels[entity_id] = folder_label
        else:
            self.secondary_folder_labels.setdefault(entity_id, []).append(folder_label)
        return node_id

    def add_relation(
        self,
        index: int,
        *,
        source_context_id: UUID,
        target_kind: Literal[
            "context_page",
            "library_item",
            "library_item_version",
        ],
        target_id: UUID,
        edge_kind: Literal["wikilink", "citation_page", "citation_source"],
        weight: float = 1.0,
        timestamp_offset: int | None = None,
    ) -> UUID:
        edge_id = fixture_uuid(self.corpus_key, "relation", index)
        timestamp = fixture_time(
            timestamp_offset if timestamp_offset is not None else 5_000 + index
        )
        if edge_kind == "wikilink":
            if target_kind != "context_page":
                raise ValueError("wikilink fixtures require a Context target")
            target_slug = self._context_slugs[target_id]
            self.session.add(
                PageLink(
                    id=fixture_uuid(self.corpus_key, "page-link", index),
                    workspace_id=self.workspace.id,
                    source_page_id=source_context_id,
                    source_version_id=self._context_versions[source_context_id],
                    target_slug=target_slug,
                    target_page_id=target_id,
                    occurrence_count=max(1, int(weight)),
                    first_snippet="Deterministic fixture wikilink",
                    created_at=timestamp,
                )
            )
        elif edge_kind == "citation_source":
            if target_kind != "library_item_version":
                raise ValueError(
                    "citation_source fixtures require a Library version target"
                )
            self.session.add(
                ContextCitation(
                    workspace_id=self.workspace.id,
                    id=fixture_uuid(self.corpus_key, "citation", index),
                    target_kind="context_page_version",
                    target_id=self._context_versions[source_context_id],
                    target_location={},
                    library_item_version_id=target_id,
                    library_artifact_id=None,
                    cited_context_page_version_id=None,
                    location={},
                    snippet="Deterministic fixture citation",
                    citation_type="raw_source",
                    verification_status="verified",
                    created_via="manual_input",
                    created_by_actor_type="human",
                    created_at=timestamp,
                )
            )
        elif edge_kind == "citation_page":
            if target_kind != "context_page":
                raise ValueError("citation_page fixtures require a Context target")
            self.session.add(
                ContextCitation(
                    workspace_id=self.workspace.id,
                    id=fixture_uuid(self.corpus_key, "citation", index),
                    target_kind="context_page_version",
                    target_id=self._context_versions[source_context_id],
                    target_location={},
                    library_item_version_id=None,
                    library_artifact_id=None,
                    cited_context_page_version_id=self._context_versions[target_id],
                    location={},
                    snippet="Deterministic fixture page citation",
                    citation_type="context_page",
                    verification_status="verified",
                    created_via="manual_input",
                    created_by_actor_type="human",
                    created_at=timestamp,
                )
            )
        self.session.add(
            EdgeProjection(
                id=edge_id,
                workspace_id=self.workspace.id,
                source_kind="context_page",
                source_id=source_context_id,
                target_kind=target_kind,
                target_id=target_id,
                edge_kind=edge_kind,
                provenance="curated",
                weight=weight,
                occurred_at=timestamp,
                source_ref={"fixture": self.corpus_key},
                created_at=timestamp,
                last_seen_at=timestamp,
            )
        )
        self.relation_ids.append(edge_id)
        return edge_id

    def finish(self, name: str) -> WorkspaceGraphCorpus:
        self.session.flush()
        return WorkspaceGraphCorpus(
            name=name,
            workspace_id=self.workspace.id,
            context_ids=tuple(self.context_ids),
            context_version_ids=tuple(self.context_version_ids),
            library_ids=tuple(self.library_ids),
            library_version_ids=tuple(self.library_version_ids),
            folder_ids=tuple(self.folder_ids),
            relation_ids=tuple(self.relation_ids),
            restricted_library_ids=tuple(self.restricted_library_ids),
            context_slugs=dict(self._context_slugs),
            library_uris=dict(self._library_uris),
            primary_folder_labels=dict(self.primary_folder_labels),
            secondary_folder_labels={
                key: tuple(values)
                for key, values in self.secondary_folder_labels.items()
            },
        )


def build_knowledge_40(
    session: Session,
    *,
    corpus_key: str = "knowledge-40",
) -> WorkspaceGraphCorpus:
    """Build 32 Context and eight canonical Library documents.

    The final Library item is restricted.  The raw projection also includes a
    self-link (which must not become a scene relation), one high occurrence
    weight, 40 authored wikilinks, and eight evidence relations.
    """

    builder = WorkspaceGraphCorpusBuilder(
        session,
        corpus_key=corpus_key,
        title="Knowledge 40",
    )
    context_folders = [
        builder.add_folder(
            index,
            section="context",
            title=f"Knowledge Area {index}",
        )
        for index in range(4)
    ]
    library_folders = [
        builder.add_folder(
            index,
            section="library",
            title=f"Source Shelf {index}",
        )
        for index in range(2)
    ]

    for index in range(32):
        page_id = builder.add_context(
            index,
            slug=f"knowledge-{index:02d}",
            title=f"Knowledge Document {index:02d}",
        )
        builder.place(
            index,
            entity_kind="context",
            entity_id=page_id,
            title=f"Knowledge Document {index:02d}",
            folder_id=context_folders[index % len(context_folders)],
        )

    for index in range(8):
        item_id, _version_id = builder.add_library(
            index,
            title=f"Knowledge Source {index:02d}",
            uri=f"https://fixtures.invalid/knowledge/{index:02d}",
            restricted=index == 7,
        )
        builder.place(
            100 + index,
            entity_kind="library",
            entity_id=item_id,
            title=f"Knowledge Source {index:02d}",
            folder_id=library_folders[index % len(library_folders)],
        )

    relation_index = 0
    for index, source_id in enumerate(builder.context_ids):
        builder.add_relation(
            relation_index,
            source_context_id=source_id,
            target_kind="context_page",
            target_id=builder.context_ids[(index + 1) % 32],
            edge_kind="wikilink",
            weight=9.0 if index == 0 else 1.0,
        )
        relation_index += 1
    for index in range(8):
        builder.add_relation(
            relation_index,
            source_context_id=builder.context_ids[index],
            target_kind="context_page",
            target_id=builder.context_ids[index + 8],
            edge_kind="wikilink",
        )
        relation_index += 1
    builder.add_relation(
        relation_index,
        source_context_id=builder.context_ids[0],
        target_kind="context_page",
        target_id=builder.context_ids[0],
        edge_kind="wikilink",
        weight=99.0,
    )
    relation_index += 1
    for index in range(8):
        builder.add_relation(
            relation_index,
            source_context_id=builder.context_ids[index],
            target_kind="library_item_version",
            target_id=builder.library_version_ids[index],
            edge_kind="citation_source",
        )
        relation_index += 1
    return builder.finish("knowledge-40")


def build_folder_mismatch_80(
    session: Session,
    *,
    corpus_key: str = "folder-mismatch-80",
) -> WorkspaceGraphCorpus:
    """Build 80 documents whose URI text deliberately lies about placement."""

    builder = WorkspaceGraphCorpusBuilder(
        session,
        corpus_key=corpus_key,
        title="Folder Mismatch 80",
    )
    context_folders = [
        builder.add_folder(
            index,
            section="context",
            title=f"Actual Context Folder {index}",
        )
        for index in range(8)
    ]
    library_folders = [
        builder.add_folder(
            index,
            section="library",
            title=f"Actual Library Folder {index}",
        )
        for index in range(8)
    ]

    for index in range(64):
        claimed = (index + 3) % 8
        page_id = builder.add_context(
            index,
            slug=f"claimed-folder-{claimed}-context-{index:02d}",
            title=f"Claimed Folder {claimed} Context {index:02d}",
        )
        builder.place(
            index,
            entity_kind="context",
            entity_id=page_id,
            title=f"Claimed Folder {claimed} Context {index:02d}",
            folder_id=context_folders[index % 8],
        )
        if index < 16:
            builder.place(
                1_000 + index,
                entity_kind="context",
                entity_id=page_id,
                title=f"Claimed Folder {claimed} Context {index:02d}",
                folder_id=context_folders[(index + 1) % 8],
                primary=False,
            )

    for index in range(16):
        claimed = (index + 5) % 8
        item_id, _version_id = builder.add_library(
            index,
            title=f"Claimed Folder {claimed} Source {index:02d}",
            uri=(
                f"https://fixtures.invalid/claimed-folder-{claimed}/source-{index:02d}"
            ),
        )
        builder.place(
            2_000 + index,
            entity_kind="library",
            entity_id=item_id,
            title=f"Claimed Folder {claimed} Source {index:02d}",
            folder_id=library_folders[index % 8],
        )
    return builder.finish("folder-mismatch-80")


def build_star_250_750(
    session: Session,
    *,
    corpus_key: str = "star-250-750",
) -> WorkspaceGraphCorpus:
    """Build the exact 250-document / 750-relation scene boundary."""

    builder = WorkspaceGraphCorpusBuilder(
        session,
        corpus_key=corpus_key,
        title="Star 250 750",
    )
    folder_id = builder.add_folder(0, section="context", title="Star Documents")
    for index in range(250):
        page_id = builder.add_context(
            index,
            slug=f"star-{index:03d}",
            title="Star Hub" if index == 0 else f"Star Spoke {index:03d}",
        )
        builder.place(
            index,
            entity_kind="context",
            entity_id=page_id,
            title="Star Hub" if index == 0 else f"Star Spoke {index:03d}",
            folder_id=folder_id,
        )

    hub = builder.context_ids[0]
    relation_index = 0
    for spoke in builder.context_ids[1:]:
        builder.add_relation(
            relation_index,
            source_context_id=hub,
            target_kind="context_page",
            target_id=spoke,
            edge_kind="wikilink",
        )
        relation_index += 1
    for spoke in builder.context_ids[1:]:
        builder.add_relation(
            relation_index,
            source_context_id=spoke,
            target_kind="context_page",
            target_id=hub,
            edge_kind="wikilink",
        )
        relation_index += 1

    spokes = builder.context_ids[1:]
    for index, spoke in enumerate(spokes):
        builder.add_relation(
            relation_index,
            source_context_id=spoke,
            target_kind="context_page",
            target_id=spokes[(index + 1) % len(spokes)],
            edge_kind="wikilink",
        )
        relation_index += 1
    for index in range(3):
        builder.add_relation(
            relation_index,
            source_context_id=spokes[index],
            target_kind="context_page",
            target_id=spokes[index + 2],
            edge_kind="wikilink",
        )
        relation_index += 1

    assert relation_index == 750
    return builder.finish("star-250-750")


def build_workspace_graph_corpus(
    session: Session,
    name: WorkspaceGraphCorpusName,
    *,
    corpus_key: str | None = None,
) -> WorkspaceGraphCorpus:
    builders = {
        "knowledge-40": build_knowledge_40,
        "folder-mismatch-80": build_folder_mismatch_80,
        "star-250-750": build_star_250_750,
    }
    return builders[name](session, corpus_key=corpus_key or name)


def build_synthetic_corpus(
    session: Session,
    *,
    documents: int,
    profile: SyntheticCorpusProfile,
    seed: int,
    workspaces: int = 1,
    restricted_library_pct: float = 0,
    relation_multiplier: int = 1,
) -> list[WorkspaceGraphCorpus]:
    """Build deterministic scale corpora used by graph benchmarks and tests.

    Each generated workspace contains 90 percent Context pages and 10 percent
    Library items. ``synthetic`` preserves the dense, preferential-link
    distribution used by the original large-scale benchmark. ``sparse`` models
    the observed pre-launch workspace: 5 percent wikilink authors, 15 percent
    URL-link authors, and 10 percent citation authors.
    """

    if workspaces < 1:
        raise ValueError("workspaces must be at least 1")

    return [
        build_synthetic_workspace(
            session,
            documents=documents,
            profile=profile,
            seed=seed,
            workspace_index=workspace_index,
            restricted_library_pct=restricted_library_pct,
            relation_multiplier=relation_multiplier,
        )
        for workspace_index in range(workspaces)
    ]


def build_synthetic_workspace(
    session: Session,
    *,
    documents: int,
    profile: SyntheticCorpusProfile,
    seed: int,
    workspace_index: int,
    restricted_library_pct: float,
    relation_multiplier: int = 1,
    corpus_key: str | None = None,
) -> WorkspaceGraphCorpus:
    """Build one indexed workspace for callers that commit incrementally."""

    plan = plan_synthetic_corpus(
        documents=documents,
        profile=profile,
        seed=seed,
        workspace_index=workspace_index,
        restricted_library_pct=restricted_library_pct,
        relation_multiplier=relation_multiplier,
        corpus_key=corpus_key,
    )
    return materialize_synthetic_corpus(session, plan)


def materialize_synthetic_corpus(
    session: Session,
    plan: SyntheticCorpusPlan,
) -> WorkspaceGraphCorpus:
    """Persist one pure plan through the chunked scale-corpus materializer."""

    from backend.tests.utils.workspace_graph_synthetic_materializer import (
        SyntheticCorpusMaterializer,
    )

    materialized = SyntheticCorpusMaterializer(session, plan).materialize()
    return WorkspaceGraphCorpus(
        name=materialized.name,
        workspace_id=materialized.workspace_id,
        context_ids=materialized.context_ids,
        context_version_ids=materialized.context_version_ids,
        library_ids=materialized.library_ids,
        library_version_ids=materialized.library_version_ids,
        folder_ids=materialized.folder_ids,
        relation_ids=materialized.relation_ids,
        restricted_library_ids=materialized.restricted_library_ids,
        context_slugs=materialized.context_slugs,
        library_uris=materialized.library_uris,
        primary_folder_labels=materialized.primary_folder_labels,
    )


def add_hidden_draft_decoys(
    session: Session,
    corpus: WorkspaceGraphCorpus,
    *,
    count: int = 1_025,
) -> tuple[UUID, ...]:
    """Add high-weight published-source -> hidden-draft adversarial rows."""

    if not corpus.context_ids:
        raise ValueError("privacy decoys require a visible Context source")
    corpus_key = f"{corpus.name}-hidden-twin"
    timestamp = fixture_time(100_000)
    pages: list[ContextPage] = []
    versions: list[ContextPageVersion] = []
    for index in range(count):
        page_id = fixture_uuid(corpus_key, "draft", index)
        version_id = fixture_uuid(corpus_key, "draft-version", index)
        pages.append(
            ContextPage(
                id=page_id,
                workspace_id=corpus.workspace_id,
                slug=f"privacy-sentinel-hidden-{index:04d}",
                title=f"PRIVACY SENTINEL HIDDEN {index:04d}",
                page_type="concept",
                lifecycle_status="draft",
                created_by_actor_type="human",
                created_by_actor_id=fixture_uuid(corpus_key, "author"),
                created_at=timestamp + timedelta(seconds=index),
                updated_at=timestamp + timedelta(seconds=index),
            )
        )
        versions.append(
            ContextPageVersion(
                workspace_id=corpus.workspace_id,
                id=version_id,
                page_id=page_id,
                version_no=1,
                body_md="# PRIVACY SENTINEL\n\nThis draft must be invisible.",
                content_hash=f"privacy-hidden-{index:04d}",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id=str(fixture_uuid(corpus_key, "author")),
                version_status="draft",
                created_at=timestamp + timedelta(seconds=index),
            )
        )
    session.add_all(pages)
    session.flush()
    session.add_all(versions)
    session.flush()
    for index, page in enumerate(pages):
        version_id = versions[index].id
        page.current_version_id = version_id
        page.latest_draft_version_id = version_id
        page.updated_at = timestamp + timedelta(seconds=index)
        flag_modified(page, "updated_at")

    edges = [
        EdgeProjection(
            id=fixture_uuid(corpus_key, "edge", index),
            workspace_id=corpus.workspace_id,
            source_kind="context_page",
            source_id=corpus.context_ids[index % len(corpus.context_ids)],
            target_kind="context_page",
            target_id=page.id,
            edge_kind="wikilink",
            provenance="curated",
            weight=float(1_000_000 - index),
            occurred_at=timestamp + timedelta(seconds=index),
            source_ref={"fixture": "privacy-twin", "hidden": True},
            created_at=timestamp + timedelta(seconds=index),
            last_seen_at=timestamp + timedelta(seconds=index),
        )
        for index, page in enumerate(pages)
    ]
    source_versions = dict(
        zip(corpus.context_ids, corpus.context_version_ids, strict=True)
    )
    page_links = [
        PageLink(
            id=fixture_uuid(corpus_key, "page-link", index),
            workspace_id=corpus.workspace_id,
            source_page_id=edge.source_id,
            source_version_id=source_versions[edge.source_id],
            target_slug=pages[index].slug,
            target_page_id=pages[index].id,
            occurrence_count=max(1, int(edge.weight)),
            first_snippet="High-weight hidden draft decoy",
            created_at=edge.created_at,
        )
        for index, edge in enumerate(edges)
    ]
    session.add_all([*page_links, *edges])
    session.flush()
    return tuple(page.id for page in pages)


def add_cross_workspace_decoys(
    session: Session,
    *,
    target_corpus: WorkspaceGraphCorpus,
    count: int = 1_025,
) -> UUID:
    """Add a separate published workspace with high-weight graph rows."""

    corpus_key = f"{target_corpus.name}-foreign-decoys"
    workspace_id = fixture_uuid(corpus_key, "workspace")
    timestamp = fixture_time(200_000)
    session.add(
        Workspace(
            id=workspace_id,
            slug=f"workspace-graph-{corpus_key}",
            title="Foreign Privacy Decoys",
            name="Foreign Privacy Decoys",
            description="Must not affect the target workspace scene",
            visibility="private",
            join_policy="invite_only",
            created_by_actor_id=fixture_uuid(corpus_key, "creator"),
            owner_actor_id=fixture_uuid(corpus_key, "owner"),
            created_at=timestamp,
            updated_at=timestamp,
        )
    )
    session.flush()

    pages: list[ContextPage] = []
    versions: list[ContextPageVersion] = []
    for index in range(count):
        page_id = fixture_uuid(corpus_key, "context", index)
        version_id = fixture_uuid(corpus_key, "context-version", index)
        instant = timestamp + timedelta(seconds=index)
        pages.append(
            ContextPage(
                id=page_id,
                workspace_id=workspace_id,
                slug=f"foreign-privacy-sentinel-{index:04d}",
                title=f"FOREIGN PRIVACY SENTINEL {index:04d}",
                page_type="concept",
                lifecycle_status="published",
                created_by_actor_type="human",
                created_by_actor_id=fixture_uuid(corpus_key, "author"),
                created_at=instant,
                updated_at=instant,
            )
        )
        versions.append(
            ContextPageVersion(
                workspace_id=workspace_id,
                id=version_id,
                page_id=page_id,
                version_no=1,
                body_md="# FOREIGN PRIVACY SENTINEL",
                content_hash=f"foreign-decoy-{index:04d}",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id=str(fixture_uuid(corpus_key, "author")),
                version_status="published_snapshot",
                created_at=instant,
            )
        )
    session.add_all(pages)
    session.flush()
    session.add_all(versions)
    session.flush()
    for index, page in enumerate(pages):
        page.current_version_id = versions[index].id
        page.current_published_version_id = versions[index].id
        page.updated_at = timestamp + timedelta(seconds=index)
        flag_modified(page, "updated_at")

    edges: list[EdgeProjection] = []
    page_links: list[PageLink] = []
    for index in range(count):
        instant = timestamp + timedelta(seconds=index)
        page_links.append(
            PageLink(
                id=fixture_uuid(corpus_key, "page-link", index),
                workspace_id=workspace_id,
                source_page_id=pages[index].id,
                source_version_id=versions[index].id,
                target_slug=pages[(index + 1) % count].slug,
                target_page_id=pages[(index + 1) % count].id,
                occurrence_count=1_000_000 - index,
                first_snippet="Foreign high-weight decoy",
                created_at=instant,
            )
        )
        edges.append(
            EdgeProjection(
                id=fixture_uuid(corpus_key, "edge", index),
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=pages[index].id,
                target_kind="context_page",
                target_id=pages[(index + 1) % count].id,
                edge_kind="wikilink",
                provenance="curated",
                weight=float(1_000_000 - index),
                occurred_at=instant,
                source_ref={"fixture": "cross-workspace-decoy"},
                created_at=instant,
                last_seen_at=instant,
            )
        )
    session.add_all([*page_links, *edges])
    session.flush()
    return workspace_id


def corpus_actor(
    corpus: WorkspaceGraphCorpus,
    *,
    role: Literal["viewer", "editor", "owner"],
    token_kind: Literal["session", "api_token"] = "session",
    scopes: tuple[str, ...] = (),
) -> ActorContext:
    actor_id = fixture_uuid(
        corpus.name,
        "actor",
        role,
        token_kind,
        ",".join(scopes),
    )
    return ActorContext(
        actor_kind="human",
        actor_id=str(actor_id),
        user_id=actor_id,
        token_kind=token_kind,
        scopes=scopes,
        workspace_role=role,
        role=role,
        workspace_id=corpus.workspace_id,
        provenance=(
            "personal_access_token" if token_kind == "api_token" else "browser_session"
        ),
    )


def seed_benchmark_principals(
    session: Session,
    corpus: WorkspaceGraphCorpus,
) -> tuple[BenchmarkPrincipal, ...]:
    """Persist deterministic users for HTTP and browser benchmark adapters.

    ``restricted_reader`` deliberately has no PAT/OAuth row: the current
    product cannot issue ``library:restricted-read``.  F00's HTTP adapter
    supplies that synthetic scope through ``get_rest_actor`` while still using
    this real viewer membership, and records that auth mode in its result. All
    three users can log in to the browser harness with ``BENCHMARK_PASSWORD``.
    """

    definitions: tuple[
        tuple[
            Literal["owner", "viewer", "restricted_reader"],
            Literal["owner", "viewer"],
            tuple[str, ...],
        ],
        ...,
    ] = (
        ("owner", "owner", ()),
        ("viewer", "viewer", ()),
        ("restricted_reader", "viewer", ("library:restricted-read",)),
    )
    principals: list[BenchmarkPrincipal] = []
    workspace = session.get(Workspace, corpus.workspace_id)
    if workspace is None:
        raise ValueError("benchmark principals require a persisted workspace")

    for name, role, scopes in definitions:
        user_id = fixture_uuid(corpus.name, "benchmark-principal", name)
        email = f"graph-{name.replace('_', '-')}+{corpus.workspace_id.hex}@example.test"
        session.add(
            User(
                id=user_id,
                email=email,
                email_verified_at=fixture_time(),
                password_hash=BENCHMARK_PASSWORD_HASH,
                display_name=f"Graph benchmark {name.replace('_', ' ')}",
                status="active",
                default_workspace_id=corpus.workspace_id,
                created_at=fixture_time(),
                updated_at=fixture_time(),
            )
        )
        session.add(
            WorkspaceMembership(
                user_id=user_id,
                workspace_id=corpus.workspace_id,
                role=role,
                joined_at=fixture_time(),
            )
        )
        principals.append(
            BenchmarkPrincipal(
                name=name,
                user_id=user_id,
                email=email,
                role=role,
                synthetic_scopes=scopes,
            )
        )
        if role == "owner":
            # The Workspace update points at this User.  Flush the principal
            # first because no ORM relationship connects these scalar FK
            # assignments for SQLAlchemy to order automatically.
            session.flush()
            workspace.owner_actor_id = user_id
            workspace.owner_user_id = user_id
            workspace.updated_at = fixture_time()
            flag_modified(workspace, "updated_at")

    session.flush()
    return tuple(principals)
