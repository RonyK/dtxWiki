"""Compose ordinary Context edits inside the lint run's transaction."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.context_media.domain import ContextMediaError
from backend.domain.input_policy import InputPolicyError
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.repositories.workspace_scoped import (
    get_in_workspace,
    join_in_workspace,
    select_in_workspace,
)
from backend.services.audit_service import AuditService
from backend.services.body_preparer import BodyInput, BodyPreparer, WriteContext
from backend.services.context_version_copy import inspect_context_version_references
from backend.services.db_write_intent import acquire_write_intent
from backend.services.lint import LintPolicy, LintRegistry
from backend.services.mcp_write_idempotency import (
    execute_idempotent_write,
    request_fingerprint,
)
from backend.services.read_errors import ReadServiceError

if TYPE_CHECKING:
    from collections.abc import Callable

    from backend.context_media.ports import ContextMediaRepository
    from backend.services.job_queue import JobQueueService


class LintFixResult(BaseModel):
    state: Literal["skipped", "review", "fixed"]
    version_id: UUID | None = None
    module_fix_counts: dict[str, int] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class _NoLintChanges(Exception):
    """Roll back a no-op claim without leaving a durable skip receipt."""


class _SessionBoundFactory:
    """The writer owns no commit, rollback or close of the caller's session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @contextmanager
    def begin(self):
        if not self.session.in_transaction():
            raise RuntimeError("LintFixWriter requires an active caller transaction")
        yield self.session

    def __call__(self):
        return self.begin()


