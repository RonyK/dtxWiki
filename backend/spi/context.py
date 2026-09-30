"""Stable request-context and database-routing contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker


class DatabaseProvider(Protocol):
    def control_session_factory(self) -> sessionmaker[Session]: ...

    def session_for(self, workspace_id: UUID) -> sessionmaker[Session]: ...


@dataclass(frozen=True)
class ToolCallContext:
    actor: Any
    db: DatabaseProvider
    page_service: Any
    search_service: Any | None = None
    conversation_token: str | None = None
    services: Any | None = None
    workspace_id: UUID | None = None

    @property
    def session_factory(self) -> sessionmaker[Session]:
        if self.workspace_id is not None:
            return self.db.session_for(self.workspace_id)
        return self.db.control_session_factory()
