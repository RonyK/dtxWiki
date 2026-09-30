"""PURGE_AUTHZ_PLAN: mint and consume the single-use workspace-purge grant.

Implements PURGE_AUTHZ_PLAN S-3/S-4/S-5. ``issue_purge_grant`` is called by
the DELETE route from a *separate*, freshly-committed transaction after a 409
refusal (the refusing transaction itself rolls back, so a grant written
inside it would vanish before the response reached the client).
``consume_purge_grant`` is called by the same route, in the SAME transaction
as the purge it gates, as one conditional DELETE -- not a read followed by a
delete, which would let two concurrent requests both observe the grant as
valid.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.workspace_purge_grant import WorkspacePurgeGrant
from backend.services.credential_hashing import CredentialHasher

# S-4: long enough for a human to read the blocker counts and confirm, short
# enough that a forgotten browser tab does not leave a live destructive grant
# lying around.
PURGE_GRANT_TTL = timedelta(minutes=5)


def issue_purge_grant(
    session: Session, *, user_id: UUID, workspace_id: UUID
) -> tuple[str, datetime]:
    """Mint (or replace) the one grant for this (user, workspace) pair.

    Composite-PK upsert in a single statement: a repeated refusal reissues
    over the same row instead of accumulating one per attempt (S-5), and two
    concurrent refusals converge on one row rather than colliding. The
    previous plain token stops working immediately, since only the newest hash
    is stored.
    """

    hasher = CredentialHasher()
    plain_token = hasher.generate_purge_grant_token()
    token_hash = hasher.purge_grant_lookup_hash(plain_token)
    expires_at = utc_now() + PURGE_GRANT_TTL
    values = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "token_hash": token_hash,
        "expires_at": expires_at,
    }
    # One statement, not read-then-write. Two refusals for the same
    # (user, workspace) can race: both would observe no row, both would INSERT,
    # and the second commit would raise IntegrityError on the composite primary
    # key -- reaching the route as a 500 instead of the 409 the caller is owed.
    # Dialect handling mirrors job_hints._upsert_hint.
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(WorkspacePurgeGrant).values(**values)
    elif dialect == "postgresql":
        statement = postgresql_insert(WorkspacePurgeGrant).values(**values)
    else:
        existing = session.get(WorkspacePurgeGrant, (user_id, workspace_id))
        if existing is None:
            session.add(WorkspacePurgeGrant(**values))
        else:
            existing.token_hash = token_hash
            existing.expires_at = expires_at
        return plain_token, expires_at
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id", "workspace_id"],
            set_={"token_hash": token_hash, "expires_at": expires_at},
        )
    )
    # The losing racer's token is the one that stops working, and the ORM
    # identity map may still hold the pre-upsert row; drop it so a later read in
    # this session sees what the statement actually wrote.
    session.expire_all()
    return plain_token, expires_at


def consume_purge_grant(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    token: str | None,
) -> bool:
    """Validate and consume a purge grant in one conditional DELETE.

    S-4: verification and consumption are the same SQL statement so two
    requests racing on the same grant cannot both see it as valid -- the
    database's own row locking serializes them, and only one DELETE can
    affect a row. Returns ``True`` iff exactly one row matched (a malformed,
    missing, expired, or mismatched-identity token all return ``False``, with
    no distinction -- S-8 requires the caller's refusal to be uniform).
    """

    if not token:
        return False
    try:
        token_hash = CredentialHasher().purge_grant_lookup_hash(token)
    except ValueError:
        return False
    result = session.execute(
        delete(WorkspacePurgeGrant).where(
            WorkspacePurgeGrant.user_id == user_id,
            WorkspacePurgeGrant.workspace_id == workspace_id,
            WorkspacePurgeGrant.token_hash == token_hash,
            WorkspacePurgeGrant.expires_at > utc_now(),
        )
    )
    return result.rowcount == 1