class LintFixWriter:
    def __init__(
        self,
        *,
        lint_registry: LintRegistry,
        audit_service: AuditService | None = None,
        job_queue: JobQueueService | None = None,
        context_media_repository_factory: Callable[[Session], ContextMediaRepository]
        | None = None,
        self_origins: frozenset[str] | None = None,
    ) -> None:
        self.registry = lint_registry
        self.audit = audit_service or AuditService()
        self.job_queue = job_queue
        self.context_media_repository_factory = context_media_repository_factory
        self.self_origins = self_origins

    def fix_page(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        run_id: UUID,
        page_id: UUID,
        base_version_id: UUID,
        base_content_hash: str,
    ) -> LintFixResult:
        """Apply one repair in the caller's transaction and retain its locks.

        Policy locks survive this method until the caller commits or rolls
        back, so a queued run holds them across its remaining page repairs.
        An integrity failure propagates to the transaction owner: queued-run
        callers must abort the whole run, including earlier repairs/findings,
        rather than continue per page or turn the failure into a review.
        """
        if not session.in_transaction():
            raise RuntimeError("LintFixWriter requires an active caller transaction")
        from backend.services.change_review_audit import ChangeReviewAuditRecorder
        from backend.services.page_service import PageService, PageServiceError

        acquire_write_intent(session)
        preparer = BodyPreparer(self.registry, self_origins=self.self_origins)
        from backend.spi.types import ActorContext

        service = PageService(
            _SessionBoundFactory(session),
            actor_context=ActorContext(
                actor_kind="system", actor_id="lint-autofix", workspace_id=workspace_id
            ),
            change_audit_recorder=ChangeReviewAuditRecorder(
                audit_service=self.audit, lint_registry=self.registry
            ),
            job_queue=self.job_queue,
            body_preparer=preparer,
            lint_registry=self.registry,
            context_media_repository_factory=self.context_media_repository_factory,
        )
        actor = {
            "actor_kind": "system",
            "actor_id": "lint-autofix",
            "actor_provenance": "internal",
        }
        key = f"lint-fix:{run_id}:{page_id}:{base_version_id}"

        def execute() -> LintFixResult:
            # Keep media intent before page locks. Share-lock the policy row
            # in between so a settings update cannot invalidate this proof
            # before publication. The lock remains until the caller's whole
            # transaction ends; concurrent maintenance SHARE locks coexist,
            # while settings UPDATE locks must wait for the lint run to end.
            service._lock_context_media_usage_intent(session, workspace_id)
            workspace = session.scalar(
                select(Workspace)
                .where(Workspace.id == workspace_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            page = session.scalar(
                select_in_workspace(
                    ContextPage,
                    workspace_id=workspace_id,
                    conditions=(ContextPage.id == page_id,),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if page is None or workspace is None or page.lifecycle_status == "archived":
                return LintFixResult(state="skipped")
            if (
                page.current_published_version_id != base_version_id
                or page.current_version_id not in {None, base_version_id}
                or page.latest_draft_version_id not in {None, base_version_id}
            ):
                return LintFixResult(state="skipped")
            editing = session.scalar(
                select_in_workspace(
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    conditions=(
                        ContextPageVersion.page_id == page_id,
                        ContextPageVersion.version_status == "editing",
                    ),
                )
                .with_only_columns(ContextPageVersion.id)
                .limit(1)
            )
            pending = session.scalar(
                join_in_workspace(
                    select_in_workspace(
                        ReviewItem,
                        workspace_id=workspace_id,
                        conditions=(
                            ReviewItem.status.in_(("pending", "changes_requested")),
                            ReviewItem.review_type.in_(
                                ("context_update", "context_reconciliation")
                            ),
                            ChangeOperation.target_type == "context_page",
                            ChangeOperation.target_id == page_id,
                        ),
                    ),
                    ReviewItem,
                    ChangeOperation,
                    workspace_id=workspace_id,
                    on=ChangeOperation.change_set_id == ReviewItem.change_set_id,
                )
                .with_only_columns(ReviewItem.id)
                .limit(1)
            )
            if editing is not None or pending is not None:
                return LintFixResult(state="skipped")
            base = session.scalar(
                select_in_workspace(
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    conditions=(
                        ContextPageVersion.page_id == page_id,
                        ContextPageVersion.id == base_version_id,
                        ContextPageVersion.version_status == "published_snapshot",
                    ),
                )
            )
            if base is None or base.content_hash != base_content_hash:
                return LintFixResult(state="skipped")
            policy = LintPolicy(workspace)
            fix_existing = {
                module.module_id
                for module in self.registry.modules
                if module.fixer is not None and policy.fix_existing(module)
            }
            if not fix_existing:
                raise _NoLintChanges
            context = WriteContext(
                workspace=workspace,
                session=session,
                page=page,
                version=base,
                source_version=base,
                link_source="published",
                **actor,
            )
            prepared = preparer.prepare_maintenance(BodyInput(base.body_md), context)
            counts = dict(prepared.module_fix_counts)
            if prepared.body == base.body_md or not fix_existing.intersection(counts):
                raise _NoLintChanges
            opened = service.begin_context_edit(
                page_id=page_id,
                workspace_id=workspace_id,
                **actor,
                idempotency_key=key + ":begin",
                base_version_id=base_version_id,
                base_content_hash=base_content_hash,
            )
            saved = service._save_prepared_context_edit_in_session(
                session=session,
                page_id=page_id,
                workspace_id=workspace_id,
                actor_kind=actor["actor_kind"],
                actor_id=actor["actor_id"],
                prepared=prepared,
                base_content_hash=opened.content_hash,
                expected_editing_revision=opened.editing_revision,
                editing_version_id=opened.version_id,
            )
            saved_version = get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=saved.version_id,
            )
            if (
                saved_version is None
                or saved_version.body_md != prepared.body
                or saved_version.content_hash != prepared.content_hash
                or saved.body_md != prepared.body
                or saved.content_hash != prepared.content_hash
            ):
                # This is a broken persistence invariant, not uncertain
                # approval evidence. Roll back this page's savepoint and let
                # the queued-run owner roll back every page/finding in its
                # transaction; do not continue through a per-page review.
                raise RuntimeError("Lint maintenance did not persist the prepared body")
            references = inspect_context_version_references(
                session,
                version=saved_version,
                body_unchanged=base.body_md == saved_version.body_md,
            )
            from backend.services.document_references import document_references_hash

            proof = (
                references.anchors_preserved
                and document_references_hash(session, base)
                == document_references_hash(session, saved_version)
                and preparer.auto_approval_allowed(prepared, context)
            )
            reason = "lint.autofix: " + json.dumps(counts, sort_keys=True)
            submitted = service._submit_context_edit_in_session(
                session=session,
                page_id=page_id,
                version_id=saved.version_id,
                workspace_id=workspace_id,
                **actor,
                expected_editing_revision=saved.editing_revision,
                idempotency_key=key + ":submit",
                reason=reason,
                needs_review=not proof,
                risk_signals={"decision_page_type": page.page_type == "decision"},
            )
            review = get_in_workspace(
                session,
                ReviewItem,
                workspace_id=workspace_id,
                resource_id=submitted.review_item_id,
            )
            if submitted.change_set_id is not None:
                get_in_workspace(
                    session,
                    ChangeSet,
                    workspace_id=workspace_id,
                    resource_id=submitted.change_set_id,
                ).reason = reason
            state = "review"
            if proof and review is not None and review.status == "approved":
                published = service.publish_draft(
                    page_id=page_id,
                    version_id=submitted.version_id,
                    expected_content_hash=submitted.content_hash,
                    reason=reason,
                    publish_despite_findings=False,
                    **actor,
                )
                state = "fixed"
                version_id = published.page.version_id
            else:
                version_id = submitted.version_id
            self.audit.emit(
                session=session,
                workspace_id=workspace_id,
                actor_kind="system",
                actor_id=actor["actor_id"],
                action="page.lint_autofix",
                target_kind="context_page",
                target_id=page_id,
                payload={
                    "source": "lint.autofix",
                    "lint_run_id": str(run_id),
                    "base_version_id": str(base_version_id),
                    "version_id": str(version_id),
                    "module_fix_counts": counts,
                    "state": state,
                },
            )
            return LintFixResult(
                state=state, version_id=version_id, module_fix_counts=counts
            )

        try:
            return execute_idempotent_write(
                session,
                workspace_id=workspace_id,
                actor_kind="system",
                actor_id=actor["actor_id"],
                tool_name="lint_fix_page",
                idempotency_key=key,
                fingerprint=request_fingerprint(
                    {
                        "page_id": str(page_id),
                        "base_version_id": str(base_version_id),
                        "base_content_hash": base_content_hash,
                    }
                ),
                result_model=LintFixResult,
                execute=execute,
            )
        except _NoLintChanges:
            # The idempotency savepoint removed its claim; there was no write
            # to replay. Conflicts retain the ordinary skipped-result policy.
            return LintFixResult(state="skipped")
        except (
            PageServiceError,
            ReadServiceError,
            InputPolicyError,
            ContextMediaError,
        ):
            # The ledger savepoint rolled back every partial edit before this
            # expected conflict is converted into a skip. Findings stay open.
            # Integrity RuntimeError deliberately escapes this handler, so
            # the queued-run owner aborts the full caller transaction.
            return LintFixResult(state="skipped")
