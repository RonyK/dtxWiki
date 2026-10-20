import hashlib
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.job import Job
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryAttempt,
    LibraryAttemptConversionDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
    LibraryRunConversionDetail,
)
from backend.services.audit_service import AuditService
from backend.services.db_write_intent import acquire_write_intent
from backend.services.job_queue import JobQueueService
from backend.services.library_storage import LibraryStorageError, read_text_ref
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
from backend.services.tree_service import (
    ensure_primary_tree_placement,
    primary_node_for_target,
)

if TYPE_CHECKING:
    from backend.media.application import InstallValidatedBlob


class ExtractionError(RuntimeError):
    pass


_CLASSIFIABLE_IMPORT_FORMATS = {
    "pdf",
    "application/pdf",
    "docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_READY_PROCESSING_STATUSES = frozenset({"completed", "ready", "ready_partial"})

# A-1: this is the trusted inline-text
# write lane's own single-attempt "conversion" -- it never goes through
# ConverterRegistry/the provider-chain machinery, so it gets its own provider
# identity rather than borrowing a real converter's.
_EXTRACTION_PROVIDER_ID = "library_extraction_service"
_EXTRACTION_PROVIDER_VERSION = "1"
# Deliberately not CONVERTER_SPI_VERSION: that constant asserts conformance to
# the Converter SPI protocol (ConverterDescriptor, ConversionRequest/Result),
# which this synchronous plain-text lane never implements. A distinct
# sentinel keeps that claim honest instead of borrowing "1.0" from converters
# this path has nothing to do with.
_EXTRACTION_PROVIDER_SPI_VERSION = "none"
# Describes only this lane's own tiny location_model shape (see
# _location_model), not the real conversion pipeline's output schema.
_EXTRACTION_OUTPUT_SCHEMA_VERSION = "1"
_EXTRACTION_POLICY_REVISION = hashlib.sha256(b"library-extraction-service").hexdigest()
_EXTRACTION_CONFIG_HASH = hashlib.sha256(b"{}").hexdigest()


class ExtractionService:
    def __init__(
        self,
        media_installer_factory: Callable[[Session, UUID], "InstallValidatedBlob"],
        job_queue: JobQueueService | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self.job_queue = job_queue or JobQueueService()
        self.audit_service = audit_service or AuditService()
        self.media_installer_factory = media_installer_factory

    def extract_library_item_version(
        self,
        session: Session,
        library_item_version_id: UUID,
        job: Job | None = None,
        raw_text: str | None = None,
        actor_type: str = "system",
        actor_id: UUID | None = None,
    ) -> LibraryArtifact:
        lineage = self._lock_lineage(
            session=session,
            library_item_version_id=library_item_version_id,
        )
        if lineage is None:
            raise ExtractionError(
                f"source version not found: {library_item_version_id}"
            )
        source, version = lineage
        if version.selected_conversion_attempt_id is not None:
            selected_artifact = self._get_selected_conversion_artifact(
                session=session,
                version=version,
            )
            if selected_artifact is None:
                raise ExtractionError(
                    "selected conversion attempt has no persisted artifact"
                )
            if job is not None and job.status != "completed":
                self.job_queue.complete(session=session, job=job)
            return selected_artifact

        existing_artifact = self._get_extracted_text_artifact(
            session=session,
            library_item_version_id=version.id,
        )
        if existing_artifact is not None:
            legacy_completion = self._promote_legacy_success(
                session=session,
                source=source,
                version=version,
                attempt_id=existing_artifact.attempt_id,
            )
            if legacy_completion:
                self._complete_legacy_projections(
                    session=session,
                    source=source,
                    version=version,
                    artifact=existing_artifact,
                    actor_type=actor_type,
                    actor_id=actor_id,
                )
            if job is not None and job.status != "completed":
                self.job_queue.complete(session=session, job=job)
            session.flush()
            return existing_artifact

        try:
            extracted_text = (
                raw_text if raw_text is not None else self._read_raw(version)
            )
        except (OSError, LibraryStorageError, UnicodeError) as exc:
            self._mark_failed(
                session=session,
                source=source,
                version=version,
                job=job,
                error=str(exc),
                actor_type=actor_type,
                actor_id=actor_id,
            )
            raise ExtractionError(str(exc)) from exc

        artifact = self._create_extraction_artifact(
            session=session,
            workspace_id=version.workspace_id,
            version=version,
            format_hint=source.format_hint,
            extracted_text=extracted_text,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        legacy_completion = self._promote_legacy_success(
            session=session,
            source=source,
            version=version,
            attempt_id=artifact.attempt_id,
        )
        if legacy_completion:
            version.extraction_summary = f"extracted {len(extracted_text)} characters"
            session.add(version)
        session.flush()
        if legacy_completion:
            self._complete_legacy_projections(
                session=session,
                source=source,
                version=version,
                artifact=artifact,
                actor_type=actor_type,
                actor_id=actor_id,
            )

        if job is not None:
            self.job_queue.complete(session=session, job=job)
        self.audit_service.emit(
            session=session,
            workspace_id=source.workspace_id,
            actor_kind=actor_type,
            actor_id=str(actor_id or actor_type),
            action="library.extraction.complete",
            target_kind="library_item_version",
            target_id=version.id,
            payload={
                "library_item_id": str(source.id),
                "artifact_id": str(artifact.id),
                "content_hash": version.content_hash,
                "character_count": len(extracted_text),
            },
        )
        return artifact

    def _create_extraction_artifact(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version: LibraryItemVersion,
        format_hint: str,
        extracted_text: str,
        actor_type: str,
        actor_id: UUID | None,
    ) -> LibraryArtifact:
        """Persist one Run/Attempt/Artifact triple for this synchronous lane.

        Mirrors ``LibraryConversionOrchestrator.schedule_initial``'s run_seq
        convention and ``_install_artifacts``'s Shared Media staging so this
        lane's artifact is structurally indistinguishable from a real
        conversion output -- just produced by a provider identity
        (``_EXTRACTION_PROVIDER_ID``) that never goes through
        ``ConverterRegistry``.
        """

        # run_seq is one counter shared across both stages for this version
        # (library_run_version_seq_uq is not stage-scoped) -- see
        # conversion_orchestrator.schedule_initial's identical query.
        run_seq = (
            session.scalar(
                select(func.max(LibraryRun.run_seq)).where(
                    LibraryRun.library_item_version_id == version.id
                )
            )
            or 0
        ) + 1
        now = utc_now()
        run = LibraryRun(
            workspace_id=workspace_id,
            library_item_version_id=version.id,
            stage="conversion",
            run_seq=run_seq,
            reason="initial",
            status="completed",
            requested_by_actor_type=actor_type,
            requested_by_actor_id=actor_id,
            provider_chain_json=[],
            policy_revision=_EXTRACTION_POLICY_REVISION,
            output_schema_version=_EXTRACTION_OUTPUT_SCHEMA_VERSION,
            idempotency_key=f"initial:{version.id}",
            finished_at=now,
        )
        run.conversion_detail = LibraryRunConversionDetail(
            error_fallback_matrix_json={},
            per_provider_budget_json={},
            total_budget_json={},
        )
        session.add(run)
        session.flush()

        attempt = LibraryAttempt(
            workspace_id=workspace_id,
            run_id=run.id,
            library_item_version_id=version.id,
            stage="conversion",
            provider_id=_EXTRACTION_PROVIDER_ID,
            provider_version=_EXTRACTION_PROVIDER_VERSION,
            provider_spi_version=_EXTRACTION_PROVIDER_SPI_VERSION,
            config_hash=_EXTRACTION_CONFIG_HASH,
            input_sha256=version.content_hash,
            output_schema_version=run.output_schema_version,
            status="completed",
            chain_index=0,
            started_at=now,
            finished_at=now,
        )
        attempt.conversion_detail = LibraryAttemptConversionDetail(
            manual_retry_disposition=None,
            disposition_reason=None,
        )
        session.add(attempt)
        session.flush()

        media_type = self._extraction_method(format_hint)
        content_bytes = extracted_text.encode("utf-8")
        installer = self.media_installer_factory(session, workspace_id)
        available = installer.execute(
            workspace_id=workspace_id,
            actor_id=attempt.id,
            session_key=f"library-extraction:{attempt.id}",
            idempotency_key=f"library-extraction:{attempt.id}:markdown:0",
            content=content_bytes,
            max_bytes=len(content_bytes),
            canonical_media_type=media_type,
            format_id="derived-markdown",
            detector_id="library-extraction-output",
            detector_version=_EXTRACTION_PROVIDER_VERSION,
            registry_version=_EXTRACTION_PROVIDER_SPI_VERSION,
            validation_schema_version=_EXTRACTION_OUTPUT_SCHEMA_VERSION,
            metadata={
                "role": "markdown",
                "ordinal": 0,
                "provider_id": _EXTRACTION_PROVIDER_ID,
                "provider_version": _EXTRACTION_PROVIDER_VERSION,
            },
        )
        if (
            available.reference.workspace_id != workspace_id
            or available.byte_size != len(content_bytes)
        ):
            raise RuntimeError(
                "Shared Media installer changed immutable extraction facts"
            )

        from backend.services.conversion_orchestrator import (
            _artifact_kind_for_media_type,
        )

        artifact = LibraryArtifact(
            workspace_id=workspace_id,
            library_item_version_id=version.id,
            attempt_id=attempt.id,
            stage="conversion",
            blob_workspace_id=available.reference.workspace_id,
            blob_id=available.reference.blob_id,
            blob_epoch=available.reference.epoch.value,
            role="markdown",
            kind=_artifact_kind_for_media_type(media_type),
            media_type=media_type,
            content_sha256=available.sha256.value,
            byte_size=available.byte_size,
        )
        artifact.conversion_detail = LibraryArtifactConversionDetail(
            is_complete=True,
            sanitizer_version=None,
            extraction_method=media_type,
            index_text=extracted_text,
            location_model=self._location_model(extracted_text),
            quality_score=Decimal("1.000"),
        )
        session.add(artifact)
        session.flush()
        return artifact

    @staticmethod
    def _produce_import_classification(
        session: Session,
        source: LibraryItem,
        version: LibraryItemVersion,
    ) -> None:
        """Queue Q-8 only after extraction has produced usable source text."""
        if source.format_hint.strip().lower() not in _CLASSIFIABLE_IMPORT_FORMATS:
            return
        from backend.services.delegated_tasks import produce_import_batch_classify_task

        produce_import_batch_classify_task(
            session,
            workspace_id=source.workspace_id,
            library_item_id=source.id,
            version_id=version.id,
            title=source.title or source.uri[:256],
            uri=source.uri,
            format_hint=source.format_hint,
        )

    def mark_version_failed(
        self,
        session: Session,
        library_item_version_id: UUID,
        error: str,
        actor_type: str = "system",
        actor_id: UUID | None = None,
    ) -> None:
        """Persist extraction failure markers (version/source status + the
        failure audit event) outside the extraction transaction, e.g. after
        a two-phase dispatcher rolled back the business session. A version
        that already completed is left untouched so a stale failed job cannot
        regress a successful extraction."""
        lineage = self._lock_lineage(
            session=session,
            library_item_version_id=library_item_version_id,
        )
        if lineage is None:
            return
        source, version = lineage
        if (
            version.processing_status in _READY_PROCESSING_STATUSES
            or version.selected_conversion_attempt_id is not None
        ):
            return
        self._mark_failed(
            session=session,
            source=source,
            version=version,
            job=None,
            error=error,
            actor_type=actor_type,
            actor_id=actor_id,
        )

    @staticmethod
    def _lock_lineage(
        session: Session,
        library_item_version_id: UUID,
    ) -> tuple[LibraryItem, LibraryItemVersion] | None:
        """Serialize legacy completion with conversion promotion.

        All Library ready-pointer writers lock the item before its version. The
        SQLite write intent supplies the equivalent serialization because
        ``FOR UPDATE`` is ignored there. ``populate_existing`` is essential:
        workers may retain ORM identities across a lease boundary and must
        compare against a ready pointer committed by a competing worker.
        """

        acquire_write_intent(session)
        library_item_id = session.scalar(
            select(LibraryItemVersion.library_item_id).where(
                LibraryItemVersion.id == library_item_version_id
            )
        )
        if library_item_id is None:
            return None
        source = session.scalar(
            select(LibraryItem)
            .where(LibraryItem.id == library_item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        version = session.scalar(
            select(LibraryItemVersion)
            .where(LibraryItemVersion.id == library_item_version_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if source is None or version is None or version.library_item_id != source.id:
            return None
        return source, version

    @staticmethod
    def _promote_legacy_success(
        *,
        session: Session,
        source: LibraryItem,
        version: LibraryItemVersion,
        attempt_id: UUID,
    ) -> bool:
        """Complete and promote a pre-converter extraction using version CAS.

        A selected conversion attempt owns the version status and artifact set,
        so a delayed legacy job becomes state-inert. Otherwise the candidate
        replaces the canonical ready pointer only when no newer successful
        version is already selected. A-1: ``LibraryArtifact.attempt_id`` is
        NOT NULL now, so this lane's own attempt must also become the
        version's selected conversion attempt -- otherwise
        ``list_canonical_library_artifacts``/``get_canonical_markdown_artifact``
        (both gated on ``selected_conversion_attempt_id``) would never see it.
        """

        if version.selected_conversion_attempt_id is not None:
            return False

        version.selected_conversion_attempt_id = attempt_id
        version.processing_status = "completed"
        if source.lifecycle_status not in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
            source.lifecycle_status = "ready"

        current_ready = (
            session.get(
                LibraryItemVersion,
                source.current_ready_version_id,
                populate_existing=True,
            )
            if source.current_ready_version_id is not None
            else None
        )
        current_ready_is_valid = (
            current_ready is not None
            and current_ready.library_item_id == source.id
            and current_ready.processing_status in _READY_PROCESSING_STATUSES
        )
        if (
            not current_ready_is_valid
            or current_ready is not None
            and version.version_no > current_ready.version_no
        ):
            source.current_ready_version_id = version.id
        session.add_all([version, source])
        return True

    def _complete_legacy_projections(
        self,
        *,
        session: Session,
        source: LibraryItem,
        version: LibraryItemVersion,
        artifact: LibraryArtifact,
        actor_type: str,
        actor_id: UUID | None,
    ) -> None:
        self._ensure_tree_placements(
            session=session,
            source=source,
            artifact=artifact,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        from backend.services.indexing.fts5 import sync_library_doc_sources

        sync_library_doc_sources(session, source)
        if source.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
            return
        self._produce_import_classification(session, source, version)
        # Keyed by content_hash so each new version triggers a fresh embed job;
        # enqueue is a no-op when no embedding provider is configured.
        self.job_queue.enqueue(
            session=session,
            workspace_id=source.workspace_id,
            job_type="embed",
            trigger_type="library_extraction",
            idempotency_key=f"library_item:{source.id}:{version.content_hash}",
            target_ref={"kind": "library_item", "id": str(source.id)},
        )

    def _ensure_tree_placements(
        self,
        session: Session,
        source: LibraryItem,
        artifact: LibraryArtifact,
        actor_type: str,
        actor_id: UUID | None,
    ) -> None:
        actor_id_value = str(actor_id or actor_type)
        source_node = ensure_primary_tree_placement(
            session=session,
            workspace_id=source.workspace_id,
            target_kind="library_item",
            target_id=source.id,
            title=source.title or source.uri[:256],
            slug=None,
            section="library",
            parent_id=None,
            default_parent_slug="library",
            actor_kind=actor_type,
            actor_id=actor_id_value,
            can_have_children=True,
        )
        if primary_node_for_target(
            session=session,
            workspace_id=source.workspace_id,
            target_kind="library_artifact",
            target_id=artifact.id,
        ):
            return
        ensure_primary_tree_placement(
            session=session,
            workspace_id=source.workspace_id,
            target_kind="library_artifact",
            target_id=artifact.id,
            # This lane only ever produces one kind of artifact -- a static
            # label is more honest than deriving one from a field (the old
            # artifact_type) that no longer exists.
            title="Extracted Text",
            slug=None,
            section="library",
            parent_id=source_node.id,
            default_parent_slug="library",
            actor_kind=actor_type,
            actor_id=actor_id_value,
            can_have_children=False,
        )

    def _mark_failed(
        self,
        session: Session,
        source: LibraryItem,
        version: LibraryItemVersion,
        job: Job | None,
        error: str,
        actor_type: str,
        actor_id: UUID | None,
    ) -> None:
        if (
            version.processing_status in _READY_PROCESSING_STATUSES
            or version.selected_conversion_attempt_id is not None
        ):
            return
        version.processing_status = "failed"
        version.extraction_summary = error
        if source.lifecycle_status not in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
            source.lifecycle_status = (
                "ready"
                if source.current_ready_version_id is not None
                else "extract_failed"
            )
        session.add_all([version, source])
        if job is not None:
            self.job_queue.fail(session=session, job=job, error=error, max_retries=1)
        session.flush()
        self.audit_service.emit(
            session=session,
            workspace_id=source.workspace_id,
            actor_kind=actor_type,
            actor_id=str(actor_id or actor_type),
            action="library.extraction.failed",
            target_kind="library_item_version",
            target_id=version.id,
            payload={
                "library_item_id": str(source.id),
                "content_hash": version.content_hash,
                "error": error,
            },
        )

    @staticmethod
    def _get_extracted_text_artifact(
        session: Session,
        library_item_version_id: UUID,
    ) -> LibraryArtifact | None:
        """Find this lane's own prior artifact, for idempotent retries.

        Identified by provider identity rather than a nullable attempt FK
        (A-1 retired that shape entirely -- ``LibraryArtifact.attempt_id`` is
        NOT NULL now), so this still only matches an attempt this same lane
        created, never a real conversion attempt.
        """

        return session.scalar(
            select(LibraryArtifact)
            .join(LibraryAttempt, LibraryAttempt.id == LibraryArtifact.attempt_id)
            .where(
                LibraryArtifact.library_item_version_id == library_item_version_id,
                LibraryArtifact.stage == "conversion",
                LibraryAttempt.provider_id == _EXTRACTION_PROVIDER_ID,
            )
            .order_by(
                LibraryArtifact.ordinal.asc(),
                LibraryArtifact.created_at.asc(),
                LibraryArtifact.id.asc(),
            )
            .limit(1)
        )

    @staticmethod
    def _get_selected_conversion_artifact(
        *,
        session: Session,
        version: LibraryItemVersion,
    ) -> LibraryArtifact | None:
        return session.scalar(
            select(LibraryArtifact)
            .where(
                LibraryArtifact.library_item_version_id == version.id,
                LibraryArtifact.stage == "conversion",
                LibraryArtifact.attempt_id == version.selected_conversion_attempt_id,
            )
            .order_by(
                LibraryArtifact.ordinal.asc(),
                LibraryArtifact.created_at.asc(),
                LibraryArtifact.id.asc(),
            )
            .limit(1)
        )

    @staticmethod
    def _read_raw(version: LibraryItemVersion) -> str:
        return read_text_ref(version.raw_storage_ref)

    @staticmethod
    def _extraction_method(format_hint: str) -> str:
        if format_hint in {"markdown", "md"}:
            return "text/markdown"
        if format_hint in {"html", "web"}:
            return "text/html"
        return "text/plain"

    @staticmethod
    def _location_model(text: str) -> dict[str, int | str]:
        lines = text.splitlines()
        return {
            "kind": "plain_text",
            "line_count": len(lines),
            "character_count": len(text),
        }
