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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.api_token import ApiToken
from backend.models.base import utc_now
from backend.services.inbox.base import (
    ComputedScan,
    InboxDisplay,
    InboxKind,
    InboxSnapshot,
    InboxViewer,
)
from backend.services.inbox.config import AccessKeyExpiryConfig
from backend.services.inbox.links import ACCESS_KEYS_PATH
from backend.services.inbox.scopes import AccountInbox
from backend.services.inbox.sources import ComputedSource
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
            {"name": token.name, "expires_at": expires_at.isoformat()},
        )

    def scan(self, session: Session, viewer: InboxViewer, limit: int) -> ComputedScan:
        now = utc_now()
        conditions = self._conditions(viewer, now)
        total = session.scalar(
            select(func.count()).select_from(ApiToken).where(*conditions)
        )
        tokens = session.scalars(
            select(ApiToken)
            .where(*conditions)
            .order_by(ApiToken.expires_at.asc(), ApiToken.id.asc())
            .limit(limit)
        ).all()
        return ComputedScan(
            [self._snapshot(token, now) for token in tokens], total or 0
        )

    def get(
        self, session: Session, viewer: InboxViewer, item_key: str
    ) -> InboxSnapshot | None:
        now = utc_now()
        token = session.scalar(
            select(ApiToken).where(
                ApiToken.id == parse_uuid_or_none(item_key),
                *self._conditions(viewer, now),
            )
        )
        return self._snapshot(token, now) if token is not None else None


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
            },
        )
        for snapshot in snapshots
    }


def access_key_expiry_kind(config: AccessKeyExpiryConfig) -> InboxKind:
    keys = _ExpiringKeys(config)
    return InboxKind(
        ACCESS_KEY_EXPIRY,
        AccountInbox.name,
        ComputedSource(scan=keys.scan, get=keys.get),
        _resolve,
        reopen_on_new_revision=True,
    )
