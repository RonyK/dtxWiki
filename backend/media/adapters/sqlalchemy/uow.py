"""SQLAlchemy media unit-of-work composition."""

from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from .outbox import SqlAlchemyMediaOutbox
from .quota import SqlAlchemyPhysicalQuota
from .repository import SqlAlchemyMediaRepository


class SqlAlchemyMediaUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self.repository: SqlAlchemyMediaRepository
        self.physical_quota: SqlAlchemyPhysicalQuota
        self.outbox: SqlAlchemyMediaOutbox
        self._committed = False

    def __enter__(self) -> "SqlAlchemyMediaUnitOfWork":
        if self._session is not None:
            raise RuntimeError("media unit of work is already active")
        self._session = self._session_factory()
        self.repository = SqlAlchemyMediaRepository(self._session)
        self.physical_quota = SqlAlchemyPhysicalQuota(self._session)
        self.outbox = SqlAlchemyMediaOutbox(self._session)
        self._committed = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc, traceback
        if self._session is None:
            return False
        try:
            if exc_type is not None or not self._committed:
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None
        return False

    def commit(self) -> None:
        session = self._require_session()
        session.commit()
        self._committed = True

    def rollback(self) -> None:
        session = self._require_session()
        session.rollback()
        self._committed = False

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("media unit of work is not active")
        return self._session


class SqlAlchemyMediaUnitOfWorkFactory:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> SqlAlchemyMediaUnitOfWork:
        return SqlAlchemyMediaUnitOfWork(self._session_factory)
