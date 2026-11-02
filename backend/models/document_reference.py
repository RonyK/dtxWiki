"""Version-owned authored bibliography and immutable publication projections."""

from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.domain.document_reference import DocumentRef
from backend.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin


def _constraints(table: str, owner: str):
    return (
        ForeignKeyConstraint(
            ["workspace_id", owner],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name=f"fk_{table}_owner",
            ondelete="CASCADE",
        ),
        *(
            ForeignKeyConstraint(
                ["workspace_id", column],
                [f"{target}.workspace_id", f"{target}.id"],
                name=f"fk_{table}_{kind}",
                ondelete="RESTRICT",
            )
            for kind, column, target in (
                ("context", "context_page_id", "context_page"),
                ("library", "library_item_id", "library_item"),
                ("agent", "agent_page_id", "agent_page"),
            )
        ),
        CheckConstraint(
            "(target_kind = 'context_page' AND context_page_id IS NOT NULL "
            "AND library_item_id IS NULL AND agent_page_id IS NULL) "
            "OR (target_kind = 'library_item' AND library_item_id IS NOT NULL "
            "AND context_page_id IS NULL AND agent_page_id IS NULL) "
            "OR (target_kind = 'agent_page' AND agent_page_id IS NOT NULL "
            "AND context_page_id IS NULL AND library_item_id IS NULL)",
            name=f"ck_{table}_target",
        ),
        *(
            Index(f"ix_{table}_{column}", "workspace_id", column)
            for column in ("context_page_id", "library_item_id", "agent_page_id")
        ),
        *(
            UniqueConstraint(owner, column, name=f"uq_{table}_{column}")
            for column in ("context_page_id", "library_item_id", "agent_page_id")
        ),
    )


class _DocumentTarget:
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    context_page_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    library_item_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    agent_page_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))

    @property
    def target_id(self) -> UUID:
        return self.context_page_id or self.library_item_id or self.agent_page_id

    @property
    def reference(self) -> DocumentRef:
        return DocumentRef(self.target_kind, self.target_id)


def target_columns(reference: DocumentRef) -> dict:
    return {
        "target_kind": reference.target_kind,
        {
            "context_page": "context_page_id",
            "library_item": "library_item_id",
            "agent_page": "agent_page_id",
        }[reference.target_kind]: reference.target_id,
    }


class ContextDocumentReference(
    _DocumentTarget, UuidPrimaryKeyMixin, CreatedAtMixin, Base
):
    __tablename__ = "context_document_reference"
    __table_args__ = _constraints("context_doc_ref", "owner_version_id")
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    note: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_actor_kind: Mapped[str | None] = mapped_column(Text)
    created_by_actor_id: Mapped[str | None] = mapped_column(Text)


class ContextPublishedCitation(
    _DocumentTarget, UuidPrimaryKeyMixin, CreatedAtMixin, Base
):
    __tablename__ = "context_published_citation"
    __table_args__ = _constraints("context_pub_cite", "source_version_id")
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    inline_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    legacy_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    occurrences: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(Text)


class ContextCitationPublication(CreatedAtMixin, Base):
    __tablename__ = "context_citation_publication"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name="fk_context_cite_publication_owner",
            ondelete="CASCADE",
        ),
    )
    version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    format_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
