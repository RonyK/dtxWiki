import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.library_media.ports import LibraryLogicalQuotaFactory
from backend.media.domain import BlobEpoch, BlobReference
from backend.models.base import utc_now
from backend.models.bundle import AgentInputBundleItem
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.models.job import Job
from backend.models.library import (
    LibraryArtifact,
    LibraryAttempt,
    LibraryItem,
    LibraryItemTombstone,
    LibraryItemVersion,
    LibraryRun,
)
from backend.models.library_discovery import (
    LibraryDiscoveryCandidate,
    LibraryDiscoveryRun,
)
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.repositories.media_blob_reference import (
    BlobReferenceOwnerKind,
    SqlAlchemyBlobReferenceLedger,
    lock_workspace_blob_reference_intent,
)
from backend.services.actor_visible_read import actor_can_read_library_trust_level
from backend.services.audit_service import AuditService
from backend.services.change_review_audit import ChangeReviewAuditRecorder
from backend.services.db_write_intent import acquire_write_intent
from backend.services.job_queue import JobQueueService
from backend.services.library_storage import inline_text_ref
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
from backend.services.read_errors import ReadNotFoundError
from backend.services.settings_service import normalized_library_policy
from backend.services.tree_service import ensure_primary_tree_placement
from backend.utils.content_hash import content_hash as calculate_content_hash


def _str_or_none(value) -> str | None:
    return str(value) if value is not None else None


