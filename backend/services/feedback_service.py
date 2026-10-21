from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.models.feedback import FeedbackItem
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.context_write import (
    FeedbackEntry,
    SubmitFeedbackRequest,
    SubmitFeedbackResult,
)
from backend.schemas.input_policy import validate_request_inputs
from backend.services.actor_visible_read import (
    actor_can_read_agent_page,
    actor_can_read_feedback,
    context_page_visible,
    library_item_visible,
)
from backend.services.audit_service import AuditService
from backend.services.job_queue import JobQueueService
from backend.services.read_errors import ReadNotFoundError
from backend.utils.uuid import parse_uuid_or_none


def _feedback_library_item(
    session: Session, target_type: str, target_id: UUID
) -> LibraryItem | None:
    if target_type in {"source", "library_item"}:
        return session.get(LibraryItem, target_id)
    artifact = session.get(LibraryArtifact, target_id)
    version = (
        session.get(LibraryItemVersion, artifact.library_item_version_id)
        if artifact is not None
        else None
    )
    return (
        session.get(LibraryItem, version.library_item_id)
        if version is not None
        else None
    )


class FeedbackService:
    def __init__(
        self,
        audit_service: AuditService | None = None,
        job_queue: JobQueueService | None = None,
    ) -> None:
        self.audit_service = audit_service or AuditService()
        self.job_queue = job_queue or JobQueueService()

    def submit_feedback(
        self,
        session: Session,
        workspace_id: UUID,
        request: SubmitFeedbackRequest,
        actor_kind: str,
        actor_id: str,
    ) -> SubmitFeedbackResult:
        validate_request_inputs(request)
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        self._validate_target(
            session=session,
            workspace_id=workspace_id,
            target_type=request.target_type,
            target_id=request.target_id,
        )
        feedback = FeedbackItem(
            workspace_id=workspace_id,
            target_type=request.target_type,
            target_id=request.target_id,
            body=request.body,
            severity=request.severity,
            status="open",
            created_by=parse_uuid_or_none(actor_id),
        )
        session.add(feedback)
        session.flush()
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="feedback.submit",
            target_kind="feedback",
            target_id=feedback.id,
            payload={
                "target_type": request.target_type,
                "target_id": str(request.target_id),
                "severity": request.severity,
                "suggested_action": request.suggested_action,
            },
        )
        self.job_queue.enqueue(
            session=session,
            workspace_id=workspace_id,
            job_type="feedback_resolution",
            trigger_type="feedback.submit",
            idempotency_key=f"feedback_resolution:{feedback.id}",
            target_ref={
                "target_kind": "feedback_item",
                "target_id": str(feedback.id),
            },
        )
        return SubmitFeedbackResult(
            feedback_id=feedback.id,
            status="open",
            next_action="agent_resolution_queued",
        )

    def list_feedback(
        self,
        session: Session,
        workspace_id: UUID,
        actor: ActorContext | None = None,
        status: str | None = None,
        target_type: str | None = None,
        limit: int = 50,
    ) -> list[FeedbackEntry]:
        # Feedback is audit-class content: the same gate the single-read
        # (domain_read.read) and search (retrieval.keyword) lanes apply must
        # hold on the list lane. A viewer session / mcp:read token gets an
        # empty list, never the bodies.
        if not actor_can_read_feedback(actor):
            return []
        statement = select(FeedbackItem).where(
            FeedbackItem.workspace_id == workspace_id
        )
        if status is not None:
            statement = statement.where(FeedbackItem.status == status)
        if target_type is not None:
            statement = statement.where(FeedbackItem.target_type == target_type)
        rows = session.scalars(
            statement.order_by(FeedbackItem.created_at.desc()).limit(limit)
        ).all()
        # actor_can_read_feedback gates the audit CLASS, but a feedback about a
        # target the reader cannot see (restricted/archived Library, sensitive
        # Agent, archived/draft-hidden Context) would still re-serve that target
        # via the row's target_id + body. Drop those (Codex R13).
        return [
            self._entry(feedback)
            for feedback in rows
            if self._feedback_target_visible(session, feedback, actor)
        ]

    @staticmethod
    def _feedback_target_visible(
        session: Session,
        feedback: FeedbackItem,
        actor: ActorContext | None,
    ) -> bool:
        if actor is None:
            return True
        target_type, target_id = feedback.target_type, feedback.target_id
        if target_type == "agent_page":
            page = session.get(AgentPage, target_id)
            return (
                page is not None
                and page.lifecycle_status != "archived"
                and actor_can_read_agent_page(page, actor)
            )
        if target_type in {"context_page", "section"}:
            page = session.get(ContextPage, target_id)
            return context_page_visible(page, actor)
        if target_type in {"source", "library_item", "library_artifact"}:
            item = _feedback_library_item(session, target_type, target_id)
            return library_item_visible(item, actor)
        # context_claim/citation/answer resolve to a published page a
        # feedback-capable (editor+) reader can already see.
        return True

    def _validate_target(
        self,
        session: Session,
        workspace_id: UUID,
        target_type: str,
        target_id: UUID,
    ) -> None:
        if target_type == "agent_page":
            page = get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
            if page is None:
                raise ReadNotFoundError("agent page not found")
            return
        if target_type in {"context_page", "section"}:
            page = get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
            if page is None:
                raise ReadNotFoundError("context page not found")
            return
        if target_type == "context_claim":
            if not self._claim_belongs_to_workspace(session, workspace_id, target_id):
                raise ReadNotFoundError("context claim not found")
            return
        if target_type == "context_citation":
            if not self._citation_belongs_to_workspace(
                session,
                workspace_id,
                target_id,
            ):
                raise ReadNotFoundError("context citation not found")
            return
        if target_type == "library_item":
            library_item = get_in_workspace(
                session,
                LibraryItem,
                workspace_id=workspace_id,
                resource_id=target_id,
            )
            if library_item is None:
                raise ReadNotFoundError("library item not found")
            return
        if target_type == "library_artifact":
            if not self._artifact_belongs_to_workspace(
                session,
                workspace_id,
                target_id,
            ):
                raise ReadNotFoundError("source artifact not found")
            return
        if target_type == "answer":
            return
        raise ReadNotFoundError("feedback target not found")

    @staticmethod
    def _claim_belongs_to_workspace(
        session: Session,
        workspace_id: UUID,
        claim_id: UUID,
    ) -> bool:
        claim = get_in_workspace(
            session,
            ContextClaim,
            workspace_id=workspace_id,
            resource_id=claim_id,
        )
        version = (
            get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=claim.page_version_id,
            )
            if claim
            else None
        )
        page = (
            get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=version.page_id,
            )
            if version
            else None
        )
        return page is not None

    def _citation_belongs_to_workspace(
        self,
        session: Session,
        workspace_id: UUID,
        citation_id: UUID,
    ) -> bool:
        citation = get_in_workspace(
            session,
            ContextCitation,
            workspace_id=workspace_id,
            resource_id=citation_id,
        )
        if citation is None:
            return False
        if citation.library_artifact_id is not None:
            return self._artifact_belongs_to_workspace(
                session,
                workspace_id,
                citation.library_artifact_id,
            )
        if citation.library_item_version_id is not None:
            library_item_version = get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=citation.library_item_version_id,
            )
            source = (
                get_in_workspace(
                    session,
                    LibraryItem,
                    workspace_id=workspace_id,
                    resource_id=library_item_version.library_item_id,
                )
                if library_item_version
                else None
            )
            return source is not None
        if citation.cited_context_page_version_id is not None:
            version = get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=citation.cited_context_page_version_id,
            )
            page = (
                get_in_workspace(
                    session,
                    ContextPage,
                    workspace_id=workspace_id,
                    resource_id=version.page_id,
                )
                if version
                else None
            )
            return page is not None
        return False

    @staticmethod
    def _artifact_belongs_to_workspace(
        session: Session,
        workspace_id: UUID,
        artifact_id: UUID,
    ) -> bool:
        artifact = get_in_workspace(
            session,
            LibraryArtifact,
            workspace_id=workspace_id,
            resource_id=artifact_id,
        )
        version = (
            get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=artifact.library_item_version_id,
            )
            if artifact
            else None
        )
        source = (
            get_in_workspace(
                session,
                LibraryItem,
                workspace_id=workspace_id,
                resource_id=version.library_item_id,
            )
            if version
            else None
        )
        return source is not None

    @staticmethod
    def _entry(feedback: FeedbackItem) -> FeedbackEntry:
        return FeedbackEntry(
            id=feedback.id,
            target_type=feedback.target_type,
            target_id=feedback.target_id,
            body=feedback.body,
            severity=feedback.severity,
            status=feedback.status,
            created_at=feedback.created_at,
        )
