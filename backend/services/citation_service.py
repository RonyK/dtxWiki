"""Explicit bibliography commands shared by MCP and the web editor."""

from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.context import ContextPageVersion
from backend.policy.mcp_scopes import has_required_scope
from backend.repositories.context_page_repo import get_page_by_id_for_update
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.context_write import CiteRequest, CiteResult
from backend.services.audit_service import AuditService
from backend.services.db_write_intent import acquire_write_intent
from backend.services.document_references import (
    apply_reference_delta,
    document_references_hash,
)
from backend.services.mcp_write_idempotency import (
    execute_idempotent_write,
    request_fingerprint,
)
from backend.services.page_service import PageConflictError, PageService
from backend.services.read_errors import (
    ReadNotFoundError,
    ReadValidationError,
    ScopeDeniedError,
)
from backend.spi.types import ActorContext


class CitationService:
    """Register chosen document IDs without finding or scoring other documents."""

    def __init__(
        self,
        audit_service: AuditService | None = None,
        *,
        self_origins: frozenset[str] | None = None,
    ) -> None:
        self.audit_service = audit_service or AuditService()
        self.self_origins = self_origins

    def cite(
        self,
        session: Session,
        workspace_id: UUID,
        request: CiteRequest,
        *,
        actor: ActorContext,
    ) -> CiteResult:
        if actor.workspace_id is not None and actor.workspace_id != workspace_id:
            raise ReadNotFoundError("context page not found")
        if actor.provenance != "browser_session" and not has_required_scope(
            actor, ("draft-write",)
        ):
            raise ScopeDeniedError("draft-write scope required")

        def execute() -> CiteResult:
            acquire_write_intent(session)
            page = get_page_by_id_for_update(session, request.page_id)
            if page is None or page.workspace_id != workspace_id:
                raise ReadNotFoundError("context page not found")
            session.refresh(page)
            if page.lifecycle_status == "archived":
                raise ReadNotFoundError("context page not found")
            version = get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=request.version_id,
            )
            if (
                version is None
                or version.page_id != page.id
                or version.created_by_actor_kind != actor.actor_kind
                or version.created_by_actor_id != actor.actor_id
            ):
                raise ReadNotFoundError("context page version not found")
            # Refresh under the page lock: a long-lived identity map is not a
            # concurrency check, particularly for metadata-only changes.
            session.refresh(version)
            # Browser saves share drafts with Editors. A reviewed version is
            # immutable evidence: autosave must fork it before this command.
            browser_draft = PageService._is_unreviewed_browser_draft(
                session=session,
                page=page,
                version=version,
                actor_provenance=actor.provenance,
            )
            if (
                (version.version_status != "editing" and not browser_draft)
                or version.graph_status != "linear"
                or version.editing_revision != request.expected_editing_revision
            ):
                raise PageConflictError(
                    server_version_id=version.id,
                    server_content_hash=version.content_hash,
                    server_editing_revision=(
                        version.editing_revision
                        if version.version_status in {"editing", "draft"}
                        else None
                    ),
                )
            try:
                delta = apply_reference_delta(
                    session,
                    version=version,
                    add=request.add,
                    remove=request.remove,
                    actor=actor,
                    self_origins=self.self_origins,
                )
            except ValueError as exc:
                raise ReadValidationError(str(exc)) from exc
            changed = bool(delta["changed"])
            receipt_id = uuid4()
            audit_event_id = None
            if changed:
                version.editing_revision += 1
                page.updated_at = utc_now()
                event = self.audit_service.emit(
                    session=session,
                    workspace_id=workspace_id,
                    actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id,
                    action="context_page.references.update",
                    target_kind="context_page",
                    target_id=page.id,
                    payload={
                        "version_id": str(version.id),
                        "editing_revision": version.editing_revision,
                        "receipt_id": str(receipt_id),
                        "added_count": len(delta["added"]),
                        "removed_count": len(delta["removed"]),
                        "actor_provenance": actor.provenance,
                    },
                )
                audit_event_id = event.id
            session.flush()
            return CiteResult(
                page_id=page.id,
                version_id=version.id,
                editing_revision=version.editing_revision,
                references_hash=document_references_hash(session, version),
                publication_state=version.version_status,
                receipt_id=receipt_id,
                audit_event_id=audit_event_id,
                # The receipt names changed registrations. Notes are authored
                # content and are read separately with the reference projection;
                # echoing 500 full notes would overwhelm a small MCP command.
                added=[
                    {key: value for key, value in ref.items() if key != "note"}
                    for ref in delta["added"]
                ],
                removed=delta["removed"],
                unchanged=[
                    {key: value for key, value in ref.items() if key != "note"}
                    for ref in delta["unchanged"]
                ],
                remaining_inline_occurrences=delta["remaining_inline_occurrences"],
            )

        return execute_idempotent_write(
            session,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            tool_name="cite",
            idempotency_key=request.idempotency_key,
            fingerprint=request_fingerprint(
                request.model_dump(
                    mode="json", exclude={"idempotency_key", "retry_only"}
                )
            ),
            result_model=CiteResult,
            execute=execute,
            replay_transform=lambda result: result.model_copy(
                update={"replayed": True}
            ),
            retry_only=request.retry_only,
        )
