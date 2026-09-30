from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


class OAuthClient(CreatedAtMixin, Base):
    __tablename__ = "oauth_clients"
    __table_args__ = (
        UniqueConstraint(
            "registration_identity",
            name="uq_oauth_client_registration_identity",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_name: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uris: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    token_endpoint_auth_method: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="none",
    )
    grant_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    response_types: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    application_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="native",
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    created_via: Mapped[str] = mapped_column(Text, nullable=False, default="dcr")
    registration_identity: Mapped[str | None] = mapped_column(Text)
    # Exact verified registration metadata may add a presentation association.
    # These values never participate in OAuth client/grant authorization.
    catalog_identity_fingerprint: Mapped[str | None] = mapped_column(Text)
    catalog_app_id: Mapped[str | None] = mapped_column(Text)
    catalog_app_label_snapshot: Mapped[str | None] = mapped_column(Text)
    catalog_revision: Mapped[str | None] = mapped_column(Text)
    catalog_association_provenance: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class OAuthConsent(Base):
    __tablename__ = "oauth_consents"

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    client_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("oauth_clients.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    # Ceiling scope for this (user, client) pair -- kept in control plane as
    # a separate defense line above the effective-scope intersection
    # (D-01v3-b/Q4). Identical across every row of the same (user, client);
    # the service layer keeps that invariant, not a DB constraint.
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    # Immutable copy from OAuthClient at the time consent is first granted.
    catalog_app_id: Mapped[str | None] = mapped_column(Text)
    catalog_app_label_snapshot: Mapped[str | None] = mapped_column(Text)
    catalog_revision: Mapped[str | None] = mapped_column(Text)
    refresh_ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sliding_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    granted_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
    # D-01v3 control-plane sync index for this row's workspace_credential_grant
    # replica -- same shape/purpose as ApiTokenWorkspace's matching columns,
    # see .claude/plan/260909-p10-3-multi-workspace-credential.md section 2.6.
    desired_scopes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sync_state: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending"
    )
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_sync_error: Mapped[str | None] = mapped_column(Text)


class OAuthCode(Base):
    __tablename__ = "oauth_codes"

    code_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("oauth_clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )


class OAuthRefreshToken(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "oauth_refresh_tokens"
    __table_args__ = (
        Index(
            "ix_oauth_refresh_tokens_user_client_expires_at",
            "user_id",
            "client_id",
            "expires_at",
        ),
    )

    token_lookup_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    grant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    hashed_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("oauth_clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    rotated_to_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    # Section 2.3 grace-reuse window: set when this row is rotated (superseded
    # by rotated_to_id), read back to judge whether a reuse of THIS row within
    # 30s of THIS timestamp is the grace case or real reuse. rotated_from_id
    # is the reverse pointer on the child so a grace-reissued second child
    # doesn't erase the branch: both children carry the same rotated_from_id,
    # letting the family tree be reconstructed from either direction even
    # though the parent's own rotated_to_id keeps pointing at the first child.
    rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    rotated_from_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class OAuthAccessTokenJti(CreatedAtMixin, Base):
    """Sprint_5 Phase_3 Task 3.4 (ADR A5): one row per issued access JWT.

    Persisting the ``jti`` claim lets refresh-reuse detection cascade through
    ``refresh_token_id`` to every still-live access JWT in the same stable
    authorization grant. The bearer-token decode path
    short-circuits on ``revoked_at IS NOT NULL`` with a 1-minute LRU cache
    so the hot path remains DB-light. The refresh-token FK is RESTRICT so
    expiry cleanup cannot delete denylist/audit rows before the access JWT
    expires. The OAuth schema creates the table + three indices (user/client
    fan-out, refresh-token join, partial revoked_at for denylist lookups).
    """

    __tablename__ = "oauth_access_token_jti"

    jti: Mapped[str] = mapped_column(Text, primary_key=True)
    refresh_token_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("oauth_refresh_tokens.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("oauth_clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
