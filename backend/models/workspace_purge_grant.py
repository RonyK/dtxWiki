from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, CreatedAtMixin, UTCDateTime


class WorkspacePurgeGrant(CreatedAtMixin, Base):
    """PURGE_AUTHZ_PLAN S-5: the single-use, short-lived confirmation a 409
    workspace-delete refusal mints so the owner's follow-up ``?purge=true``
    request can prove it is the same human who saw the blocker counts.

    A workspace-plane table, not control-plane: it must be purged along with
    the rest of the workspace's rows (and, on a sharded overlay, live in the
    same database as the purge transaction it gates -- see S-4's "same
    session" requirement). ``user_id`` deliberately carries no FK, matching
    ``WorkspaceCredentialGrant.user_id``/``credential_id``
    (``backend/models/workspace_credential_grant.py``): a workspace-plane
    table does not FK into the control plane.

    ``token_hash`` is a lookup digest only (``CredentialHasher.
    purge_grant_lookup_hash``), never the plain token. Composite PK on
    ``(user_id, workspace_id)`` -- at most one live grant per (user,
    workspace) -- backs the upsert-on-reissue behaviour: a repeated refusal
    replaces the previous grant instead of accumulating rows.
    """

    __tablename__ = "workspace_purge_grant"

    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
