"""Content chunk + embedding storage — Epic-1 Sprint-1 Phase 3.

``content_chunk`` is the SQL source of truth (dialect-neutral): the chunked,
offset-preserving slices of Context published/draft bodies and Library
extracted text. ``chunk_embedding`` is the rebuildable derived layer, one row
per (chunk, provider, model) so multiple models can coexist during a reindex.

The embedding is stored as packed float32 bytes (``LargeBinary``) on every
dialect — the dependency-zero brute-force VectorStore reads it directly. A
pgvector ``vector(dim)`` column + ANN index is a future optimization layered
behind the same VectorStore protocol once profiling proves brute-force
insufficient (guardrail: no pgvector/ANN dependency until then).
"""

from uuid import UUID

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin


class ContentChunk(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "content_chunk"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "document_kind",
            "document_id",
            "content_scope",
            "ordinal",
            name="content_chunk_document_scope_ordinal_uq",
        ),
        Index(
            "idx_content_chunk_document",
            "workspace_id",
            "document_kind",
            "document_id",
            "content_scope",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_content_chunk_workspace_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id"), nullable=False
    )
    # Stable document identity (NOT the version) so a re-publish swaps the
    # document's chunks rather than leaving the old version's behind.
    document_kind: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    content_scope: Mapped[str] = mapped_column(
        Text, nullable=False, default="published"
    )
    # The specific version that was chunked — for citation provenance only.
    source_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # Markdown breadcrumb prepended to the embedding input (contextual chunk
    # header); NOT counted in the char offsets below.
    heading_path: Mapped[str | None] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_text_hash: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_content_hash: Mapped[str | None] = mapped_column(Text)

    embeddings: Mapped[list["ChunkEmbedding"]] = relationship(
        back_populates="chunk",
        cascade="all, delete-orphan",
    )


class ChunkEmbedding(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "chunk_embedding"
    __table_args__ = (
        UniqueConstraint(
            "chunk_id",
            "provider",
            "model",
            name="chunk_embedding_chunk_provider_model_uq",
        ),
        Index("idx_chunk_embedding_active", "provider", "model"),
        # Epic-3 D-3 final schema: workspace-scoped, not just chunk-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "chunk_id"],
            ["content_chunk.workspace_id", "content_chunk.id"],
            name="fk_chunk_embedding_chunk_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    # Epic-3 D-2: denormalized from `chunk` (content_chunk.workspace_id is
    # already NOT NULL, so this is a plain backfill).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    chunk_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    # Packed little-endian float32 vector (dim * 4 bytes).
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    chunk: Mapped["ContentChunk"] = relationship(back_populates="embeddings")