def library_item_for_version_insert_statement(library_item_id: UUID):
    return (
        select(LibraryItem)
        .where(LibraryItem.id == library_item_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )


@dataclass(frozen=True)
class LibraryCandidateInput:
    canonical_uri: str
    title: str | None = None
    connector_type: str = "web"
    format_hint: str = "unknown"
    trust_level: str = "unknown"
    confidence: str = "unknown"
    raw_text: str | None = None
    raw_storage_ref: str | None = None


@dataclass(frozen=True)
class LibraryRegistrationResult:
    library_item: LibraryItem
    version: LibraryItemVersion
    extraction_job: Job
    created_library_item: bool
    created_version: bool


class LibraryService:
    def __init__(
        self,
        job_queue: JobQueueService | None = None,
        audit_service: AuditService | None = None,
        change_review_audit: ChangeReviewAuditRecorder | None = None,
        logical_quota_factory: LibraryLogicalQuotaFactory | None = None,
    ) -> None:
        self.job_queue = job_queue or JobQueueService()
        self.audit_service = audit_service or AuditService()
        self.change_review_audit = change_review_audit or ChangeReviewAuditRecorder(
            audit_service=self.audit_service
        )
        self.logical_quota_factory = logical_quota_factory

    def list_item_references(
        self,
        session: Session,
        workspace_id: UUID,
        library_item_id: UUID,
        actor: ActorContext | None = None,
    ) -> list[dict[str, str]]:
        """Context pages that cite this Library item (via any of its versions or
        artifacts). Drives the delete dialog: deleting a referenced source leaves
        a render-time tombstone on each of these pages and notifies reviewers."""
        item = self._require_item(session, workspace_id, library_item_id, actor)
        # A read of an archived/hidden item's references must 404 like every
        # other read lane, so its id is not a references oracle (Codex R12).
        # Deletion itself still works: delete_library_item uses _require_item,
        # which stays lifecycle-agnostic so an archived item can be purged.
        if item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
            raise ReadNotFoundError("library item not found")
        citations = self._citations_for_item(session, item)
        pages = self._citing_pages(session, citations)
        return [
            {"id": str(page.id), "title": page.title, "slug": page.slug}
            for page in pages
        ]

    def list_tombstones(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        actor: ActorContext | None = None,
    ) -> list[dict[str, object | None]]:
        """All deleted-source tombstones for the workspace. The reader recomputes
        each source's ``@cite{...}`` key from ``uri``/``title`` to swap dead
        citation tokens for a "(deleted)" marker."""
        rows = session.scalars(
            select(LibraryItemTombstone)
            .where(LibraryItemTombstone.workspace_id == workspace_id)
            .order_by(LibraryItemTombstone.deleted_at.desc())
        ).all()
        return [
            {
                "library_item_id": str(row.library_item_id),
                "uri": row.uri,
                "title": row.title,
                "last_version_no": row.last_version_no,
                "deleted_at": (row.deleted_at.isoformat() if row.deleted_at else None),
            }
            for row in rows
            if actor_can_read_library_trust_level(
                row.trust_level_snapshot,
                actor,
            )
        ]

    def delete_library_item(
        self,
        session: Session,
        workspace_id: UUID,
        library_item_id: UUID,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> None:
        """Hard-delete a Library source (item -> versions -> artifacts) and
        archive its tree placement. Referenced sources are no longer refused: a
        render-time tombstone is recorded so the inline ``@cite{...}`` tokens
        left behind in page bodies resolve to a "(deleted)" marker, the
        structured citation rows are removed (clearing the ondelete=RESTRICT
        FKs), and a pending review notice is raised on every citing page."""
        acquire_write_intent(session)
        lock_workspace_blob_reference_intent(
            session,
            workspace_id,
            allow_deleting=False,
        )
        item = self._require_item(session, workspace_id, library_item_id, actor)
        from backend.services.indexing.fts5 import sync_library_doc_sources

        item.lifecycle_status = "archived"
        sync_library_doc_sources(session, item)
        title = item.title or item.uri
        uri = item.uri
        last_version_no = max(
            (version.version_no for version in item.versions),
            default=None,
        )

        citations = self._citations_for_item(session, item)
        citing_pages = self._citing_pages(session, citations) if citations else []
        citing_page_refs = [(page.id, page.title) for page in citing_pages]
        # Codex #14: capture per-citation provenance BEFORE the rows are deleted
        # so review/stale/audit/export lineage survives the hard delete.
        citation_provenance = self._serialize_citation_provenance(session, citations)

        self._record_tombstone(
            session=session,
            workspace_id=workspace_id,
            library_item_id=library_item_id,
            uri=uri,
            title=item.title,
            trust_level_snapshot=item.trust_level,
            last_version_no=last_version_no,
            citations=citation_provenance,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )

        # Drop the structured citation rows first so the ondelete=RESTRICT FKs
        # from context_citation -> library_item_version / library_artifact don't
        # block the ordered deletes below.
        for citation in citations:
            session.delete(citation)
        session.flush()

        node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.target_kind == "library_item",
                WorkspaceTreeNode.target_id == library_item_id,
            )
        )
        if node is not None:
            node.visibility_status = "archived"
        versions = list(item.versions)
        version_ids = {version.id for version in versions}
        preview_runs = session.scalars(
            select(LibraryRun).where(
                LibraryRun.library_item_version_id.in_(version_ids),
                LibraryRun.stage == "preview_generation",
            )
        ).all()
        preview_attempt_ids = set(
            session.scalars(
                select(LibraryAttempt.id).where(
                    LibraryAttempt.library_item_version_id.in_(version_ids),
                    LibraryAttempt.stage == "preview_generation",
                )
            )
        )
        preview_artifacts = list(
            session.scalars(
                select(LibraryArtifact).where(
                    LibraryArtifact.library_item_version_id.in_(version_ids),
                    LibraryArtifact.stage == "preview_generation",
                )
            )
        )
        preview_artifact_ids = {artifact.id for artifact in preview_artifacts}

        # A Library hard-delete owns the complete association teardown.  Do
        # not leave physical ownership to ORM cascades: the shared ledger is
        # the only authoritative cross-domain zero-reference predicate, so
        # every source, conversion, and Preview association is removed in
        # this same transaction before its domain row disappears.
        blob_references = SqlAlchemyBlobReferenceLedger(session)
        owned_blobs: list[tuple[BlobReferenceOwnerKind, UUID, BlobReference]] = []
        for preview_artifact in preview_artifacts:
            if (
                preview_artifact.blob_workspace_id is not None
                and preview_artifact.blob_id is not None
                and preview_artifact.blob_epoch is not None
            ):
                owned_blobs.append(
                    (
                        BlobReferenceOwnerKind.LIBRARY_ARTIFACT,
                        preview_artifact.id,
                        BlobReference(
                            preview_artifact.blob_workspace_id,
                            preview_artifact.blob_id,
                            BlobEpoch(preview_artifact.blob_epoch),
                        ),
                    )
                )
        for version in versions:
            for artifact in version.artifacts:
                # version.artifacts is now the unified (conversion +
                # preview_generation) collection; preview-stage artifacts were
                # already collected above as `preview_artifacts` (registered
                # under the same BlobReferenceOwnerKind.LIBRARY_ARTIFACT, since
                # both stages now live in the one table), so only
                # conversion-stage ones belong to this loop.
                if artifact.stage != "conversion":
                    continue
                if (
                    artifact.blob_workspace_id is not None
                    and artifact.blob_id is not None
                    and artifact.blob_epoch is not None
                ):
                    owned_blobs.append(
                        (
                            BlobReferenceOwnerKind.LIBRARY_ARTIFACT,
                            artifact.id,
                            BlobReference(
                                artifact.blob_workspace_id,
                                artifact.blob_id,
                                BlobEpoch(artifact.blob_epoch),
                            ),
                        )
                    )
            if (
                version.source_blob_workspace_id is not None
                and version.source_blob_id is not None
                and version.source_blob_epoch is not None
            ):
                owned_blobs.append(
                    (
                        BlobReferenceOwnerKind.LIBRARY_ITEM_VERSION,
                        version.id,
                        BlobReference(
                            version.source_blob_workspace_id,
                            version.source_blob_id,
                            BlobEpoch(version.source_blob_epoch),
                        ),
                    )
                )
        for owner_kind, owner_id, blob in sorted(
            owned_blobs,
            key=lambda entry: (
                entry[2].workspace_id.hex,
                entry[2].blob_id.hex,
                entry[2].epoch.value,
                entry[0].value,
                entry[1].hex,
            ),
        ):
            if blob.workspace_id != workspace_id:
                raise RuntimeError("library_blob_reference_workspace_mismatch")
            blob_references.unregister(
                owner_kind=owner_kind,
                owner_id=owner_id,
                blob=blob,
            )
        if self.logical_quota_factory is not None:
            logical_quota = self.logical_quota_factory(session)
            logical_quota.release(
                workspace_id,
                quota_kind="derived",
                association_ids={
                    artifact.id
                    for version in versions
                    for artifact in version.artifacts
                    if artifact.blob_id is not None
                }
                | preview_artifact_ids,
            )
            logical_quota.release(
                workspace_id,
                quota_kind="source",
                association_ids={
                    version.id
                    for version in versions
                    if version.source_blob_id is not None
                },
            )
        conversion_run_ids = {
            run.id
            for version in versions
            for run in version.runs
            if run.stage == "conversion"
        }
        conversion_attempt_ids = {
            attempt.id
            for version in versions
            for attempt in version.attempts
            if attempt.stage == "conversion"
        }
        removed_jobs = self._remove_library_lineage_jobs(
            session=session,
            workspace_id=workspace_id,
            library_item_id=item.id,
            version_ids=version_ids,
            conversion_run_ids=conversion_run_ids,
            conversion_attempt_ids=conversion_attempt_ids,
            preview_run_ids={run.id for run in preview_runs},
            preview_attempt_ids=preview_attempt_ids,
        )
        # Break the selected/current and self-parent cycles before deleting the
        # owned lineage. The remaining child rows are removed by the ORM
        # ownership cascades, which mirror the database ON DELETE CASCADE FKs.
        item.current_version_id = None
        item.current_ready_version_id = None
        item.current_preview_version_id = None
        for version in versions:
            version.previous_version_id = None
            version.selected_conversion_attempt_id = None
            version.selected_preview_attempt_id = None
            version.selected_semantic_overlay_id = None
        session.flush()
        # LibraryAttempt.artifacts has no ORM cascade, and every artifact (on
        # either stage) holds a NOT NULL FK to its attempt, so artifacts must
        # be deleted before their owning attempts/runs -- not after -- or
        # SQLAlchemy tries to null out their FK columns when the attempt/
        # version is deleted instead, which the NOT NULL columns reject.
        removed_bundle_refs = self._remove_bundle_artifact_references(
            session=session,
            item=item,
        )
        for version in versions:
            for artifact in list(version.artifacts):
                session.delete(artifact)
        session.flush()
        # Preview attempts may RESTRICT-delete their selected conversion input.
        # Remove the Preview lineage before conversion artifacts/runs, after the
        # selected pointer cycles above have been broken.
        for preview_run in preview_runs:
            session.delete(preview_run)
        session.flush()
        # Session.delete() leaves flushed children in loaded collections.
        # Reload runs before the version cascade, so already deleted Preview
        # runs and their attempts/details are not scheduled for deletion twice.
        for version in versions:
            session.expire(version, ["runs"])
        for version in versions:
            session.delete(version)
        session.delete(item)
        session.flush()

        # Raise an FYI review notice on each citing page now the source is gone.
        for page_id, _page_title in citing_page_refs:
            self.change_review_audit.record_library_item_deleted(
                session=session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                page_id=page_id,
                library_item_id=library_item_id,
                item_title=title,
                source_uri=uri,
                note=f'Cited source "{title}" was deleted.',
                actor_provenance=(
                    actor.provenance if actor is not None else "internal"
                ),
            )

        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="library.item.delete",
            target_kind="library_item",
            target_id=library_item_id,
            payload={
                "title": title,
                "uri": uri,
                "citing_page_count": len(citing_page_refs),
                "removed_bundle_reference_count": removed_bundle_refs,
                "removed_job_count": removed_jobs,
            },
        )
        session.flush()

    @staticmethod
    def _serialize_citation_provenance(
        session: Session, citations: list[ContextCitation]
    ) -> list[dict]:
        """Capture each citation's durable provenance before the rows are deleted.

        Preserves what review/stale/audit/export need: snippet, location, type,
        which version-or-artifact was cited (+ its version_no), and the citing
        page/claim — none of which the item-level tombstone otherwise keeps.
        """
        serialized: list[dict] = []
        for citation in citations:
            version_id = citation.library_item_version_id
            if version_id is None and citation.library_artifact_id is not None:
                artifact = session.get(LibraryArtifact, citation.library_artifact_id)
                version_id = artifact.library_item_version_id if artifact else None
            version_no = None
            if version_id is not None:
                version = session.get(LibraryItemVersion, version_id)
                version_no = version.version_no if version else None
            serialized.append(
                {
                    "citation_id": str(citation.id),
                    "citation_type": citation.citation_type,
                    "target_kind": citation.target_kind,
                    "target_id": str(citation.target_id),
                    "library_item_version_id": _str_or_none(
                        citation.library_item_version_id
                    ),
                    "library_artifact_id": _str_or_none(citation.library_artifact_id),
                    "cited_context_page_version_id": _str_or_none(
                        citation.cited_context_page_version_id
                    ),
                    "version_no": version_no,
                    "snippet": citation.snippet,
                    "location": citation.location,
                    "target_location": citation.target_location,
                    "verification_status": citation.verification_status,
                }
            )
        return serialized

    @staticmethod
    def _record_tombstone(
        session: Session,
        workspace_id: UUID,
        library_item_id: UUID,
        uri: str,
        title: str | None,
        trust_level_snapshot: str,
        last_version_no: int | None,
        actor_kind: str,
        actor_id: str,
        citations: list[dict] | None = None,
    ) -> None:
        # Upsert on (workspace, item) so a re-run (or a re-registered-then-
        # deleted source reusing the id) refreshes rather than violating the
        # unique constraint.
        existing = session.scalar(
            select(LibraryItemTombstone).where(
                LibraryItemTombstone.workspace_id == workspace_id,
                LibraryItemTombstone.library_item_id == library_item_id,
            )
        )
        if existing is not None:
            existing.uri = uri
            existing.title = title
            existing.trust_level_snapshot = trust_level_snapshot
            existing.last_version_no = last_version_no
            existing.citations = citations
            existing.deleted_at = utc_now()
            existing.deleted_by_actor_type = actor_kind
            existing.deleted_by_actor_id = actor_id
            return
        session.add(
            LibraryItemTombstone(
                workspace_id=workspace_id,
                library_item_id=library_item_id,
                uri=uri,
                title=title,
                trust_level_snapshot=trust_level_snapshot,
                last_version_no=last_version_no,
                citations=citations,
                deleted_at=utc_now(),
                deleted_by_actor_type=actor_kind,
                deleted_by_actor_id=actor_id,
            )
        )

    @staticmethod
    def _remove_bundle_artifact_references(
        session: Session,
        item: LibraryItem,
    ) -> int:
        """Remove ephemeral bundle rows that pin artifacts being hard-deleted.

        Agent input bundles keep ``library_artifact_id`` as a RESTRICT FK. Once
        the source itself is hard-deleted there is no useful artifact payload
        left for a future agent packet, so remove those bundle rows before
        deleting artifacts. Context-page citations get a user-facing tombstone;
        bundle rows are internal generated input snapshots.
        """
        artifact_ids = [
            artifact.id for version in item.versions for artifact in version.artifacts
        ]
        if not artifact_ids:
            return 0
        bundle_items = session.scalars(
            select(AgentInputBundleItem).where(
                AgentInputBundleItem.library_artifact_id.in_(artifact_ids)
            )
        ).all()
        for bundle_item in bundle_items:
            session.delete(bundle_item)
        return len(bundle_items)

    @staticmethod
    def _remove_library_lineage_jobs(
        *,
        session: Session,
        workspace_id: UUID,
        library_item_id: UUID,
        version_ids: set[UUID],
        conversion_run_ids: set[UUID],
        conversion_attempt_ids: set[UUID],
        preview_run_ids: set[UUID],
        preview_attempt_ids: set[UUID],
    ) -> int:
        """Delete queue rows whose JSON target points into a deleted lineage."""

        item_ref = str(library_item_id)
        version_refs = {str(value) for value in version_ids}
        run_refs = {str(value) for value in conversion_run_ids}
        attempt_refs = {str(value) for value in conversion_attempt_ids}
        preview_run_refs = {str(value) for value in preview_run_ids}
        preview_attempt_refs = {str(value) for value in preview_attempt_ids}
        removed = 0
        jobs = session.scalars(
            select(Job).where(Job.workspace_id == workspace_id)
        ).all()
        for job in jobs:
            target = job.target_ref
            if not isinstance(target, dict):
                continue
            points_to_item = target.get("library_item_id") == item_ref or (
                target.get("kind") == "library_item" and target.get("id") == item_ref
            )
            version_ref = target.get("library_item_version_id")
            run_ref = target.get("library_conversion_run_id")
            attempt_ref = target.get("library_conversion_attempt_id")
            canonical_attempt_ref = target.get("conversion_attempt_id")
            preview_run_ref = target.get("library_preview_run_id")
            preview_attempt_ref = target.get("library_preview_attempt_id")
            points_to_version = (
                isinstance(version_ref, str) and version_ref in version_refs
            )
            points_to_run = isinstance(run_ref, str) and run_ref in run_refs
            points_to_attempt = (
                isinstance(attempt_ref, str)
                and attempt_ref in attempt_refs
                or isinstance(canonical_attempt_ref, str)
                and canonical_attempt_ref in attempt_refs
            )
            points_to_preview_run = (
                isinstance(preview_run_ref, str)
                and preview_run_ref in preview_run_refs
            )
            points_to_preview_attempt = (
                isinstance(preview_attempt_ref, str)
                and preview_attempt_ref in preview_attempt_refs
            )
            if not (
                points_to_item
                or points_to_version
                or points_to_run
                or points_to_attempt
                or points_to_preview_run
                or points_to_preview_attempt
            ):
                continue
            session.delete(job)
            removed += 1
        return removed

    @staticmethod
    def _citing_pages(
        session: Session,
        citations: list[ContextCitation],
    ) -> list[ContextPage]:
        """Distinct Context pages that own ``citations``.

        A citation's ``target_id`` is a ContextClaim id (when ``target_kind`` is
        "context_claim") or a ContextPageVersion id otherwise — never a page id
        directly — so resolve claim -> version -> page."""
        if not citations:
            return []
        version_ids: set[UUID] = set()
        claim_ids: set[UUID] = set()
        for citation in citations:
            if citation.target_kind == "context_claim":
                claim_ids.add(citation.target_id)
            else:
                version_ids.add(citation.target_id)
        if claim_ids:
            for page_version_id in session.scalars(
                select(ContextClaim.page_version_id).where(
                    ContextClaim.id.in_(claim_ids)
                )
            ):
                version_ids.add(page_version_id)
        if not version_ids:
            return []
        page_ids = set(
            session.scalars(
                select(ContextPageVersion.page_id).where(
                    ContextPageVersion.id.in_(version_ids)
                )
            )
        )
        if not page_ids:
            return []
        return list(
            session.scalars(
                select(ContextPage).where(ContextPage.id.in_(page_ids))
            ).all()
        )

    @staticmethod
    def _require_item(
        session: Session,
        workspace_id: UUID,
        library_item_id: UUID,
        actor: ActorContext | None = None,
    ) -> LibraryItem:
        item = session.get(LibraryItem, library_item_id)
        if item is None or item.workspace_id != workspace_id:
            raise ReadNotFoundError("library item not found")
        # H-03: a direct-ID reference lookup or mutation must enforce the SAME
        # trust gate as GET — a restricted item the actor cannot read stays a
        # uniform 404, never a references oracle or an editable target.
        if actor is not None and not actor_can_read_library_trust_level(
            item.trust_level, actor
        ):
            raise ReadNotFoundError("library item not found")
        return item

    @staticmethod
    def _citations_for_item(
        session: Session,
        item: LibraryItem,
    ) -> list[ContextCitation]:
        version_ids = [version.id for version in item.versions]
        artifact_ids = [
            artifact.id for version in item.versions for artifact in version.artifacts
        ]
        conditions = []
        if version_ids:
            conditions.append(ContextCitation.library_item_version_id.in_(version_ids))
        if artifact_ids:
            conditions.append(ContextCitation.library_artifact_id.in_(artifact_ids))
        if not conditions:
            return []
        return list(
            session.scalars(select(ContextCitation).where(or_(*conditions))).all()
        )

    def run_discovery(
        self,
        session: Session,
        workspace: Workspace,
        run_token: str,
        candidates: list[LibraryCandidateInput],
        job_id: UUID | None = None,
        actor_type: str = "system",
        actor_id: UUID | None = None,
    ) -> LibraryDiscoveryRun:
        policy_snapshot = normalized_library_policy(workspace.library_policy)
        policy_hash = self.policy_snapshot_hash(policy_snapshot)
        existing = self._get_discovery_run(
            session=session,
            workspace_id=workspace.id,
            policy_snapshot_hash=policy_hash,
            run_token=run_token,
        )
        if existing is not None:
            return existing

        started_at = utc_now()
        run = LibraryDiscoveryRun(
            workspace_id=workspace.id,
            job_id=job_id,
            policy_snapshot_hash=policy_hash,
            run_token=run_token,
            status="running",
            candidate_count=len(candidates),
            created_by_actor_type=actor_type,
            created_by_actor_id=actor_id,
            started_at=started_at,
        )
        session.add(run)
        session.flush()

        manual_approval_required = policy_snapshot["manual_approval_required"] is True
        accepted_count = 0
        excluded_count = 0

        for candidate_input in candidates:
            self._validate_candidate(candidate_input)
            existing_library_item = self.get_library_item_by_uri(
                session=session,
                workspace_id=workspace.id,
                uri=candidate_input.canonical_uri,
            )
            if existing_library_item is not None:
                candidate = self._build_candidate(
                    workspace_id=workspace.id,
                    discovery_run_id=run.id,
                    candidate_input=candidate_input,
                    decision_status="excluded",
                    decision_reason="already_registered",
                )
                excluded_count += 1
            elif manual_approval_required:
                candidate = self._build_candidate(
                    workspace_id=workspace.id,
                    discovery_run_id=run.id,
                    candidate_input=candidate_input,
                    decision_status="pending_review",
                    decision_reason="manual_approval_required",
                )
            else:
                registration = self.register_library_item(
                    session=session,
                    workspace_id=workspace.id,
                    canonical_uri=candidate_input.canonical_uri,
                    title=candidate_input.title,
                    connector_type=candidate_input.connector_type,
                    format_hint=candidate_input.format_hint,
                    trust_level=candidate_input.trust_level,
                    raw_text=candidate_input.raw_text,
                    raw_storage_ref=candidate_input.raw_storage_ref,
                    actor_type=actor_type,
                    actor_id=actor_id,
                )
                candidate = self._build_candidate(
                    workspace_id=workspace.id,
                    discovery_run_id=run.id,
                    candidate_input=candidate_input,
                    decision_status="accepted",
                    decision_reason="auto_approved_by_policy",
                    accepted_library_item_id=registration.library_item.id,
                )
                accepted_count += 1
            session.add(candidate)

        run.status = "completed"
        run.accepted_count = accepted_count
        run.excluded_count = excluded_count
        run.decision_summary = (
            f"{accepted_count} accepted, {excluded_count} excluded, "
            f"{len(candidates) - accepted_count - excluded_count} pending review"
        )
        run.completed_at = utc_now()
        session.add(run)
        session.flush()
        self.audit_service.emit(
            session=session,
            workspace_id=workspace.id,
            actor_kind=actor_type,
            actor_id=str(actor_id or actor_type),
            action="library.discovery.complete",
            target_kind="library_discovery_run",
            target_id=run.id,
            payload={
                "run_token": run_token,
                "candidate_count": len(candidates),
                "accepted_count": accepted_count,
                "excluded_count": excluded_count,
                "policy_snapshot_hash": policy_hash,
            },
        )
        return run

    def register_library_item(
        self,
        session: Session,
        workspace_id: UUID,
        canonical_uri: str,
        title: str | None = None,
        connector_type: str = "web",
        format_hint: str = "unknown",
        trust_level: str = "unknown",
        raw_text: str | None = None,
        raw_storage_ref: str | None = None,
        actor_type: str = "system",
        actor_id: UUID | None = None,
        parent_node_id: UUID | None = None,
    ) -> LibraryRegistrationResult:
        if not canonical_uri.strip():
            raise ValueError("canonical_uri is required")

        normalized_uri = canonical_uri.strip()
        item = self.get_library_item_by_uri(
            session=session,
            workspace_id=workspace_id,
            uri=normalized_uri,
        )
        created_library_item = False
        content_hash = self._item_content_hash(
            raw_text=raw_text,
            raw_storage_ref=raw_storage_ref,
            fallback_uri=normalized_uri,
        )
        storage_ref = self._raw_storage_ref(
            raw_text=raw_text,
            raw_storage_ref=raw_storage_ref,
            content_hash=content_hash,
        )

        if item is None:
            candidate = LibraryItem(
                workspace_id=workspace_id,
                connector_type=connector_type,
                format_hint=format_hint,
                uri=normalized_uri,
                title=title,
                metadata_json={},
                trust_level=trust_level,
                lifecycle_status="extract_pending",
                created_by_actor_type=actor_type,
                created_by_actor_id=actor_id,
            )
            try:
                with session.begin_nested():
                    session.add(candidate)
                    session.flush()
                item = candidate
                created_library_item = True
            except IntegrityError:
                item = self.get_library_item_by_uri(
                    session=session,
                    workspace_id=workspace_id,
                    uri=normalized_uri,
                )
                if item is None:
                    raise
            if created_library_item:
                ensure_primary_tree_placement(
                    session=session,
                    workspace_id=workspace_id,
                    target_kind="library_item",
                    target_id=item.id,
                    title=item.title or item.uri,
                    slug=None,
                    section="library",
                    parent_id=parent_node_id,
                    default_parent_slug="library",
                    actor_kind=actor_type,
                    actor_id=str(actor_id or actor_type),
                    can_have_children=True,
                )
        # SQLite: FOR UPDATE is a no-op. The create path above already holds the
        # writer lock from its INSERT, but the already-exists path -- the
        # contended one -- reaches here with no DML emitted, and two concurrent
        # registrations then allocate the same version_no and collide on
        # library_item_version_no_uq. Repeating the statement is safe.
        acquire_write_intent(session)
        locked_item = self._lock_library_item_for_version_insert(
            session=session,
            library_item_id=item.id,
        )
        if locked_item is None:
            raise ReadNotFoundError("Library item disappeared during registration")
        item = locked_item

        if not created_library_item and title and item.title is None:
            item.title = title
            session.add(item)
            ensure_primary_tree_placement(
                session=session,
                workspace_id=workspace_id,
                target_kind="library_item",
                target_id=item.id,
                title=item.title or item.uri,
                slug=None,
                section="library",
                parent_id=None,
                default_parent_slug="library",
                actor_kind=actor_type,
                actor_id=str(actor_id or actor_type),
                can_have_children=True,
            )

        existing_version = self._get_library_item_version_by_hash(
            session=session,
            library_item_id=item.id,
            content_hash=content_hash,
        )
        created_version = existing_version is None
        if existing_version is None:
            next_version_no = self._next_version_no(
                session=session,
                library_item_id=item.id,
            )
            previous_version_id = (
                item.current_version_id if next_version_no > 1 else None
            )
            existing_version = LibraryItemVersion(
                workspace_id=item.workspace_id,
                library_item_id=item.id,
                version_no=next_version_no,
                content_hash=content_hash,
                raw_storage_ref=storage_ref,
                processing_status="pending",
                previous_version_id=previous_version_id,
            )
            session.add(existing_version)
            session.flush()
            item.current_version_id = existing_version.id
            item.lifecycle_status = "extract_pending"
            session.add(item)
            session.flush()

        extraction_job = self.job_queue.enqueue(
            session=session,
            workspace_id=workspace_id,
            job_type="extraction",
            trigger_type="library_registration",
            idempotency_key=str(existing_version.id),
            target_ref={
                "library_item_id": str(item.id),
                "library_item_version_id": str(existing_version.id),
            },
        )
        if created_library_item:
            self.audit_service.emit(
                session=session,
                workspace_id=workspace_id,
                actor_kind=actor_type,
                actor_id=str(actor_id or actor_type),
                action="library.item.register",
                target_kind="library_item",
                target_id=item.id,
                payload={
                    "library_item_version_id": str(existing_version.id),
                    "content_hash": existing_version.content_hash,
                    "uri": item.uri,
                },
            )
        elif created_version:
            self.audit_service.emit(
                session=session,
                workspace_id=workspace_id,
                actor_kind=actor_type,
                actor_id=str(actor_id or actor_type),
                action="library.item.version.create",
                target_kind="library_item",
                target_id=item.id,
                payload={
                    "library_item_version_id": str(existing_version.id),
                    "content_hash": existing_version.content_hash,
                    "uri": item.uri,
                },
            )
        from backend.services.indexing.fts5 import sync_library_doc_sources

        sync_library_doc_sources(session, item)
        return LibraryRegistrationResult(
            library_item=item,
            version=existing_version,
            extraction_job=extraction_job,
            created_library_item=created_library_item,
            created_version=created_version,
        )

    @staticmethod
    def policy_snapshot_hash(policy: dict[str, object]) -> str:
        payload = json.dumps(policy or {}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get_library_item_by_uri(
        self,
        session: Session,
        workspace_id: UUID,
        uri: str,
    ) -> LibraryItem | None:
        return session.scalar(
            select(LibraryItem).where(
                LibraryItem.workspace_id == workspace_id,
                LibraryItem.uri == uri.strip(),
            )
        )

    def _get_discovery_run(
        self,
        session: Session,
        workspace_id: UUID,
        policy_snapshot_hash: str,
        run_token: str,
    ) -> LibraryDiscoveryRun | None:
        return session.scalar(
            select(LibraryDiscoveryRun).where(
                LibraryDiscoveryRun.workspace_id == workspace_id,
                LibraryDiscoveryRun.policy_snapshot_hash == policy_snapshot_hash,
                LibraryDiscoveryRun.run_token == run_token,
            )
        )

    def _get_library_item_version_by_hash(
        self,
        session: Session,
        library_item_id: UUID,
        content_hash: str,
    ) -> LibraryItemVersion | None:
        return session.scalar(
            select(LibraryItemVersion).where(
                LibraryItemVersion.library_item_id == library_item_id,
                LibraryItemVersion.content_hash == content_hash,
            )
        )

    def _lock_library_item_for_version_insert(
        self,
        session: Session,
        library_item_id: UUID,
    ) -> LibraryItem | None:
        return session.scalar(
            library_item_for_version_insert_statement(library_item_id)
        )

    def _next_version_no(self, session: Session, library_item_id: UUID) -> int:
        current_max = session.scalar(
            select(func.max(LibraryItemVersion.version_no)).where(
                LibraryItemVersion.library_item_id == library_item_id
            )
        )
        return int(current_max or 0) + 1

    @staticmethod
    def _validate_candidate(candidate_input: LibraryCandidateInput) -> None:
        if not candidate_input.canonical_uri.strip():
            raise ValueError("candidate canonical_uri is required")

    @staticmethod
    def _build_candidate(
        workspace_id: UUID,
        discovery_run_id: UUID,
        candidate_input: LibraryCandidateInput,
        decision_status: str,
        decision_reason: str,
        accepted_library_item_id: UUID | None = None,
    ) -> LibraryDiscoveryCandidate:
        return LibraryDiscoveryCandidate(
            workspace_id=workspace_id,
            discovery_run_id=discovery_run_id,
            canonical_uri=candidate_input.canonical_uri.strip(),
            title=candidate_input.title,
            connector_type=candidate_input.connector_type,
            format_hint=candidate_input.format_hint,
            trust_level=candidate_input.trust_level,
            confidence=candidate_input.confidence,
            decision_status=decision_status,
            decision_reason=decision_reason,
            accepted_library_item_id=accepted_library_item_id,
            decided_at=utc_now() if decision_status != "pending_review" else None,
        )

    @staticmethod
    def _item_content_hash(
        raw_text: str | None,
        raw_storage_ref: str | None,
        fallback_uri: str,
    ) -> str:
        if raw_text is not None:
            return calculate_content_hash(raw_text)
        payload = raw_storage_ref or fallback_uri
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _raw_storage_ref(
        raw_text: str | None,
        raw_storage_ref: str | None,
        content_hash: str,
    ) -> str:
        if raw_storage_ref is not None:
            return raw_storage_ref
        if raw_text is not None:
            return inline_text_ref(raw_text)
        return f"external:{content_hash}"
