"""Access Key expiry, on the account inbox.

Derived on read from the user's own Access Keys (PATs). A key that is not
revoked shows from ``AccessKeyExpiryConfig.warn_before`` ahead of its expiry
until ``keep_after_expiry`` after it. The revision is the stage, ``expiring``
then ``expired``, so the item becomes unread again when the key expires.
Keys without an expiry and OAuth connections, which renew while in use, are
not shown.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.api_token import ApiToken
from backend.models.base import utc_now
from backend.services.inbox.base import (
    ComputedScan,
    InboxDisplay,
    InboxKind,
    InboxPosition,
    InboxSnapshot,
    InboxViewer,
)
from backend.services.inbox.config import AccessKeyExpiryConfig
from backend.services.inbox.links import ACCESS_KEYS_PATH
from backend.services.inbox.scopes import AccountInbox
from backend.services.inbox.sources import ComputedSource
from backend.services.inbox.status_pagination import chronological_after, uuid_key
from backend.utils.uuid import parse_uuid_or_none

ACCESS_KEY_EXPIRY = "access_key_expiry"


def _aware(moment: datetime) -> datetime:
    # SQLite round-trips timestamps without an offset; they are UTC.
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class _ExpiringKeys:
    def __init__(self, config: AccessKeyExpiryConfig) -> None:
        self.config = config

    def _conditions(self, viewer: InboxViewer, now: datetime):
        return (
            ApiToken.user_id == viewer.user_id,
            ApiToken.revoked_at.is_(None),
            ApiToken.expires_at.is_not(None),
            ApiToken.expires_at <= now + self.config.warn_before,
            ApiToken.expires_at > now - self.config.keep_after_expiry,
        )

    def _snapshot(self, token: ApiToken, now: datetime) -> InboxSnapshot:
        expires_at = _aware(token.expires_at)
        expired = expires_at <= now
        return InboxSnapshot(
            str(token.id),
            "expired" if expired else "expiring",
            expires_at if expired else expires_at - self.config.warn_before,
            {
                "name": token.name,
                "expires_at": expires_at.isoformat(),
                "visible_until": (
                    expires_at + self.config.keep_after_expiry
                ).isoformat(),
            },
        )

    def scan(
        self,
        session: Session,
        viewer: InboxViewer,
        limit: int,
        after: InboxPosition | None = None,
    ) -> ComputedScan:
        now = utc_now()
        conditions = self._conditions(viewer, now)
        snapshots = []
        # Each stage is monotone in expires_at. Merge two bounded queries
        # rather than subtracting intervals in SQL, which loses subsecond
        # precision on SQLite and differs from PostgreSQL date arithmetic.
        for expired in (True, False):
            branch_after = after
            if after is not None and not expired:
                try:
                    branch_after = (
                        after[0] + self.config.warn_before,
                        after[1],
                        after[2],
                    )
                except OverflowError:
                    # Every representable expiry precedes this boundary.
                    branch_after = None
            tokens = session.scalars(
                select(ApiToken)
                .where(
                    *conditions,
                    ApiToken.expires_at <= now
                    if expired
                    else ApiToken.expires_at > now,
                    chronological_after(
                        ApiToken.expires_at,
                        ACCESS_KEY_EXPIRY,
                        branch_after,
                        lambda key: ApiToken.id < uuid_key(key),
                    ),
                )
                .order_by(ApiToken.expires_at.desc(), ApiToken.id.desc())
                .limit(limit + 1)
            ).all()
            snapshots.extend(self._snapshot(token, now) for token in tokens)
        snapshots.sort(key=lambda item: (item.event_at, item.item_key), reverse=True)
        return ComputedScan(snapshots[:limit], len(snapshots) > limit)

    def lookup(
        self, session: Session, viewer: InboxViewer, item_keys: Sequence[str]
    ) -> Mapping[str, InboxSnapshot]:
        ids = {token_id for key in item_keys if (token_id := parse_uuid_or_none(key))}
        if not ids:
            return {}
        now = utc_now()
        tokens = session.scalars(
            select(ApiToken).where(ApiToken.id.in_(ids), *self._conditions(viewer, now))
        )
        return {
            snapshot.item_key: snapshot
            for snapshot in (self._snapshot(token, now) for token in tokens)
        }


def _resolve(
    _session: Session, _viewer: InboxViewer, snapshots: Sequence[InboxSnapshot]
) -> Mapping[str, InboxDisplay]:
    # The source already limits items to the viewer's own keys.
    return {
        snapshot.item_key: InboxDisplay(
            title=(snapshot.ref or {}).get("name"),
            href=ACCESS_KEYS_PATH,
            params={
                "stage": snapshot.revision,
                "expires_at": (snapshot.ref or {}).get("expires_at"),
                "visible_until": (snapshot.ref or {}).get("visible_until"),
            },
        )
        for snapshot in snapshots
    }


def access_key_expiry_kind(config: AccessKeyExpiryConfig) -> InboxKind:
    keys = _ExpiringKeys(config)
    return InboxKind(
        ACCESS_KEY_EXPIRY,
        AccountInbox.name,
        ComputedSource(scan=keys.scan, lookup=keys.lookup),
        _resolve,
        reopen_on_new_revision=True,
    )
