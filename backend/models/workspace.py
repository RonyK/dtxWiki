from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)
from backend.models.normalized_keys import (
    normalize_search_key,
    workspace_directory_title_key_default,
)

if TYPE_CHECKING:
    from backend.models.user import User


def default_actor_id() -> UUID:
    return UUID("00000000-0000-0000-0000-000000000010")


# Product default for newly inserted workspaces. Existing rows are moved to the
# current shape by an Alembic data migration rather than a read-time legacy
# branch.
NEW_LIBRARY_MAX_UPLOAD_BYTES = 20_000_000


def default_library_policy() -> dict[str, Any]:
    return {
        # No sample web rule: example.com is a reserved documentation domain, and
        # seeding it as a real rule made every new workspace look like it had a
        # web source configured — the pattern was even identical to the field's
        # own placeholder, so the row could not be told apart from a hint.
        # Retired source-rule keys are not seeded for new workspaces. Existing
        # JSON values remain dormant so rollback never requires a data restore.
        "manual_approval_required": True,
        "policy_schema_version": 5,
        "max_upload_bytes": NEW_LIBRARY_MAX_UPLOAD_BYTES,
        # A new Workspace starts without user-defined group policy. Every
        # enabled format therefore inherits the Workspace default limit unless
        # an Owner deliberately adds a family or exact-format preference.
        "format_overrides": {},
        "builtin_format_overrides": {},
        "allow_store_without_converter": True,
        "format_families": [],
        "custom_formats": [],
        "unavailable_behavior": {
            "preview": "store_and_download",
            "semantic_conversion": "store_and_download",
        },
    }


def default_write_policy() -> dict[str, Any]:
    return {"draft_only": True, "publish_requires_review": True}


def default_citation_policy() -> dict[str, Any]:
    return {
        "strictness": "standard",
        "accepted_citation_types": [
            "raw_source",
            "derived_artifact",
            "context_page",
        ],
    }


def default_agent_injection_policy() -> dict[str, Any]:
    return {
        "bundle_ttl_default_seconds": 3600,
        "bundle_ttl_max_seconds": 86400,
        "category_inclusion_policy": {
            "agent_guide": "always",
            "persona": "default_on",
            "skill": "task_matched",
            "memory": "task_matched",
        },
    }


def default_review_policy() -> dict[str, Any]:
    from backend.services.review_risk_policy import default_review_policy as default

    return default()


class Workspace(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "workspace"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('private', 'team', 'public')",
            name="workspace_visibility_check",
        ),
        CheckConstraint(
            "join_policy IN ('invite_only', 'request')",
            name="workspace_join_policy_check",
        ),
        CheckConstraint(
            "visibility != 'private' OR join_policy = 'invite_only'",
            name="workspace_private_join_policy_check",
        ),
        Index("ix_workspace_directory_title_key", "directory_title_key", "id"),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    directory_title_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=workspace_directory_title_key_default,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    goal: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=(
            "Build a source-grounded workspace for cited answers, reusable notes, "
            "and team knowledge."
        ),
    )
    default_language: Mapped[str] = mapped_column(Text, nullable=False, default="en")
    context_media_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    library_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=default_library_policy,
    )
    write_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=default_write_policy,
    )
    citation_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=default_citation_policy,
    )
    agent_injection_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=default_agent_injection_policy,
    )
    review_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=default_review_policy,
    )
    # Per-workspace lint rule toggles: {rule_id: bool}. Missing entries fall back
    # to each rule's default_enabled. Empty by default.
    lint_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    # Auto-fix mode (F2): when on, draft saves are normalized (safe Markdown
    # formatting) before persisting. Off by default.
    # Which retrieval mode Ask starts from. "auto" keeps the historical
    # hybrid > semantic > keyword ladder, so an existing workspace behaves
    # exactly as before until someone changes it.
    ask_retrieval_mode: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="auto",
        server_default="auto",
    )
    auto_fix_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    # Knowledge graph Settings card. The workspace-wide map is an owner
    # choice and starts off; per-document Local Graphs do not depend on it.
    workspace_graph_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    # Whether usage-derived relations (documents read together in MCP
    # sessions) are projected and shown. A team workspace starts with them on.
    usage_relation_signals_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    edge_projection_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    graph_observation_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    authority_projection_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=-1,
        server_default="-1",
    )
    visibility: Mapped[str] = mapped_column(Text, nullable=False, default="private")
    join_policy: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="invite_only",
        server_default="invite_only",
    )
    created_by_actor_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=default_actor_id,
    )
    owner_actor_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=default_actor_id,
    )
    owner_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    # Set while this workspace waits to be deleted with the named account: the
    # account was its only admin when its deletion was approved. Access and
    # admissions stay closed until an active admin appears or the account is
    # restored; account cleanup deletes the workspace. No foreign key, so the
    # marker never forces a table rebuild; account cleanup clears leftovers.
    account_deletion_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    pages: Mapped[list["ContextPage"]] = relationship(back_populates="workspace")
    mcp_clients: Mapped[list["MCPClient"]] = relationship(back_populates="workspace")
    memberships: Mapped[list["WorkspaceMembership"]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )

    @validates("title")
    def _maintain_directory_title_key(self, _key: str, value: str) -> str:
        self.directory_title_key = normalize_search_key(value)
        return value


class MCPClient(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "mcp_client"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="mcp_client_workspace_name_uq"),
        UniqueConstraint(
            "workspace_id",
            "runtime_binding_key",
            name="mcp_client_workspace_runtime_binding_uq",
        ),
        Index(
            "ix_mcp_client_workspace_deleted_status_created_id",
            "workspace_id",
            "deleted_at",
            "status",
            "created_at",
            "id",
        ),
        # Epic-3 D-3 final schema: parent side of the composite FK its
        # children now use.
        UniqueConstraint("workspace_id", "id", name="uq_mcp_client_workspace_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    client_type: Mapped[str] = mapped_column(Text, nullable=False, default="generic")
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    # Stable, server-derived credential identity used by the MCP pre-dispatch
    # lifecycle gate. It contains only non-secret identifiers (for example an
    # OAuth grant UUID or API-token row UUID), never the bearer itself. Legacy
    # rows remain nullable and are bound on their next authenticated use.
    runtime_binding_key: Mapped[str | None] = mapped_column(Text)
    # User-visible deletion is a tombstone: trace/chat/bundle foreign keys keep
    # pointing at this row while Settings projections omit it permanently.
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    issued_to_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    brand_hint: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    token_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    workspace: Mapped[Workspace] = relationship(back_populates="mcp_clients")


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "workspace_id", name="workspace_membership_uq"),
        CheckConstraint(
            "role IN ('viewer', 'editor', 'owner')",
            name="workspace_membership_role_check",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="workspace_memberships")
    workspace: Mapped[Workspace] = relationship(back_populates="memberships")


from backend.models.context import ContextPage  # noqa: E402
