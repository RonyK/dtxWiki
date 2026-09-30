from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Timezone-aware DateTime that guarantees UTC tzinfo on read.

    SQLite has no native timezone type, so a ``DateTime(timezone=True)`` column
    round-trips UTC instants as *naive* datetimes there. Pydantic then serializes
    them without an offset and clients parse them as local time (off by the
    viewer's UTC offset). This decorator re-attaches UTC on the way in and out so
    every dialect yields offset-aware UTC values and the serialized contract
    always carries "+00:00".
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value, dialect):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


class UuidPrimaryKeyMixin:
    id: Mapped[object] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
