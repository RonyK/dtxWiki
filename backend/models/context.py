from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import conv

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)

CONTEXT_PAGE_TYPES = (
    "overview",
    "source",
    "entity",
    "concept",
    "synthesis",
    "comparison",
    "decision",
    "contradiction",
    "query",
    "guide",
)
CONTEXT_PAGE_LIFECYCLE_STATUSES = ("draft", "published", "archived")
CONTEXT_PAGE_PUBLIC_EXPOSURE_SCOPES = ("indexed_web", "unlisted_web")
CONTEXT_PAGE_VERSION_STATUSES = (
    "editing",
    "draft",
    "published_snapshot",
    "superseded_snapshot",
    "discarded",
)


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class ContextPage(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "context_page"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_page_ws_slug"),
        UniqueConstraint("workspace_id", "id", name="uq_context_page_workspace_id"),
        CheckConstraint(
            f"page_type IN ({_sql_in(CONTEXT_PAGE_TYPES)})",
            name="context_page_page_type_check",
        ),
        CheckConstraint(
            f"lifecycle_status IN ({_sql_in(CONTEXT_PAGE_LIFECYCLE_STATUSES)})",
            name="context_page_lifecycle_status_check",
        ),
        CheckConstraint(
            "((NOT is_public AND public_exposure_scope IS NULL "
            "AND public_exposed_version_id IS NULL) OR "
            "(is_public AND public_exposure_scope IS NOT NULL "
            "AND public_exposed_version_id IS NOT NULL))",
            name="context_page_public_exposure_complete_check",
        ),
        CheckConstraint(
            "public_exposure_scope IS NULL OR "
            "public_exposure_scope IN "
            f"({_sql_in(CONTEXT_PAGE_PUBLIC_EXPOSURE_SCOPES)})",
            name="context_page_public_exposure_scope_check",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # page-scoped (was a 2-column ownership FK).
        ForeignKeyConstraint(
            ["workspace_id", "id", "public_exposed_version_id"],
            [
                "context_page_version.workspace_id",
                "context_page_version.page_id",
                "context_page_version.id",
            ],
            name="fk_context_page_id_workspace_scoped",
            ondelete="RESTRICT",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "current_published_version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name="fk_context_page_current_published_version_id_workspace_scoped",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "latest_draft_version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name="fk_context_page_latest_draft_version_id_workspace_scoped",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    page_type: Mapped[str] = mapped_column(Text, nullable=False, default="overview")
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    lifecycle_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="draft",
    )
    current_published_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    # Compatibility projection for the durable public exposure grant below.
    # It may be true only when both scope and pinned version are populated.
    is_public: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    public_exposure_scope: Mapped[str | None] = mapped_column(Text)
    public_exposed_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    latest_draft_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    stale_candidate: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    created_from_ask_session_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    placement_parent_node_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    placement_reason: Mapped[str | None] = mapped_column(Text)
    placement_confidence: Mapped[str | None] = mapped_column(Text)
    created_by_actor_type: Mapped[str | None] = mapped_column(Text)
    created_by_actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    content_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="pages")
    versions: Mapped[list["ContextPageVersion"]] = relationship(
        back_populates="page",
        foreign_keys="ContextPageVersion.page_id",
    )
    current_version: Mapped["ContextPageVersion | None"] = relationship(
        # `current_version_id` has no FK constraint (unlike the other three
        # version-pointer columns below), so the join has to be named
        # explicitly rather than inferred from one.
        primaryjoin="ContextPage.current_version_id == ContextPageVersion.id",
        foreign_keys=[current_version_id],
        viewonly=True,
    )
    current_published_version: Mapped["ContextPageVersion | None"] = relationship(
        foreign_keys=[current_published_version_id],
        post_update=True,
    )
    public_exposed_version: Mapped["ContextPageVersion | None"] = relationship(
        foreign_keys=[public_exposed_version_id],
        post_update=True,
    )
    latest_draft_version: Mapped["ContextPageVersion | None"] = relationship(
        foreign_keys=[latest_draft_version_id],
        post_update=True,
    )


class ContextPageVersion(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "context_page_version"
    __table_args__ = (
        UniqueConstraint("page_id", "version_no", name="context_page_version_no_uq"),
        UniqueConstraint("page_id", "id", name="uq_context_page_version_page_id"),
        # Epic-3 D-3 final schema: parent side of the composite
        # ownership FKs context_page/context_media_usage now use.
        UniqueConstraint(
            "workspace_id",
            "page_id",
            "id",
            name="uq_context_page_version_workspace_page_owner",
        ),
        CheckConstraint(
            f"version_status IN ({_sql_in(CONTEXT_PAGE_VERSION_STATUSES)})",
            name="context_page_version_status_check",
        ),
        Index("idx_cpv_page_created", "page_id", "created_at"),
        Index("idx_cpv_parent", "parent_version_id"),
        Index(
            "uq_cpv_current_published_per_page",
            "page_id",
            unique=True,
            sqlite_where=text("version_status = 'published_snapshot'"),
            postgresql_where=text("version_status = 'published_snapshot'"),
        ),
        # P2.2: at most one open (unsubmitted) editing session per
        # page+author. `draft` (submitted, hash-locked, under review) and
        # `published_snapshot` have no such limit -- several authors may each
        # have a draft under review against the same published head at once.
        Index(
            "uq_cpv_current_editing_per_author",
            "page_id",
            "created_by_actor_kind",
            "created_by_actor_id",
            unique=True,
            sqlite_where=text("version_status = 'editing'"),
            postgresql_where=text("version_status = 'editing'"),
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "page_id"],
            ["context_page.workspace_id", "context_page.id"],
            name="fk_context_page_version_page_id_workspace_scoped",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_context_page_version_workspace_id"
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # version-scoped (self-referential -- D-3b).
        ForeignKeyConstraint(
            ["workspace_id", "parent_version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name="fk_context_page_version_parent_version_id_workspace_scoped",
        ),
        # Same workspace-scoped self-reference shape as parent_version_id
        # above, for the reconcile merge edge (section 7.1).
        ForeignKeyConstraint(
            ["workspace_id", "merged_into_version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name="fk_context_page_version_merged_into_version_id_workspace_scoped",
        ),
    )

    # Epic-3 D-2: denormalized from `page` -- every join edge to it is a
    # NOT NULL FK, so this is a plain backfill, not a judgment call. Lets
    # a version be routed to its shard without joining context_page first.
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    page_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    parent_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    rendering_profile: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="gfm_mermaid",
    )
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    graph_status: Mapped[str] = mapped_column(Text, nullable=False, default="linear")
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    actor_display_name: Mapped[str | None] = mapped_column(Text)
    change_set_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    version_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="draft",
    )
    # P2.4: bumped on every autosave while version_status == "editing", never
    # reset, never itself creating a new version row. Covers body + title +
    # slug + tags + aliases + private metadata + usage as one monotonic
    # counter -- content_hash alone only covers the markdown body, so it
    # cannot catch a stale client overwriting a metadata-only change with an
    # otherwise-unchanged body (see docstring on the conflict check in
    # backend/repositories/context_visibility.py).
    editing_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # P8.1: the version row's own last-saved timestamp, distinct from
    # `created_at` (set once, at insert). Updated on every real autosave so
    # a caller can show "last saved at" without conflating it with when the
    # row was first opened.
    last_saved_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )
    # Write-time navigational description (first prose paragraph, <=200 chars).
    # NULL on legacy rows = derive at read time; reindex backfills (rebuildable).
    nav_description: Mapped[str | None] = mapped_column(Text)
    # Persisted identity/provenance sidecars for block-addressed edits. Existing
    # versions remain NULL and are derived lazily; new writes preserve the IDs
    # and exact reconciliation evidence that content alone cannot reconstruct.
    block_map: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    reconcile_manifest: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    # Stamped on a diverged branch (this row) at reconcile time, naming the
    # linear version its conflicts were folded into -- the git-merge "second
    # parent" edge. NULL for every non-branch version and for a branch that
    # re-diverged instead of landing cleanly (see _complete_reconciled_branch).
    merged_into_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    created_by_actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_actor_id: Mapped[str] = mapped_column(Text, nullable=False)

    page: Mapped[ContextPage] = relationship(
        back_populates="versions",
        foreign_keys=[page_id],
    )

    @property
    def body_markdown(self) -> str:
        """Compatibility alias for older ContextPageVersion callers."""

        return self.body_md

    @body_markdown.setter
    def body_markdown(self, value: str) -> None:
        self.body_md = value


class PageLink(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "page_links"
    __table_args__ = (
        UniqueConstraint(
            "source_page_id",
            "source_version_id",
            "locator_key",
            name="uq_page_links_source_version_locator",
        ),
        Index("idx_page_links_target_workspace", "workspace_id", "target_slug"),
        Index("idx_page_links_target_page", "workspace_id", "target_page_id"),
        Index("idx_page_links_source_version", "source_version_id"),
        Index("idx_page_links_source_page", "source_page_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "source_page_id"],
            ["context_page.workspace_id", "context_page.id"],
            name="fk_page_links_source_page_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "target_page_id"],
            ["context_page.workspace_id", "context_page.id"],
            name="fk_page_links_target_page_id_workspace_scoped",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "source_version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name="fk_page_links_source_version_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_page_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    source_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    locator_key: Mapped[str] = mapped_column(Text, nullable=False)
    target_slug: Mapped[str | None] = mapped_column(Text)
    target_page_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_snippet: Mapped[str | None] = mapped_column(Text)


class PageNameIndex(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "page_name_index"
    __table_args__ = (
        CheckConstraint(
            "name_kind IN ('title', 'slug', 'alias')",
            name="page_name_index_name_kind_check",
        ),
        CheckConstraint(
            "visibility_scope IN ('published', 'draft')",
            name="page_name_index_visibility_scope_check",
        ),
        Index("idx_page_name_index_lookup", "workspace_id", "name_norm"),
        Index("idx_page_name_index_page", "page_id", "source_version_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "page_id"],
            ["context_page.workspace_id", "context_page.id"],
            name="fk_page_name_index_page_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "source_version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name="fk_page_name_index_source_version_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    page_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    source_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    visibility_scope: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_kind: Mapped[str] = mapped_column(Text, nullable=False)


class PageProvenance(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Normalized page→origin relation (G0 §19.5.7 item 3).

    Replaces the ContextPage.created_from_ask_turn_ids JSON array: rows are
    queryable/joinable for provenance edges (ask_turn today, other ingress
    sources later).
    """

    __tablename__ = "page_provenance"
    __table_args__ = (
        UniqueConstraint(
            "page_id",
            "source_kind",
            "source_id",
            name="page_provenance_uq",
        ),
        Index("idx_page_provenance_source", "source_kind", "source_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "page_id"],
            ["context_page.workspace_id", "context_page.id"],
            name="fk_page_provenance_page_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    # Epic-3 D-2: denormalized from `page` (see ContextPageVersion.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    page_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class EdgeProjection(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "edge_projection"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "source_kind",
            "source_id",
            "target_kind",
            "target_id",
            "edge_kind",
            name="uq_edge_projection_identity",
        ),
        CheckConstraint(
            "provenance IN ('curated', 'derived', 'usage')",
            name="edge_projection_provenance_check",
        ),
        Index("idx_edge_projection_source", "workspace_id", "source_kind", "source_id"),
        Index("idx_edge_projection_target", "workspace_id", "target_kind", "target_id"),
        Index("idx_edge_projection_kind", "workspace_id", "edge_kind"),
        Index(
            "idx_edge_projection_ppr_source",
            "workspace_id",
            "source_kind",
            "source_id",
            "provenance",
            "weight",
            "id",
        ),
        Index(
            "idx_edge_projection_ppr_target",
            "workspace_id",
            "target_kind",
            "target_id",
            "provenance",
            "weight",
            "id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    edge_kind: Mapped[str] = mapped_column(Text, nullable=False)
    # G1 read-model fields (design 19.3): curated > derived > usage lineage,
    # occurrence/trust weight, and the source-row instant for temporal decay.
    provenance: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="curated",
    )
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    occurred_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source_ref: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class ContextClaim(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "context_claim"
    __table_args__ = (
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # version-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "page_version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name="fk_context_claim_page_version_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    # Epic-3 D-2: denormalized from `page_version` (see
    # ContextPageVersion.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    page_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    location_in_page: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    claim_type: Mapped[str] = mapped_column(Text, nullable=False, default="fact")
    confidence: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    is_core: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    core_reason: Mapped[str | None] = mapped_column(Text)
    created_by_actor_type: Mapped[str | None] = mapped_column(Text)


class ContextCitation(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "context_citation"
    __table_args__ = (
        CheckConstraint(
            "(citation_type = 'raw_source' "
            "AND library_item_version_id IS NOT NULL "
            "AND cited_context_page_version_id IS NULL) "
            "OR (citation_type = 'derived_artifact' "
            "AND library_artifact_id IS NOT NULL "
            "AND cited_context_page_version_id IS NULL) "
            "OR (citation_type = 'context_page' "
            "AND cited_context_page_version_id IS NOT NULL "
            "AND library_item_version_id IS NULL "
            "AND library_artifact_id IS NULL)",
            name="context_citation_type_source_xor",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # source-scoped (each column is one arm of the XOR above).
        ForeignKeyConstraint(
            ["workspace_id", "library_item_version_id"],
            ["library_item_version.workspace_id", "library_item_version.id"],
            name="fk_context_citation_library_item_version_id_workspace_scoped",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "library_artifact_id"],
            ["library_artifact.workspace_id", "library_artifact.id"],
            name="fk_context_citation_library_artifact_id_workspace_scoped",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "cited_context_page_version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name=conv(
                "fk_context_citation_cited_context_page_version_id_workspace_scoped"
            ),
            ondelete="RESTRICT",
        ),
    )

    # Epic-3 D-2: denormalized from whichever of the three source refs below
    # the citation_type XOR check picks -- see 0015's dependency-ordered
    # per-branch backfill.
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_location: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    library_item_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    library_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    cited_context_page_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    location: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    snippet: Mapped[str | None] = mapped_column(Text)
    citation_type: Mapped[str] = mapped_column(Text, nullable=False)
    verification_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="weak",
    )
    created_via: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="cite_tool",
    )
    created_by_actor_type: Mapped[str | None] = mapped_column(Text)


from backend.models.workspace import Workspace  # noqa: E402
