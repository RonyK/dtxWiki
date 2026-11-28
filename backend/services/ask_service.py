"""Ask, assembled from its parts."""

from __future__ import annotations

from backend.services.ask_composer import AskComposer
from backend.services.ask_reader import AskTurnReader
from backend.services.ask_retrieval import AskRetriever
from backend.services.ask_session_service import AskSessionService
from backend.services.ask_turn_store import AskTurnStore
from backend.services.audit_service import AuditService
from backend.services.graph_service import GraphService
from backend.services.search_service import SearchService
from backend.services.tree_read import TreeReadService


class AskService:
    """The Ask application's parts, built once around their shared services.

    Callers use the part that owns the work: ``retriever`` (evidence and
    history), ``composer`` (prompt and model call), ``store`` (questions and
    their endings), ``reader`` (what a reader may see) and ``sessions``.
    """

    def __init__(
        self,
        search_service: SearchService | None = None,
        audit_service: AuditService | None = None,
        tree_read_service: TreeReadService | None = None,
        graph_service: GraphService | None = None,
    ) -> None:
        audit_service = audit_service or AuditService()
        self.retriever = AskRetriever(search_service or SearchService())
        self.composer = AskComposer()
        self.reader = AskTurnReader(
            tree_read_service or TreeReadService(),
            graph_service,
            self.retriever,
            self.composer,
        )
        self.sessions = AskSessionService(audit_service, self.reader)
        self.store = AskTurnStore(audit_service, self.sessions)
