"""Persisted, fenced Library conversion scheduling and execution.

The API process only schedules immutable recipes. Converter code runs from the
worker through this service, and only a completed, validated attempt may become
the canonical artifact set for a source version.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.library_media import LibraryBlobCandidate, RegisterLibraryBlob
from backend.library_media.domain import LibraryBlobRecord
from backend.library_media.ports import (
    LibraryBlobRepositoryFactory,
    LibraryLogicalQuotaExceededError,
    LibraryLogicalQuotaFactory,
)
from backend.media.application import InstallValidatedBlob
from backend.media.domain import (
    AvailableBlobRecord,
    BlobEpoch,
    BlobReference,
    Sha256Digest,
    WorkspaceBlobIdentity,
)
from backend.media.ports.install import (
    BlobAssociationHold,
    BlobAssociationHoldProducerKind,
)
from backend.models.base import utc_now
from backend.models.job import Job
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryAttempt,
    LibraryAttemptConversionDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryLogicalCharge,
    LibraryProjectionOutbox,
    LibraryRun,
    LibraryRunConversionDetail,
)
from backend.repositories.media_blob_reference import (
    BlobReferenceOwnerKind,
    SqlAlchemyBlobReferenceLedger,
    lock_workspace_blob_reference_intent,
)
from backend.repositories.workspace_scoped import get_in_workspace
from backend.services.audit_service import AuditService
from backend.services.conversion_failure_policy import (
    decide_conversion_manual_retry,
    is_conversion_timeout_error,
    is_fail_closed_conversion_error,
    is_retryable_conversion_failure,
)
from backend.services.conversion_result_validator import (
    SANITIZER_VERSION,
    validate_conversion_result,
)
from backend.services.converter_registry import ConverterRegistry
from backend.services.db_write_intent import acquire_write_intent
from backend.services.job_queue import JobQueueService
from backend.services.library_blob_storage import (
    FilesystemLibraryBlobStore,
    StagedLibraryBlob,
    StoredLibraryBlob,
)
from backend.spi.conversion import (
    CONVERSION_OUTPUT_SCHEMA_VERSION,
    ConversionError,
    ConversionLimits,
    ConversionRequest,
    ProbeRequest,
    UnsupportedConversionError,
)

if TYPE_CHECKING:
    from backend.services.preview_orchestrator import LibraryPreviewOrchestrator

LIBRARY_CONVERSION_JOB_TYPE = "library_conversion"
MAX_TRANSPORT_RETRIES = 1
# One conversion may persist every SPI asset plus an optional Markdown artifact.
# The durable Shared Media installer profile uses this same exported ceiling.
MAX_DURABLE_CONVERSION_OUTPUTS = 65
_FALLBACK_ACTIONS = frozenset({"stop", "next_provider", "next_provider_if_allowed"})
_PROVIDER_SNAPSHOT_KEYS = frozenset(
    {
        "converter_id",
        "converter_version",
        "spi_version",
        "priority",
        "execution_mode",
        "network_required",
        "deterministic",
        "license_expression",
        "distribution_digest",
        "sbom_sha256",
        "signature_identity",
        "sandbox_profile",
        "max_input_bytes",
        "output_schema_version",
    }
)
_PROVIDER_REQUIRED_STRING_FIELDS = frozenset(
    {
        "converter_id",
        "converter_version",
        "spi_version",
        "execution_mode",
        "license_expression",
        "output_schema_version",
    }
)
_PROVIDER_OPTIONAL_STRING_FIELDS = frozenset(
    {
        "distribution_digest",
        "sbom_sha256",
        "signature_identity",
        "sandbox_profile",
    }
)
_FALLBACK_MATRIX: dict[str, str] = {
    "unsupported": "next_provider",
    "converter_result_invalid": "next_provider",
    "invalid_format": "stop",
    "encrypted_document": "stop",
    "active_content_denied": "stop",
    "policy_rejected": "stop",
    "default": "next_provider_if_allowed",
}


@dataclass(frozen=True, slots=True)
class ScheduledConversion:
    run: LibraryRun
    attempt: LibraryAttempt | None
    job: Job | None


@dataclass(frozen=True, slots=True)
class ConversionExecution:
    attempt_id: UUID
    run_id: UUID
    version_id: UUID
    status: Literal[
        "ready",
        "ready_partial",
        "requeued",
        "fallback_queued",
        "failed",
        "timed_out",
        "stale",
    ]
    selected: bool = False
    next_attempt_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RetriedConversion:
    run: LibraryRun
    attempt: LibraryAttempt
    job: Job


class LibraryConversionRetryNotFoundError(RuntimeError):
    """The requested Library item/version association is not visible here."""


class LibraryConversionRetryNotAvailableError(RuntimeError):
    """The current durable conversion state cannot be manually retried."""


class _PersistedConversionSnapshotError(ConversionError):
    """Fail-closed durable error that must not consult persisted policy again."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(
            code,
            safe_message,
            retryable=False,
            fallback_allowed=False,
        )


@dataclass(frozen=True, slots=True)
class _MaterializedArtifact:
    role: str
    media_type: str
    ordinal: int
    content: bytes
    max_bytes: int
    index_text: str | None = None
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class _InstalledArtifact:
    role: str
    media_type: str
    ordinal: int
    available: AvailableBlobRecord
    index_text: str | None = None
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class _StagedArtifact:
    role: str
    media_type: str
    ordinal: int
    staged: StagedLibraryBlob
    max_bytes: int
    index_text: str | None = None
    filename: str | None = None


class LibraryConversionOrchestrator:
    """Application service for immutable conversion runs and canonical CAS."""

    def __init__(
        self,
        *,
        converter_registry: ConverterRegistry,
        store_for: Callable[[UUID], FilesystemLibraryBlobStore],
        blob_repository_factory: LibraryBlobRepositoryFactory,
        media_installer_factory: Callable[[Session, UUID], InstallValidatedBlob],
        logical_quota_factory: LibraryLogicalQuotaFactory,
        durable_media_installer_factory: Callable[
            [sessionmaker[Session], UUID], InstallValidatedBlob
        ]
        | None = None,
        job_queue: JobQueueService | None = None,
        audit_service: AuditService | None = None,
        preferred_chains: dict[str, tuple[str, ...]] | None = None,
        allow_network: bool = False,
        conversion_enabled: bool = True,
        preview_orchestrator: LibraryPreviewOrchestrator | None = None,
    ) -> None:
        self.converter_registry = converter_registry
        # D-5: resolved per call, not cached -- see LibraryContentService.
        self.store_for = store_for
        self.blob_repository_factory = blob_repository_factory
        self.media_installer_factory = media_installer_factory
        self.durable_media_installer_factory = durable_media_installer_factory
        self.logical_quota_factory = logical_quota_factory
        self.job_queue = job_queue or JobQueueService()
        self.audit_service = audit_service or AuditService()
        self.preferred_chains = dict(preferred_chains or {})
        self.allow_network = allow_network
        self.conversion_enabled = conversion_enabled
        self.preview_orchestrator = preview_orchestrator

    def schedule_initial(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version: LibraryItemVersion,
        workspace_policy: dict[str, Any] | None,
        limits: ConversionLimits,
        actor_kind: str,
        actor_id: str,
        allow_conversion: bool = True,
    ) -> ScheduledConversion:
        """Create exactly one initial run recipe for a persisted source version."""

        if version.source_blob_id is None or not version.format_id:
            raise ValueError("binary source identity is required for conversion")
        acquire_write_intent(session)
        locked_item = session.scalar(
            select(LibraryItem)
            .where(LibraryItem.id == version.library_item_id)
            .with_for_update()
        )
        locked_version = session.scalar(
            select(LibraryItemVersion)
            .where(LibraryItemVersion.id == version.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            locked_item is None
            or locked_item.workspace_id != workspace_id
            or locked_version is None
        ):
            raise ValueError("binary source association is invalid")
        version = locked_version
        idempotency_key = f"initial:{version.id}"
        existing = session.scalar(
            select(LibraryRun).where(
                LibraryRun.library_item_version_id == version.id,
                LibraryRun.stage == "conversion",
                LibraryRun.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return self._existing_schedule(
                session=session,
                workspace_id=workspace_id,
                run=existing,
            )

        preferred = self.preferred_chains.get(version.format_id, ())
        chain = (
            self.converter_registry.provider_chain_snapshot(
                version.format_id,
                preferred_ids=preferred,
                allow_network=self.allow_network,
                ready_only=True,
            )
            if self.conversion_enabled and allow_conversion
            else []
        )
        # run_seq is one counter shared across both stages for this version
        # (library_run_version_seq_uq is not stage-scoped) -- do not filter by
        # stage here.
        run_seq = (
            session.scalar(
                select(func.max(LibraryRun.run_seq)).where(
                    LibraryRun.library_item_version_id == version.id
                )
            )
            or 0
        ) + 1
        policy_revision = _stable_hash(workspace_policy or {})
        run = LibraryRun(
            workspace_id=version.workspace_id,
            library_item_version_id=version.id,
            stage="conversion",
            run_seq=run_seq,
            reason="initial",
            status="queued" if chain else "stored_unconverted",
            requested_by_actor_type=actor_kind,
            requested_by_actor_id=_uuid_or_none(actor_id),
            provider_chain_json=chain,
            policy_revision=policy_revision,
            output_schema_version=CONVERSION_OUTPUT_SCHEMA_VERSION,
            idempotency_key=idempotency_key,
            finished_at=utc_now() if not chain else None,
        )
        run.conversion_detail = LibraryRunConversionDetail(
            error_fallback_matrix_json=dict(_FALLBACK_MATRIX),
            per_provider_budget_json={
                "limits": asdict(limits),
                "max_transport_retries": MAX_TRANSPORT_RETRIES,
            },
            total_budget_json={
                "max_provider_attempts": len(chain),
                "max_wall_seconds": limits.timeout_seconds * max(len(chain), 1),
                "max_output_bytes_per_provider": limits.max_output_bytes,
                "max_asset_bytes_per_provider": limits.max_asset_bytes,
            },
        )
        session.add(run)
        session.flush()
        if not chain:
            version.processing_status = "stored_unconverted"
            session.add(version)
            return ScheduledConversion(run=run, attempt=None, job=None)

        attempt = self._new_attempt(
            run=run,
            version=version,
            chain_index=0,
            parent_attempt_id=None,
        )
        session.add(attempt)
        session.flush()
        version.processing_status = "queued"
        session.add(version)
        job = self._enqueue_attempt(
            session=session,
            workspace_id=workspace_id,
            attempt=attempt,
        )
        return ScheduledConversion(run=run, attempt=attempt, job=job)

    def retry_failed_conversion(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        library_item_id: UUID,
        version_id: UUID,
        actor_kind: str,
        actor_id: str,
    ) -> RetriedConversion:
        """Atomically requeue one retryable terminal conversion generation.

        A conversion provider can report a durable failure as a successful
        queue execution, leaving its Job ``completed`` while the attempt, run,
        and version are terminally failed. Worker infrastructure failure takes
        another path and leaves the Job ``failed``. The two-phase worker can
        also commit the domain failure before its Job completion transaction,
        leaving an exact-generation Job ``running`` briefly or until lease
        reconciliation after a crash. Item-level retry reconciles all three
        shapes without creating a second attempt or exposing the Job as a user
        concept; advancing the generation fences any late phase-two completion.

        Transactions that touch both queue and conversion state use one global
        PostgreSQL lock order: Job, item, version, run, attempt. This matches
        legacy dispatch, stale-lease reclaim, and isolated worker failure
        recovery, all of which already own the Job row before resetting domain
        state. The initial lineage reads below are unlocked discovery only;
        every mutable predicate is rechecked after the Job and lineage rows
        have been locked, so concurrent state changes fail closed.

        A manual retry is one new user-initiated conversion execution. It keeps
        the Job's infrastructure ``retry_count`` (so it does not grant a fresh
        queue retry budget) but resets the attempt's provider-transport counter
        so the snapshotted converter retry policy applies once to this new
        execution. Failure fields are cleared from the live attempt only after
        an append-only audit snapshot has captured the prior generation.
        """

        acquire_write_intent(session)
        seed_item = session.scalar(
            select(LibraryItem)
            .where(
                LibraryItem.workspace_id == workspace_id,
                LibraryItem.id == library_item_id,
            )
            .execution_options(populate_existing=True)
        )
        if seed_item is None:
            raise LibraryConversionRetryNotFoundError("Library item was not found")
        seed_version = session.scalar(
            select(LibraryItemVersion)
            .where(
                LibraryItemVersion.workspace_id == workspace_id,
                LibraryItemVersion.library_item_id == seed_item.id,
                LibraryItemVersion.id == version_id,
            )
            .execution_options(populate_existing=True)
        )
        if seed_version is None:
            raise LibraryConversionRetryNotFoundError(
                "Library item version was not found"
            )
        seed_run = session.scalar(
            select(LibraryRun)
            .where(
                LibraryRun.workspace_id == workspace_id,
                LibraryRun.library_item_version_id == seed_version.id,
                LibraryRun.stage == "conversion",
            )
            .order_by(LibraryRun.run_seq.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        if seed_run is None:
            raise LibraryConversionRetryNotAvailableError(
                "Library conversion has no retryable run"
            )
        seed_attempt = session.scalar(
            select(LibraryAttempt)
            .where(
                LibraryAttempt.workspace_id == workspace_id,
                LibraryAttempt.run_id == seed_run.id,
                LibraryAttempt.stage == "conversion",
            )
            .order_by(LibraryAttempt.chain_index.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        if seed_attempt is None:
            raise LibraryConversionRetryNotAvailableError(
                "Library conversion has no retryable attempt"
            )

        seed_generation = seed_attempt.execution_generation
        previous_key = f"{seed_attempt.id}:{seed_generation}"
        # Global lock order starts here. The worker's legacy dispatch,
        # reclaim, and isolated-failure paths all mutate/lock this Job before
        # calling their domain reset helper.
        job = session.scalar(
            select(Job)
            .where(
                Job.workspace_id == workspace_id,
                Job.job_type == LIBRARY_CONVERSION_JOB_TYPE,
                Job.idempotency_key == previous_key,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if job is None:
            raise LibraryConversionRetryNotAvailableError(
                "Library conversion retry state changed"
            )

        item = session.scalar(
            select(LibraryItem)
            .where(
                LibraryItem.workspace_id == workspace_id,
                LibraryItem.id == library_item_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        version = session.scalar(
            select(LibraryItemVersion)
            .where(
                LibraryItemVersion.workspace_id == workspace_id,
                LibraryItemVersion.library_item_id == library_item_id,
                LibraryItemVersion.id == version_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if item is None:
            raise LibraryConversionRetryNotFoundError("Library item was not found")
        if version is None:
            raise LibraryConversionRetryNotFoundError(
                "Library item version was not found"
            )
        if item.current_version_id != version.id:
            raise LibraryConversionRetryNotAvailableError(
                "Only the current Library item version can be retried"
            )

        run = session.scalar(
            select(LibraryRun)
            .where(
                LibraryRun.workspace_id == workspace_id,
                LibraryRun.library_item_version_id == version.id,
                LibraryRun.stage == "conversion",
            )
            .order_by(LibraryRun.run_seq.desc())
            .limit(1)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if run is None:
            raise LibraryConversionRetryNotAvailableError(
                "Library conversion has no retryable run"
            )
        attempt = session.scalar(
            select(LibraryAttempt)
            .where(
                LibraryAttempt.workspace_id == workspace_id,
                LibraryAttempt.run_id == run.id,
                LibraryAttempt.stage == "conversion",
            )
            .order_by(LibraryAttempt.chain_index.desc())
            .limit(1)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if attempt is None:
            raise LibraryConversionRetryNotAvailableError(
                "Library conversion has no retryable attempt"
            )
        if (
            version.selected_conversion_attempt_id is not None
            or version.processing_status not in {"failed", "timed_out"}
            or run.status not in {"failed", "timed_out"}
            or attempt.status not in {"failed", "timed_out"}
            or not is_retryable_conversion_failure(
                status=version.processing_status,
                error_code=attempt.error_code,
                manual_retry_disposition=attempt.conversion_detail.manual_retry_disposition,
                disposition_reason=attempt.conversion_detail.disposition_reason,
            )
        ):
            raise LibraryConversionRetryNotAvailableError(
                "Library conversion is not retryable"
            )

        previous_generation = attempt.execution_generation
        target = job.target_ref if job is not None else None
        if (
            attempt.id != seed_attempt.id
            or run.id != seed_run.id
            or previous_generation != seed_generation
            or job.idempotency_key != previous_key
            or job.status not in {"completed", "failed", "running"}
            or not isinstance(target, dict)
            or target.get("library_conversion_attempt_id") != str(attempt.id)
            or target.get("library_conversion_run_id") != str(run.id)
            or target.get("library_item_version_id") != str(version.id)
            or target.get("execution_generation") != previous_generation
        ):
            raise LibraryConversionRetryNotAvailableError(
                "Library conversion retry state changed"
            )

        previous_failure = {
            "attempt_status": attempt.status,
            "execution_generation": previous_generation,
            "transport_retry_count": attempt.transport_retry_count,
            "error_code": attempt.error_code,
            "safe_error_summary": attempt.safe_error_summary,
            "manual_retry_disposition": (
                attempt.conversion_detail.manual_retry_disposition
            ),
            "disposition_reason": attempt.conversion_detail.disposition_reason,
            # Production failure paths populate the bounded scalar error
            # envelope, not these success-only result fields. Preserve compact
            # integrity evidence if a legacy/inconsistent row carries them;
            # copying arbitrary JSON into an append-only audit would widen the
            # sensitive-data and storage surface on every manual retry.
            "warnings_evidence": _retry_audit_json_evidence(attempt.warnings_json),
            "provenance_evidence": _retry_audit_json_evidence(attempt.provenance_json),
            "metrics_evidence": _retry_audit_json_evidence(attempt.metrics_json),
            "job_id": str(job.id),
            "job_status": job.status,
            "job_idempotency_key": job.idempotency_key,
            "job_retry_count": job.retry_count,
        }
        attempt.execution_generation += 1
        attempt.transport_retry_count = 0
        attempt.status = "queued"
        attempt.started_at = None
        attempt.finished_at = None
        attempt.error_code = None
        attempt.safe_error_summary = None
        attempt.conversion_detail.manual_retry_disposition = None
        attempt.conversion_detail.disposition_reason = None
        attempt.warnings_json = []
        attempt.provenance_json = {}
        attempt.metrics_json = {}
        run.status = "queued"
        run.finished_at = None
        version.processing_status = "queued"

        target_ref = dict(target)
        target_ref["execution_generation"] = attempt.execution_generation
        job.target_ref = target_ref
        job.idempotency_key = f"{attempt.id}:{attempt.execution_generation}"
        job.status = "queued"
        job.stage = None
        job.progress = 0
        job.started_at = None
        job.next_attempt_at = None
        job.completed_at = None
        session.add_all([attempt, run, version, job])
        session.flush()
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="library.conversion.retried",
            target_kind="library_conversion_attempt",
            target_id=attempt.id,
            payload={
                "library_item_id": str(item.id),
                "library_item_version_id": str(version.id),
                "conversion_run_id": str(run.id),
                "execution_generation": attempt.execution_generation,
                "retry_count": job.retry_count,
                "previous_failure": previous_failure,
            },
            event_summary="A user retried a failed Library conversion",
            retention_class="sensitive",
        )
        return RetriedConversion(run=run, attempt=attempt, job=job)

    def execute_attempt(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt_id: UUID,
        execution_generation: int,
    ) -> ConversionExecution:
        """Execute one snapshotted provider and atomically promote its result."""

        attempt = get_in_workspace(
            session,
            LibraryAttempt,
            workspace_id=workspace_id,
            resource_id=attempt_id,
        )
        if attempt is None or attempt.stage != "conversion":
            raise ValueError("Library conversion attempt was not found")
        run = get_in_workspace(
            session,
            LibraryRun,
            workspace_id=workspace_id,
            resource_id=attempt.run_id,
        )
        if run is not None and run.stage != "conversion":
            run = None
        version = get_in_workspace(
            session,
            LibraryItemVersion,
            workspace_id=workspace_id,
            resource_id=attempt.library_item_version_id,
        )
        if run is None or version is None:
            raise ValueError("Library conversion lineage is incomplete")
        item = get_in_workspace(
            session,
            LibraryItem,
            workspace_id=workspace_id,
            resource_id=version.library_item_id,
        )
        blob = _resolve_version_source_blob(
            session,
            version,
            repository_factory=self.blob_repository_factory,
        )
        if (
            item is None
            or item.workspace_id != workspace_id
            or blob is None
            or blob.reference.workspace_id != workspace_id
        ):
            raise ValueError("Library conversion source association is invalid")
        if attempt.execution_generation != execution_generation:
            return self._execution(attempt, run, "stale")
        if attempt.status == "completed":
            selected = version.selected_conversion_attempt_id == attempt.id
            return self._execution(
                attempt,
                run,
                version.processing_status if selected else "stale",
                selected=selected,
            )
        if attempt.status not in {"queued", "running"}:
            return self._execution(attempt, run, "stale")

        try:
            chain_entry = self._chain_entry(run, attempt.chain_index)
            _validate_attempt_recipe(
                attempt=attempt,
                run=run,
                version=version,
                chain_entry=chain_entry,
            )
            descriptor = self.converter_registry.descriptor(attempt.provider_id)
            if _provider_snapshot(descriptor) != chain_entry:
                raise ConversionError(
                    "converter_recipe_changed",
                    "The converter recipe no longer matches the scheduled run.",
                    retryable=False,
                    fallback_allowed=False,
                )
            plugin = self.converter_registry.plugin(attempt.provider_id)
            limits = _limits_from_run(run)
            _validate_total_budget(run)
            if attempt.transport_retry_count > _max_transport_retries(run):
                raise ConversionError(
                    "converter_retry_budget_exhausted",
                    "The converter retry budget was exhausted.",
                    retryable=False,
                    fallback_allowed=False,
                )
            media_type = version.detected_media_type or blob.media_type
            if not version.format_id or not media_type or not version.original_filename:
                raise ConversionError(
                    "converter_source_invalid",
                    "The Library source metadata is incomplete.",
                    retryable=False,
                    fallback_allowed=False,
                )
            probe = plugin.probe(
                ProbeRequest(
                    format_id=version.format_id,
                    media_type=media_type,
                    original_filename=version.original_filename,
                    byte_size=blob.byte_size,
                )
            )
            if not probe.supported:
                raise UnsupportedConversionError(
                    "The converter does not support this source."
                )
            handle = self.store_for(workspace_id).open_handle(
                blob.storage_key,
                byte_size=blob.byte_size,
            )
            result = plugin.convert(
                ConversionRequest(
                    source=handle,
                    source_sha256=version.content_hash,
                    byte_size=blob.byte_size,
                    format_id=version.format_id,
                    media_type=media_type,
                    original_filename=version.original_filename,
                    limits=limits,
                    options={},
                    trace_id=str(attempt.id),
                )
            )
            validated = validate_conversion_result(result, limits=limits)
        except ConversionError as exc:
            return self._handle_conversion_error(
                session=session,
                workspace_id=workspace_id,
                attempt=attempt,
                run=run,
                version=version,
                item=item,
                error=exc,
                execution_generation=execution_generation,
            )
        except ValueError:
            raise
        except Exception:
            return self._handle_conversion_error(
                session=session,
                workspace_id=workspace_id,
                attempt=attempt,
                run=run,
                version=version,
                item=item,
                error=ConversionError(
                    "converter_transport_failed",
                    "The converter could not be executed safely.",
                    retryable=True,
                    fallback_allowed=False,
                ),
                execution_generation=execution_generation,
            )

        acquire_write_intent(session)
        lock_workspace_blob_reference_intent(
            session,
            workspace_id,
            allow_deleting=False,
        )
        locked = _lock_conversion_lineage(
            session,
            item_id=item.id,
            version_id=version.id,
            run_id=run.id,
            attempt_id=attempt.id,
        )
        if locked is None:
            return self._execution(attempt, run, "stale")
        item, version, run, current_attempt = locked
        if (
            current_attempt.execution_generation != execution_generation
            or current_attempt.status not in {"queued", "running"}
        ):
            return self._execution(current_attempt, run, "stale")

        try:
            materialized = self._materialize_result(
                workspace_id=workspace_id,
                result=validated,
                limits=limits,
            )
        except ConversionError as exc:
            return self._handle_conversion_error(
                session=session,
                workspace_id=workspace_id,
                attempt=current_attempt,
                run=run,
                version=version,
                item=item,
                error=exc,
                execution_generation=execution_generation,
            )
        except Exception:
            return self._handle_conversion_error(
                session=session,
                workspace_id=workspace_id,
                attempt=current_attempt,
                run=run,
                version=version,
                item=item,
                error=ConversionError(
                    "converter_materialization_failed",
                    "The converter output could not be stored safely.",
                    retryable=True,
                    fallback_allowed=False,
                ),
                execution_generation=execution_generation,
            )

        # Legacy single-UoW dispatch retained for injected/test callers. The
        # production worker uses ``execute_attempt_durable`` so filesystem
        # publication never depends on this Library transaction committing.
        try:
            with session.begin_nested():
                artifact_ids = self._reserve_artifact_quota(
                    session=session,
                    workspace_id=workspace_id,
                    attempt=current_attempt,
                    materialized=materialized,
                )
                installed = self._install_artifacts(
                    session=session,
                    workspace_id=workspace_id,
                    attempt=current_attempt,
                    materialized=materialized,
                )
                persisted_artifacts = self._persist_artifacts(
                    session=session,
                    attempt=current_attempt,
                    version=version,
                    result=validated,
                    installed=installed,
                    artifact_ids=artifact_ids,
                )
        except LibraryLogicalQuotaExceededError:
            return self._handle_conversion_error(
                session=session,
                workspace_id=workspace_id,
                attempt=current_attempt,
                run=run,
                version=version,
                item=item,
                error=ConversionError(
                    "policy_rejected",
                    "The Library derived-media quota was exceeded.",
                    retryable=False,
                    fallback_allowed=False,
                ),
                execution_generation=execution_generation,
            )
        except ConversionError as exc:
            return self._handle_conversion_error(
                session=session,
                workspace_id=workspace_id,
                attempt=current_attempt,
                run=run,
                version=version,
                item=item,
                error=exc,
                execution_generation=execution_generation,
            )
        except Exception:
            return self._handle_conversion_error(
                session=session,
                workspace_id=workspace_id,
                attempt=current_attempt,
                run=run,
                version=version,
                item=item,
                error=ConversionError(
                    "converter_materialization_failed",
                    "The converter output could not be stored safely.",
                    retryable=True,
                    fallback_allowed=False,
                ),
                execution_generation=execution_generation,
            )
        now = utc_now()
        current_attempt.status = "completed"
        current_attempt.started_at = current_attempt.started_at or now
        current_attempt.finished_at = now
        current_attempt.error_code = None
        current_attempt.safe_error_summary = None
        current_attempt.conversion_detail.manual_retry_disposition = None
        current_attempt.conversion_detail.disposition_reason = None
        current_attempt.warnings_json = [
            {"code": warning.code, "message": warning.message}
            for warning in validated.warnings
        ]
        current_attempt.provenance_json = {
            "source_sha256": version.content_hash,
            "source_byte_size": blob.byte_size,
            "detector": dict(version.detection_json or {}),
            "converter_id": current_attempt.provider_id,
            "converter_version": current_attempt.provider_version,
            "converter_spi_version": current_attempt.provider_spi_version,
            "config_hash": current_attempt.config_hash,
            "output_schema_version": current_attempt.output_schema_version,
            "sanitizer_version": SANITIZER_VERSION,
            "completeness": validated.completeness,
            "warning_codes": [warning.code for warning in validated.warnings],
            "result_metadata": dict(validated.metadata),
            "result_locations": [
                {
                    "kind": location.kind,
                    "ordinal": location.ordinal,
                    "label": location.label,
                    "data": dict(location.data),
                }
                for location in validated.locations
            ],
        }
        current_attempt.metrics_json = {
            "artifact_count": len(persisted_artifacts),
            "artifact_bytes": sum(row.byte_size or 0 for row in persisted_artifacts),
        }
        run.status = "completed"
        run.finished_at = now
        selected = self._promote_if_newer_run(
            session=session,
            workspace_id=workspace_id,
            item=item,
            version=version,
            run=run,
            attempt=current_attempt,
            completeness=validated.completeness,
        )
        session.add_all([current_attempt, run, version, item])
        session.flush()
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind="system",
            actor_id="library-converter",
            action="library.conversion.completed",
            target_kind="library_conversion_attempt",
            target_id=current_attempt.id,
            payload={
                "library_item_id": str(item.id),
                "library_item_version_id": str(version.id),
                "converter_id": current_attempt.provider_id,
                "converter_version": current_attempt.provider_version,
                "format_id": version.format_id,
                "selected": selected,
                "completeness": validated.completeness,
            },
        )
        status: Literal["ready", "ready_partial"] = (
            "ready" if validated.completeness == "complete" else "ready_partial"
        )
        return self._execution(
            current_attempt,
            run,
            status,
            selected=selected,
        )

    def execute_attempt_durable(
        self,
        *,
        session_factory: sessionmaker[Session],
        workspace_id: UUID,
        attempt_id: UUID,
        execution_generation: int,
    ) -> ConversionExecution:
        """Convert, durably install, then activate artifacts in separate UoWs.

        Production workers use this path so neither the converter nor Shared
        Media filesystem publication runs while a Library lineage transaction
        is open. A stale generation or post-install quota race can leave an
        unassociated blob, but its install request and physical charge are
        already durable and therefore visible to reconciliation/GC.
        """

        if self.durable_media_installer_factory is None:
            raise RuntimeError("durable media installer is not configured")

        preflight_error: ConversionError | None = None
        preflight_force_terminal = False
        with session_factory() as read_session:
            attempt = get_in_workspace(
                read_session,
                LibraryAttempt,
                workspace_id=workspace_id,
                resource_id=attempt_id,
            )
            if attempt is None or attempt.stage != "conversion":
                raise ValueError("Library conversion attempt was not found")
            run = get_in_workspace(
                read_session,
                LibraryRun,
                workspace_id=workspace_id,
                resource_id=attempt.run_id,
            )
            if run is not None and run.stage != "conversion":
                run = None
            version = get_in_workspace(
                read_session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=attempt.library_item_version_id,
            )
            if run is None or version is None:
                raise ValueError("Library conversion lineage is incomplete")
            item = get_in_workspace(
                read_session,
                LibraryItem,
                workspace_id=workspace_id,
                resource_id=version.library_item_id,
            )
            blob = _resolve_version_source_blob(
                read_session,
                version,
                repository_factory=self.blob_repository_factory,
            )
            if (
                item is None
                or item.workspace_id != workspace_id
                or blob is None
                or blob.reference.workspace_id != workspace_id
            ):
                raise ValueError("Library conversion source association is invalid")
            if attempt.execution_generation != execution_generation:
                return self._execution(attempt, run, "stale")
            if attempt.status == "completed":
                selected = version.selected_conversion_attempt_id == attempt.id
                return self._execution(
                    attempt,
                    run,
                    version.processing_status if selected else "stale",
                    selected=selected,
                )
            if attempt.status not in {"queued", "running"}:
                return self._execution(attempt, run, "stale")

            # Snapshot every fact used after this read-only session closes.
            item_id = item.id
            version_id = version.id
            run_id = run.id
            source_sha256 = version.content_hash
            source_byte_size = blob.byte_size
            source_storage_key = blob.storage_key
            detected_media_type = version.detected_media_type
            source_media_type = blob.media_type
            format_id = version.format_id
            original_filename = version.original_filename
            chain_index = attempt.chain_index
            converter_id = attempt.provider_id
            converter_version = attempt.provider_version
            converter_spi_version = attempt.provider_spi_version
            output_schema_version = run.output_schema_version
            transport_retry_count = attempt.transport_retry_count

            # Validate every persisted execution-policy snapshot before
            # detaching the models or touching converter/storage capabilities.
            try:
                (
                    chain_entry,
                    limits,
                    max_transport_retries,
                ) = self._durable_preflight(
                    attempt=attempt,
                    run=run,
                    version=version,
                    chain_index=chain_index,
                )
            except _PersistedConversionSnapshotError as exc:
                preflight_error = exc
                preflight_force_terminal = True
            except ConversionError as exc:
                preflight_error = exc

        if preflight_error is not None:
            return self._record_durable_conversion_error(
                session_factory=session_factory,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                execution_generation=execution_generation,
                error=preflight_error,
                force_terminal=preflight_force_terminal,
            )

        try:
            descriptor = self.converter_registry.descriptor(converter_id)
            if _provider_snapshot(descriptor) != chain_entry:
                raise ConversionError(
                    "converter_recipe_changed",
                    "The converter recipe no longer matches the scheduled run.",
                    retryable=False,
                    fallback_allowed=False,
                )
            if transport_retry_count > max_transport_retries:
                raise ConversionError(
                    "converter_retry_budget_exhausted",
                    "The converter retry budget was exhausted.",
                    retryable=False,
                    fallback_allowed=False,
                )
            media_type = detected_media_type or source_media_type
            if not format_id or not media_type or not original_filename:
                raise ConversionError(
                    "converter_source_invalid",
                    "The Library source metadata is incomplete.",
                    retryable=False,
                    fallback_allowed=False,
                )
            plugin = self.converter_registry.plugin(converter_id)
            probe = plugin.probe(
                ProbeRequest(
                    format_id=format_id,
                    media_type=media_type,
                    original_filename=original_filename,
                    byte_size=source_byte_size,
                )
            )
            if not probe.supported:
                raise UnsupportedConversionError(
                    "The converter does not support this source."
                )
            handle = self.store_for(workspace_id).open_handle(
                source_storage_key,
                byte_size=source_byte_size,
            )
            result = plugin.convert(
                ConversionRequest(
                    source=handle,
                    source_sha256=source_sha256,
                    byte_size=source_byte_size,
                    format_id=format_id,
                    media_type=media_type,
                    original_filename=original_filename,
                    limits=limits,
                    options={},
                    trace_id=str(attempt_id),
                )
            )
            validated = validate_conversion_result(result, limits=limits)
        except ConversionError as exc:
            return self._record_durable_conversion_error(
                session_factory=session_factory,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                execution_generation=execution_generation,
                error=exc,
            )
        except ValueError:
            raise
        except Exception:
            return self._record_durable_conversion_error(
                session_factory=session_factory,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                execution_generation=execution_generation,
                error=ConversionError(
                    "converter_transport_failed",
                    "The converter could not be executed safely.",
                    retryable=True,
                    fallback_allowed=False,
                ),
            )

        try:
            materialized = self._materialize_result(
                workspace_id=workspace_id,
                result=validated,
                limits=limits,
            )
        except ConversionError as exc:
            return self._record_durable_conversion_error(
                session_factory=session_factory,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                execution_generation=execution_generation,
                error=exc,
            )
        except Exception:
            return self._record_durable_conversion_error(
                session_factory=session_factory,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                execution_generation=execution_generation,
                error=ConversionError(
                    "converter_materialization_failed",
                    "The converter output could not be stored safely.",
                    retryable=True,
                    fallback_allowed=False,
                ),
            )

        if len(materialized) > MAX_DURABLE_CONVERSION_OUTPUTS:
            return self._record_durable_conversion_error(
                session_factory=session_factory,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                execution_generation=execution_generation,
                error=ConversionError(
                    "conversion_output_capacity_exceeded",
                    "The conversion output exceeds the durable install capacity.",
                    retryable=False,
                    fallback_allowed=False,
                ),
            )

        # Fail fast on the exact idempotent derived-byte delta in its own short
        # transaction. The activation UoW below repeats the authoritative
        # reservation after install; a concurrent quota change between the two
        # decisions therefore yields a tracked physical orphan, not a bypass.
        try:
            with session_factory.begin() as quota_session:
                self._ensure_artifact_quota_capacity(
                    session=quota_session,
                    workspace_id=workspace_id,
                    attempt_id=attempt_id,
                    materialized=materialized,
                )
        except LibraryLogicalQuotaExceededError:
            return self._record_durable_conversion_error(
                session_factory=session_factory,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                execution_generation=execution_generation,
                error=ConversionError(
                    "policy_rejected",
                    "The Library derived-media quota was exceeded.",
                    retryable=False,
                    fallback_allowed=False,
                ),
            )

        # Resolve each digest's canonical media type in a closed read
        # transaction. For a new digest, the first batch occurrence becomes
        # canonical for every duplicate that follows, even before its install
        # is visible to a later journal lookup.
        materialized_digests = tuple(
            Sha256Digest(hashlib.sha256(output.content).hexdigest())
            for output in materialized
        )
        canonical_media_type_by_digest: dict[Sha256Digest, str] = {}
        with session_factory() as lookup_session:
            repository = self.blob_repository_factory(lookup_session)
            for output, digest in zip(
                materialized,
                materialized_digests,
                strict=True,
            ):
                if digest in canonical_media_type_by_digest:
                    continue
                existing = repository.find(
                    WorkspaceBlobIdentity(
                        workspace_id,
                        digest,
                    )
                )
                canonical_media_type_by_digest[digest] = (
                    existing.media_type if existing is not None else output.media_type
                )

        installer = self.durable_media_installer_factory(session_factory, workspace_id)
        installed_rows: list[_InstalledArtifact] = []
        try:
            for output, digest in zip(
                materialized,
                materialized_digests,
                strict=True,
            ):
                available = installer.execute(
                    workspace_id=workspace_id,
                    actor_id=attempt_id,
                    session_key=f"library-conversion:{attempt_id}",
                    idempotency_key=(
                        f"library-conversion:{attempt_id}:"
                        f"{output.role}:{output.ordinal}"
                    ),
                    content=output.content,
                    max_bytes=output.max_bytes,
                    canonical_media_type=canonical_media_type_by_digest[digest],
                    format_id=f"derived-{output.role}",
                    detector_id="library-conversion-output",
                    detector_version=converter_version,
                    registry_version=converter_spi_version,
                    validation_schema_version=output_schema_version,
                    metadata={
                        "role": output.role,
                        "ordinal": output.ordinal,
                        "converter_id": converter_id,
                        "converter_version": converter_version,
                        "sanitizer_version": SANITIZER_VERSION,
                    },
                    association_hold=BlobAssociationHold(
                        producer_kind=(
                            BlobAssociationHoldProducerKind.LIBRARY_CONVERSION_ATTEMPT
                        ),
                        producer_id=attempt_id,
                        producer_generation=execution_generation,
                    ),
                )
                if (
                    available.reference.workspace_id != workspace_id
                    or available.sha256 != digest
                    or available.byte_size != len(output.content)
                ):
                    raise RuntimeError(
                        "Shared Media installer changed immutable conversion facts"
                    )
                installed_rows.append(
                    _InstalledArtifact(
                        role=output.role,
                        media_type=output.media_type,
                        ordinal=output.ordinal,
                        available=available,
                        index_text=output.index_text,
                        filename=output.filename,
                    )
                )
        except Exception:
            return self._record_durable_conversion_error(
                session_factory=session_factory,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                execution_generation=execution_generation,
                error=ConversionError(
                    "converter_materialization_failed",
                    "The converter output could not be stored safely.",
                    retryable=True,
                    fallback_allowed=False,
                ),
            )
        installed = tuple(installed_rows)

        with session_factory.begin() as write_session:
            acquire_write_intent(write_session)
            lock_workspace_blob_reference_intent(
                write_session,
                workspace_id,
                allow_deleting=False,
            )
            locked = _lock_conversion_lineage(
                write_session,
                item_id=item_id,
                version_id=version_id,
                run_id=run_id,
                attempt_id=attempt_id,
            )
            if locked is None:
                return ConversionExecution(
                    attempt_id=attempt_id,
                    run_id=run_id,
                    version_id=version_id,
                    status="stale",
                )
            item, version, run, current_attempt = locked
            if (
                current_attempt.execution_generation != execution_generation
                or current_attempt.status not in {"queued", "running"}
            ):
                return self._execution(current_attempt, run, "stale")

            try:
                with write_session.begin_nested():
                    artifact_ids = self._reserve_artifact_quota(
                        session=write_session,
                        workspace_id=workspace_id,
                        attempt=current_attempt,
                        materialized=materialized,
                    )
                    persisted_artifacts = self._persist_artifacts(
                        session=write_session,
                        attempt=current_attempt,
                        version=version,
                        result=validated,
                        installed=installed,
                        artifact_ids=artifact_ids,
                    )
            except LibraryLogicalQuotaExceededError:
                return self._handle_conversion_error(
                    session=write_session,
                    workspace_id=workspace_id,
                    attempt=current_attempt,
                    run=run,
                    version=version,
                    item=item,
                    error=ConversionError(
                        "policy_rejected",
                        "The Library derived-media quota was exceeded.",
                        retryable=False,
                        fallback_allowed=False,
                    ),
                    execution_generation=execution_generation,
                )
            except ConversionError as exc:
                return self._handle_conversion_error(
                    session=write_session,
                    workspace_id=workspace_id,
                    attempt=current_attempt,
                    run=run,
                    version=version,
                    item=item,
                    error=exc,
                    execution_generation=execution_generation,
                )
            except Exception:
                return self._handle_conversion_error(
                    session=write_session,
                    workspace_id=workspace_id,
                    attempt=current_attempt,
                    run=run,
                    version=version,
                    item=item,
                    error=ConversionError(
                        "converter_materialization_failed",
                        "The converter output could not be stored safely.",
                        retryable=True,
                        fallback_allowed=False,
                    ),
                    execution_generation=execution_generation,
                )

            now = utc_now()
            current_attempt.status = "completed"
            current_attempt.started_at = current_attempt.started_at or now
            current_attempt.finished_at = now
            current_attempt.error_code = None
            current_attempt.safe_error_summary = None
            current_attempt.conversion_detail.manual_retry_disposition = None
            current_attempt.conversion_detail.disposition_reason = None
            current_attempt.warnings_json = [
                {"code": warning.code, "message": warning.message}
                for warning in validated.warnings
            ]
            current_attempt.provenance_json = {
                "source_sha256": version.content_hash,
                "source_byte_size": source_byte_size,
                "detector": dict(version.detection_json or {}),
                "converter_id": current_attempt.provider_id,
                "converter_version": current_attempt.provider_version,
                "converter_spi_version": current_attempt.provider_spi_version,
                "config_hash": current_attempt.config_hash,
                "output_schema_version": current_attempt.output_schema_version,
                "sanitizer_version": SANITIZER_VERSION,
                "completeness": validated.completeness,
                "warning_codes": [warning.code for warning in validated.warnings],
                "result_metadata": dict(validated.metadata),
                "result_locations": [
                    {
                        "kind": location.kind,
                        "ordinal": location.ordinal,
                        "label": location.label,
                        "data": dict(location.data),
                    }
                    for location in validated.locations
                ],
            }
            current_attempt.metrics_json = {
                "artifact_count": len(persisted_artifacts),
                "artifact_bytes": sum(
                    row.byte_size or 0 for row in persisted_artifacts
                ),
            }
            run.status = "completed"
            run.finished_at = now
            selected = self._promote_if_newer_run(
                session=write_session,
                workspace_id=workspace_id,
                item=item,
                version=version,
                run=run,
                attempt=current_attempt,
                completeness=validated.completeness,
            )
            write_session.add_all([current_attempt, run, version, item])
            write_session.flush()
            self.audit_service.emit(
                session=write_session,
                workspace_id=workspace_id,
                actor_kind="system",
                actor_id="library-converter",
                action="library.conversion.completed",
                target_kind="library_conversion_attempt",
                target_id=current_attempt.id,
                payload={
                    "library_item_id": str(item.id),
                    "library_item_version_id": str(version.id),
                    "converter_id": current_attempt.provider_id,
                    "converter_version": current_attempt.provider_version,
                    "format_id": version.format_id,
                    "selected": selected,
                    "completeness": validated.completeness,
                },
            )
            status: Literal["ready", "ready_partial"] = (
                "ready" if validated.completeness == "complete" else "ready_partial"
            )
            return self._execution(
                current_attempt,
                run,
                status,
                selected=selected,
            )

    def _record_durable_conversion_error(
        self,
        *,
        session_factory: sessionmaker[Session],
        workspace_id: UUID,
        attempt_id: UUID,
        execution_generation: int,
        error: ConversionError,
        force_terminal: bool = False,
    ) -> ConversionExecution:
        with session_factory.begin() as session:
            attempt = get_in_workspace(
                session,
                LibraryAttempt,
                workspace_id=workspace_id,
                resource_id=attempt_id,
            )
            if attempt is None or attempt.stage != "conversion":
                raise ValueError("Library conversion attempt was not found")
            run = get_in_workspace(
                session,
                LibraryRun,
                workspace_id=workspace_id,
                resource_id=attempt.run_id,
            )
            if run is not None and run.stage != "conversion":
                run = None
            version = get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=attempt.library_item_version_id,
            )
            item = (
                get_in_workspace(
                    session,
                    LibraryItem,
                    workspace_id=workspace_id,
                    resource_id=version.library_item_id,
                )
                if version is not None
                else None
            )
            if run is None or version is None or item is None:
                raise ValueError("Library conversion lineage is incomplete")
            return self._handle_conversion_error(
                session=session,
                workspace_id=workspace_id,
                attempt=attempt,
                run=run,
                version=version,
                item=item,
                error=error,
                execution_generation=execution_generation,
                force_terminal=force_terminal,
            )

    def _ensure_artifact_quota_capacity(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt_id: UUID,
        materialized: tuple[_MaterializedArtifact, ...],
    ) -> None:
        artifact_sizes: dict[UUID, int] = {}
        for output in materialized:
            existing_artifact = session.scalar(
                select(LibraryArtifact).where(
                    LibraryArtifact.attempt_id == attempt_id,
                    LibraryArtifact.stage == "conversion",
                    LibraryArtifact.role == output.role,
                    LibraryArtifact.ordinal == output.ordinal,
                )
            )
            artifact_id = (
                existing_artifact.id
                if existing_artifact is not None
                else uuid5(
                    NAMESPACE_URL,
                    f"library-artifact:{attempt_id}:{output.role}:{output.ordinal}",
                )
            )
            artifact_sizes[artifact_id] = len(output.content)
        charged_ids = set(
            session.scalars(
                select(LibraryLogicalCharge.association_id).where(
                    LibraryLogicalCharge.workspace_id == workspace_id,
                    LibraryLogicalCharge.quota_kind == "derived",
                    LibraryLogicalCharge.association_id.in_(artifact_sizes),
                    LibraryLogicalCharge.state == "charged",
                )
            )
        )
        self.logical_quota_factory(session).ensure_capacity(
            workspace_id,
            quota_kind="derived",
            additional_bytes=sum(
                byte_size
                for artifact_id, byte_size in artifact_sizes.items()
                if artifact_id not in charged_ids
            ),
        )

    def mark_attempt_running(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt_id: UUID,
        execution_generation: int,
    ) -> bool:
        """Commit the externally observable conversion start state.

        Production workers call this in a short transaction before invoking a
        converter in their separate business transaction.  Keeping the marker
        out of ``execute_attempt`` avoids holding a SQLite writer lock (or a
        PostgreSQL lineage lock) while untrusted/external converter work runs.
        """

        attempt = get_in_workspace(
            session,
            LibraryAttempt,
            workspace_id=workspace_id,
            resource_id=attempt_id,
        )
        if attempt is None or attempt.stage != "conversion":
            raise ValueError("Library conversion attempt was not found")
        run = get_in_workspace(
            session,
            LibraryRun,
            workspace_id=workspace_id,
            resource_id=attempt.run_id,
        )
        if run is not None and run.stage != "conversion":
            run = None
        version = get_in_workspace(
            session,
            LibraryItemVersion,
            workspace_id=workspace_id,
            resource_id=attempt.library_item_version_id,
        )
        if run is None or version is None:
            raise ValueError("Library conversion lineage is incomplete")

        acquire_write_intent(session)
        locked = _lock_conversion_lineage(
            session,
            item_id=version.library_item_id,
            version_id=version.id,
            run_id=run.id,
            attempt_id=attempt.id,
        )
        if locked is None:
            raise ValueError("Library conversion lineage is incomplete")
        item, version, run, attempt = locked
        if item.workspace_id != workspace_id:
            raise ValueError("Library conversion source association is invalid")
        if (
            attempt.execution_generation != execution_generation
            or attempt.status not in {"queued", "running"}
        ):
            return False

        selected = _selected_run_context(
            session,
            workspace_id=workspace_id,
            version=version,
        )
        superseded = (
            selected is not None
            and selected[0].id != attempt.id
            and selected[1].run_seq >= run.run_seq
        )
        now = utc_now()
        attempt.status = "running"
        attempt.started_at = attempt.started_at or now
        if not superseded:
            run.status = "running"
            version.processing_status = "converting"
        session.add_all([attempt, run, version])
        return True

    def mark_terminal_failure(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt_id: UUID,
        execution_generation: int,
        manual_retryable: bool,
        safe_error_code: str = "worker_failed",
    ) -> None:
        """Reconcile a terminal queue failure without exposing worker details.

        ``manual_retryable`` is the worker queue's failure-time classification:
        deterministic terminal dispatch errors pass False, while a generic or
        lease failure that exhausted its bounded queue budget passes True.
        """

        attempt = get_in_workspace(
            session,
            LibraryAttempt,
            workspace_id=workspace_id,
            resource_id=attempt_id,
        )
        if attempt is None or attempt.stage != "conversion":
            return
        run = get_in_workspace(
            session,
            LibraryRun,
            workspace_id=workspace_id,
            resource_id=attempt.run_id,
        )
        if run is not None and run.stage != "conversion":
            run = None
        version = get_in_workspace(
            session,
            LibraryItemVersion,
            workspace_id=workspace_id,
            resource_id=attempt.library_item_version_id,
        )
        if run is None or version is None:
            return
        acquire_write_intent(session)
        locked = _lock_conversion_lineage(
            session,
            item_id=version.library_item_id,
            version_id=version.id,
            run_id=run.id,
            attempt_id=attempt.id,
        )
        if locked is None:
            return
        item, version, run, attempt = locked
        if (
            attempt.execution_generation != execution_generation
            or attempt.status not in {"queued", "running"}
        ):
            return
        selected = _selected_run_context(
            session,
            workspace_id=workspace_id,
            version=version,
        )
        superseded = (
            selected is not None
            and selected[0].id != attempt.id
            and selected[1].run_seq >= run.run_seq
        )
        attempt.status = "failed"
        attempt.error_code = safe_error_code
        attempt.safe_error_summary = "The converter worker failed."
        retry_decision = decide_conversion_manual_retry(
            error_retryable=manual_retryable,
            policy_stops=is_fail_closed_conversion_error(safe_error_code),
        )
        attempt.conversion_detail.manual_retry_disposition = retry_decision.disposition
        attempt.conversion_detail.disposition_reason = retry_decision.reason
        attempt.finished_at = utc_now()
        if not superseded or selected[1].id != run.id:
            run.status = "failed"
            run.finished_at = utc_now()
        if not superseded:
            version.processing_status = "failed"
        session.add_all([attempt, run, version])

    def _existing_schedule(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        run: LibraryRun,
    ) -> ScheduledConversion:
        attempt = session.scalar(
            select(LibraryAttempt)
            .where(
                LibraryAttempt.run_id == run.id,
                LibraryAttempt.stage == "conversion",
            )
            .order_by(LibraryAttempt.chain_index.desc())
            .limit(1)
        )
        if attempt is None or attempt.status == "completed":
            return ScheduledConversion(run=run, attempt=attempt, job=None)
        job = self._enqueue_attempt(
            session=session,
            workspace_id=workspace_id,
            attempt=attempt,
        )
        return ScheduledConversion(run=run, attempt=attempt, job=job)

    def _new_attempt(
        self,
        *,
        run: LibraryRun,
        version: LibraryItemVersion,
        chain_index: int,
        parent_attempt_id: UUID | None,
    ) -> LibraryAttempt:
        entry = self._chain_entry(run, chain_index)
        attempt = LibraryAttempt(
            workspace_id=version.workspace_id,
            run_id=run.id,
            library_item_version_id=version.id,
            stage="conversion",
            provider_id=str(entry["converter_id"]),
            provider_version=str(entry["converter_version"]),
            provider_spi_version=str(entry["spi_version"]),
            config_hash=_stable_hash(
                {
                    "provider": entry,
                    "budget": run.conversion_detail.per_provider_budget_json,
                    "policy_revision": run.policy_revision,
                }
            ),
            input_sha256=version.content_hash,
            output_schema_version=run.output_schema_version,
            status="queued",
            chain_index=chain_index,
            transport_retry_count=0,
            execution_generation=1,
            parent_attempt_id=parent_attempt_id,
            provenance_json={},
            metrics_json={},
            warnings_json=[],
        )
        attempt.conversion_detail = LibraryAttemptConversionDetail(
            manual_retry_disposition=None,
            disposition_reason=None,
        )
        return attempt

    def _enqueue_attempt(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt: LibraryAttempt,
    ) -> Job:
        return self.job_queue.enqueue(
            session=session,
            workspace_id=workspace_id,
            job_type=LIBRARY_CONVERSION_JOB_TYPE,
            trigger_type="library_conversion",
            idempotency_key=f"{attempt.id}:{attempt.execution_generation}",
            target_ref={
                "library_conversion_attempt_id": str(attempt.id),
                "library_conversion_run_id": str(attempt.run_id),
                "library_item_version_id": str(attempt.library_item_version_id),
                "execution_generation": attempt.execution_generation,
            },
        )

    @staticmethod
    def _chain_entry(run: LibraryRun, chain_index: int) -> dict[str, Any]:
        chain = _validated_provider_chain(run)
        if chain_index < 0 or chain_index >= len(chain):
            raise ValueError("Library conversion provider snapshot is invalid")
        return dict(chain[chain_index])

    def _durable_preflight(
        self,
        *,
        attempt: LibraryAttempt,
        run: LibraryRun,
        version: LibraryItemVersion,
        chain_index: int,
    ) -> tuple[dict[str, Any], ConversionLimits, int]:
        """Validate persisted worker policy without widening ValueError handling."""

        try:
            chain_entry = self._chain_entry(run, chain_index)
        except ValueError as exc:
            raise _PersistedConversionSnapshotError(
                "conversion_provider_snapshot_invalid",
                "The persisted converter provider snapshot is invalid.",
            ) from exc
        try:
            limits = _limits_from_run(run)
        except ValueError as exc:
            raise _PersistedConversionSnapshotError(
                "conversion_limit_snapshot_invalid",
                "The persisted converter limit snapshot is invalid.",
            ) from exc
        try:
            max_transport_retries = _max_transport_retries(run)
        except ValueError as exc:
            raise _PersistedConversionSnapshotError(
                "conversion_retry_snapshot_invalid",
                "The persisted converter retry snapshot is invalid.",
            ) from exc
        try:
            _validate_fallback_snapshot(run)
        except ValueError as exc:
            raise _PersistedConversionSnapshotError(
                "conversion_fallback_snapshot_invalid",
                "The persisted converter fallback snapshot is invalid.",
            ) from exc
        try:
            _total_budget_max_wall_seconds(run)
        except ValueError as exc:
            raise _PersistedConversionSnapshotError(
                "conversion_total_budget_invalid",
                "The persisted conversion total budget is invalid.",
            ) from exc

        _validate_attempt_recipe(
            attempt=attempt,
            run=run,
            version=version,
            chain_entry=chain_entry,
        )
        _validate_total_budget(run)
        return chain_entry, limits, max_transport_retries

    def _materialize_result(
        self,
        *,
        workspace_id: UUID,
        result,
        limits: ConversionLimits,
    ) -> tuple[_MaterializedArtifact, ...]:
        staged: list[_StagedArtifact] = []
        store = self.store_for(workspace_id)
        try:
            if result.markdown is not None:
                staged.append(
                    _StagedArtifact(
                        role="markdown",
                        media_type="text/markdown",
                        ordinal=0,
                        staged=store.stage_chunks(
                            (result.markdown.encode("utf-8"),),
                            workspace_id=workspace_id,
                            max_bytes=limits.max_output_bytes,
                        ),
                        max_bytes=limits.max_output_bytes,
                        index_text=result.markdown,
                    )
                )
            for asset in result.assets:
                staged.append(
                    _StagedArtifact(
                        role=asset.role,
                        media_type=asset.media_type,
                        ordinal=asset.ordinal,
                        staged=store.stage_chunks(
                            (asset.content,),
                            workspace_id=workspace_id,
                            max_bytes=limits.max_asset_bytes,
                        ),
                        max_bytes=limits.max_asset_bytes,
                        filename=asset.filename,
                    )
                )
            if (
                result.markdown is None
                and not result.assets
                and (result.metadata or result.locations)
            ):
                metadata_payload = json.dumps(
                    {
                        "metadata": dict(result.metadata),
                        "locations": [
                            {
                                "kind": location.kind,
                                "ordinal": location.ordinal,
                                "label": location.label,
                                "data": dict(location.data),
                            }
                            for location in result.locations
                        ],
                        "completeness": result.completeness,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
                if len(metadata_payload) > limits.max_asset_bytes:
                    raise ConversionError(
                        "converter_asset_bytes_exceeded",
                        "The converter metadata exceeds the configured limit.",
                        retryable=False,
                        fallback_allowed=True,
                    )
                staged.append(
                    _StagedArtifact(
                        role="metadata",
                        media_type="application/json",
                        ordinal=0,
                        staged=store.stage_chunks(
                            (metadata_payload,),
                            workspace_id=workspace_id,
                            max_bytes=limits.max_asset_bytes,
                        ),
                        max_bytes=limits.max_asset_bytes,
                        filename="metadata.json",
                    )
                )
            return tuple(
                _MaterializedArtifact(
                    role=row.role,
                    media_type=row.media_type,
                    ordinal=row.ordinal,
                    content=row.staged.path.read_bytes(),
                    max_bytes=row.max_bytes,
                    index_text=row.index_text,
                    filename=row.filename,
                )
                for row in staged
            )
        finally:
            for row in staged:
                row.staged.cleanup()

    def _install_artifacts(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt: LibraryAttempt,
        materialized: tuple[_MaterializedArtifact, ...],
    ) -> tuple[_InstalledArtifact, ...]:
        installer = self.media_installer_factory(session, workspace_id)
        repository = self.blob_repository_factory(session)
        installed: list[_InstalledArtifact] = []
        for output in materialized:
            digest = Sha256Digest(hashlib.sha256(output.content).hexdigest())
            existing = repository.find(WorkspaceBlobIdentity(workspace_id, digest))
            canonical_media_type = (
                existing.media_type if existing is not None else output.media_type
            )
            available = installer.execute(
                workspace_id=workspace_id,
                actor_id=attempt.id,
                session_key=f"library-conversion:{attempt.id}",
                idempotency_key=(
                    f"library-conversion:{attempt.id}:{output.role}:{output.ordinal}"
                ),
                content=output.content,
                max_bytes=output.max_bytes,
                canonical_media_type=canonical_media_type,
                format_id=f"derived-{output.role}",
                detector_id="library-conversion-output",
                detector_version=attempt.provider_version,
                registry_version=attempt.provider_spi_version,
                validation_schema_version=attempt.output_schema_version,
                metadata={
                    "role": output.role,
                    "ordinal": output.ordinal,
                    "converter_id": attempt.provider_id,
                    "converter_version": attempt.provider_version,
                    "sanitizer_version": SANITIZER_VERSION,
                },
            )
            if (
                available.reference.workspace_id != workspace_id
                or available.sha256 != digest
                or available.byte_size != len(output.content)
            ):
                raise RuntimeError(
                    "Shared Media installer changed immutable conversion facts"
                )
            installed.append(
                _InstalledArtifact(
                    role=output.role,
                    media_type=output.media_type,
                    ordinal=output.ordinal,
                    available=available,
                    index_text=output.index_text,
                    filename=output.filename,
                )
            )
        return tuple(installed)

    def _persist_artifacts(
        self,
        *,
        session: Session,
        attempt: LibraryAttempt,
        version: LibraryItemVersion,
        result,
        installed: tuple[_InstalledArtifact, ...],
        artifact_ids: dict[tuple[str, int], UUID],
    ) -> list[LibraryArtifact]:
        locations = [
            {
                "kind": location.kind,
                "ordinal": location.ordinal,
                "label": location.label,
                "data": dict(location.data),
            }
            for location in result.locations
        ]
        planned: list[tuple[LibraryArtifact, bool]] = []
        for output in installed:
            blob = output.available
            existing = session.scalar(
                select(LibraryArtifact).where(
                    LibraryArtifact.attempt_id == attempt.id,
                    LibraryArtifact.stage == "conversion",
                    LibraryArtifact.role == output.role,
                    LibraryArtifact.ordinal == output.ordinal,
                )
            )
            if existing is not None:
                if (
                    existing.content_sha256 != blob.sha256.value
                    or existing.blob_workspace_id != blob.reference.workspace_id
                    or existing.blob_id != blob.reference.blob_id
                    or existing.blob_epoch != blob.reference.epoch.value
                    or existing.byte_size != blob.byte_size
                ):
                    raise ValueError("conversion artifact identity conflict")
                planned.append((existing, False))
                continue
            artifact = LibraryArtifact(
                id=artifact_ids[(output.role, output.ordinal)],
                workspace_id=version.workspace_id,
                library_item_version_id=version.id,
                attempt_id=attempt.id,
                stage="conversion",
                blob_workspace_id=blob.reference.workspace_id,
                blob_id=blob.reference.blob_id,
                blob_epoch=blob.reference.epoch.value,
                role=output.role,
                kind=_artifact_kind_for_media_type(output.media_type),
                media_type=output.media_type,
                content_sha256=blob.sha256.value,
                byte_size=blob.byte_size,
                ordinal=output.ordinal,
            )
            artifact.conversion_detail = LibraryArtifactConversionDetail(
                is_complete=result.completeness == "complete",
                sanitizer_version=SANITIZER_VERSION,
                extraction_method=attempt.provider_id,
                index_text=output.index_text,
                location_model=(
                    {
                        "metadata": dict(result.metadata),
                        "locations": locations,
                    }
                    if output.role == "markdown"
                    else {"filename": output.filename}
                    if output.filename
                    else {}
                ),
                quality_score=result.quality_score,
            )
            planned.append((artifact, True))
        persisted: list[LibraryArtifact] = []
        blob_references = SqlAlchemyBlobReferenceLedger(session)
        for artifact, is_new in planned:
            if (
                artifact.blob_workspace_id is None
                or artifact.blob_id is None
                or artifact.blob_epoch is None
            ):
                raise RuntimeError("conversion artifact blob reference is incomplete")
            blob_references.register(
                owner_kind=BlobReferenceOwnerKind.LIBRARY_ARTIFACT,
                owner_id=artifact.id,
                blob=BlobReference(
                    workspace_id=artifact.blob_workspace_id,
                    blob_id=artifact.blob_id,
                    epoch=BlobEpoch(artifact.blob_epoch),
                ),
            )
            if is_new:
                session.add(artifact)
            persisted.append(artifact)
        session.flush()
        return persisted

    def _reserve_artifact_quota(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt: LibraryAttempt,
        materialized: tuple[_MaterializedArtifact, ...],
    ) -> dict[tuple[str, int], UUID]:
        artifact_ids: dict[tuple[str, int], UUID] = {}
        associations: dict[UUID, int] = {}
        for output in materialized:
            key = (output.role, output.ordinal)
            existing = session.scalar(
                select(LibraryArtifact).where(
                    LibraryArtifact.attempt_id == attempt.id,
                    LibraryArtifact.stage == "conversion",
                    LibraryArtifact.role == output.role,
                    LibraryArtifact.ordinal == output.ordinal,
                )
            )
            artifact_id = (
                existing.id
                if existing is not None
                else uuid5(
                    NAMESPACE_URL,
                    f"library-artifact:{attempt.id}:{output.role}:{output.ordinal}",
                )
            )
            artifact_ids[key] = artifact_id
            associations[artifact_id] = len(output.content)
        self.logical_quota_factory(session).reserve(
            workspace_id,
            quota_kind="derived",
            associations=associations,
        )
        return artifact_ids

    def _promote_if_newer_run(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        item: LibraryItem,
        version: LibraryItemVersion,
        run: LibraryRun,
        attempt: LibraryAttempt,
        completeness: str,
    ) -> bool:
        selected_run_seq = 0
        if version.selected_conversion_attempt_id is not None:
            selected_attempt = get_in_workspace(
                session,
                LibraryAttempt,
                workspace_id=workspace_id,
                resource_id=version.selected_conversion_attempt_id,
            )
            if selected_attempt is not None:
                selected_run = get_in_workspace(
                    session,
                    LibraryRun,
                    workspace_id=workspace_id,
                    resource_id=selected_attempt.run_id,
                )
                selected_run_seq = selected_run.run_seq if selected_run else 0
        if selected_run_seq > run.run_seq:
            return False

        if (
            version.selected_conversion_attempt_id != attempt.id
            and version.selected_semantic_overlay_id is not None
        ):
            from backend.services.library_semantic_overlay_service import (
                invalidate_selected_library_semantic_overlay,
            )

            invalidate_selected_library_semantic_overlay(
                session,
                version=version,
                reason="selected_conversion_attempt_changed",
            )
        version.selected_conversion_attempt_id = attempt.id
        version.processing_status = (
            "ready" if completeness == "complete" else "ready_partial"
        )
        if self.preview_orchestrator is not None:
            self.preview_orchestrator.invalidate_for_conversion_promotion(
                session=session,
                item=item,
                version=version,
                conversion_attempt_id=attempt.id,
            )
        current_ready = (
            get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=item.current_ready_version_id,
            )
            if item.current_ready_version_id is not None
            else None
        )
        promoted_item_projection = (
            current_ready is None or version.version_no >= current_ready.version_no
        )
        if promoted_item_projection:
            item.current_ready_version_id = version.id
            from backend.services.indexing.fts5 import sync_library_doc_sources

            sync_library_doc_sources(session, item)
            # A source-version fence cannot distinguish two selected attempts
            # for the same version. Remove the old DB-native semantic
            # generation in the promotion transaction so retrieval falls back
            # to the newly swapped lexical document until an embed job writes
            # chunks for the new canonical attempt.
            from backend.services.indexing.db_native import remove_document_chunks

            remove_document_chunks(
                session,
                document_kind="library_item",
                document_id=item.id,
            )
            outbox = session.scalar(
                select(LibraryProjectionOutbox).where(
                    LibraryProjectionOutbox.conversion_attempt_id == attempt.id,
                    LibraryProjectionOutbox.projection_kind == "fts",
                    LibraryProjectionOutbox.generation == attempt.execution_generation,
                )
            )
            if outbox is None:
                session.add(
                    LibraryProjectionOutbox(
                        workspace_id=version.workspace_id,
                        library_item_version_id=version.id,
                        conversion_attempt_id=attempt.id,
                        projection_kind="fts",
                        generation=attempt.execution_generation,
                        status="completed",
                        idempotency_key=(
                            f"fts:{attempt.id}:{attempt.execution_generation}"
                        ),
                        retry_count=0,
                        completed_at=utc_now(),
                    )
                )
            from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES

            if item.lifecycle_status not in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES:
                self.job_queue.enqueue(
                    session=session,
                    workspace_id=item.workspace_id,
                    job_type="embed",
                    trigger_type="library_conversion",
                    idempotency_key=f"library-conversion:{attempt.id}",
                    target_ref={
                        "kind": "library_item",
                        "id": str(item.id),
                        "library_item_version_id": str(version.id),
                        "conversion_attempt_id": str(attempt.id),
                        "execution_generation": attempt.execution_generation,
                    },
                )
        return True

    def _handle_conversion_error(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt: LibraryAttempt,
        run: LibraryRun,
        version: LibraryItemVersion,
        item: LibraryItem,
        error: ConversionError,
        execution_generation: int,
        force_terminal: bool = False,
    ) -> ConversionExecution:
        acquire_write_intent(session)
        locked = _lock_conversion_lineage(
            session,
            item_id=item.id,
            version_id=version.id,
            run_id=run.id,
            attempt_id=attempt.id,
        )
        if locked is None:
            return self._execution(attempt, run, "stale")
        item, version, run, attempt = locked
        if (
            attempt.execution_generation != execution_generation
            or attempt.status not in {"queued", "running"}
        ):
            return self._execution(attempt, run, "stale")
        now = utc_now()
        if force_terminal:
            fallback_action = "stop"
            policy_stops = True
            max_transport_retries = 0
        else:
            fallback_action = _fallback_action(run, error.code)
            policy_stops = fallback_action == "stop" or is_fail_closed_conversion_error(
                error.code
            )
            max_transport_retries = _max_transport_retries(run)
        retry_decision = decide_conversion_manual_retry(
            error_retryable=error.retryable,
            policy_stops=policy_stops,
        )
        selected = _selected_run_context(
            session,
            workspace_id=workspace_id,
            version=version,
        )
        if (
            selected is not None
            and selected[0].id != attempt.id
            and selected[1].run_seq >= run.run_seq
        ):
            attempt.status = (
                "timed_out" if is_conversion_timeout_error(error.code) else "failed"
            )
            attempt.error_code = error.code
            attempt.safe_error_summary = error.safe_message
            attempt.conversion_detail.manual_retry_disposition = (
                retry_decision.disposition
            )
            attempt.conversion_detail.disposition_reason = retry_decision.reason
            attempt.finished_at = now
            if selected[1].id != run.id:
                run.status = "timed_out" if attempt.status == "timed_out" else "failed"
                run.finished_at = now
            session.add_all([attempt, run])
            return self._execution(attempt, run, "stale")
        attempt.started_at = attempt.started_at or now
        attempt.error_code = error.code
        attempt.safe_error_summary = error.safe_message
        if (
            not policy_stops
            and error.retryable
            and attempt.transport_retry_count < max_transport_retries
        ):
            attempt.transport_retry_count += 1
            attempt.execution_generation += 1
            attempt.status = "queued"
            attempt.conversion_detail.manual_retry_disposition = None
            attempt.conversion_detail.disposition_reason = None
            run.status = "queued"
            version.processing_status = "queued"
            session.add_all([attempt, run, version])
            session.flush()
            self._enqueue_attempt(
                session=session,
                workspace_id=workspace_id,
                attempt=attempt,
            )
            return self._execution(attempt, run, "requeued")

        attempt.status = (
            "timed_out" if is_conversion_timeout_error(error.code) else "failed"
        )
        attempt.conversion_detail.manual_retry_disposition = (
            retry_decision.disposition
        )
        attempt.conversion_detail.disposition_reason = retry_decision.reason
        attempt.finished_at = now
        next_index = attempt.chain_index + 1
        if (
            not policy_stops
            and error.fallback_allowed
            and fallback_action in {"next_provider", "next_provider_if_allowed"}
            and next_index < len(run.provider_chain_json)
        ):
            next_attempt = self._new_attempt(
                run=run,
                version=version,
                chain_index=next_index,
                parent_attempt_id=attempt.id,
            )
            session.add(next_attempt)
            run.status = "queued"
            version.processing_status = "queued"
            session.add_all([attempt, run, version])
            session.flush()
            self._enqueue_attempt(
                session=session,
                workspace_id=workspace_id,
                attempt=next_attempt,
            )
            return self._execution(
                attempt,
                run,
                "fallback_queued",
                next_attempt_id=next_attempt.id,
            )

        run.status = "timed_out" if attempt.status == "timed_out" else "failed"
        run.finished_at = now
        version.processing_status = (
            "timed_out" if attempt.status == "timed_out" else "failed"
        )
        session.add_all([attempt, run, version, item])
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind="system",
            actor_id="library-converter",
            action="library.conversion.failed",
            target_kind="library_conversion_attempt",
            target_id=attempt.id,
            payload={
                "library_item_id": str(item.id),
                "library_item_version_id": str(version.id),
                "converter_id": attempt.provider_id,
                "converter_version": attempt.provider_version,
                "format_id": version.format_id,
                "error_code": error.code,
            },
        )
        return self._execution(
            attempt,
            run,
            "timed_out" if attempt.status == "timed_out" else "failed",
        )

    @staticmethod
    def _execution(
        attempt: LibraryAttempt,
        run: LibraryRun,
        status: str,
        *,
        selected: bool = False,
        next_attempt_id: UUID | None = None,
    ) -> ConversionExecution:
        allowed = {
            "ready",
            "ready_partial",
            "requeued",
            "fallback_queued",
            "failed",
            "timed_out",
            "stale",
        }
        normalized = status if status in allowed else "stale"
        return ConversionExecution(
            attempt_id=attempt.id,
            run_id=run.id,
            version_id=attempt.library_item_version_id,
            status=normalized,  # type: ignore[arg-type]
            selected=selected,
            next_attempt_id=next_attempt_id,
        )


def get_or_create_library_blob(
    session: Session,
    *,
    repository_factory: LibraryBlobRepositoryFactory,
    workspace_id: UUID,
    stored: StoredLibraryBlob,
    media_type: str | None,
    format_id: str = "binary",
    detector_id: str = "library-admission",
    metadata: dict[str, str | int | bool] | None = None,
) -> LibraryBlobRecord:
    if stored.workspace_id != workspace_id:
        raise ValueError("Library stored blob workspace conflict")
    candidate = LibraryBlobCandidate(
        workspace_id=workspace_id,
        sha256=Sha256Digest(stored.sha256),
        byte_size=stored.byte_size,
        storage_key=stored.storage_key,
        media_type=media_type or "application/octet-stream",
        format_id=format_id,
        detector_id=detector_id,
        metadata=metadata or {},
    )
    return RegisterLibraryBlob().execute(
        repository_factory(session),
        candidate,
    )


def _resolve_version_source_blob(
    session: Session,
    version: LibraryItemVersion,
    *,
    repository_factory: LibraryBlobRepositoryFactory,
) -> LibraryBlobRecord | None:
    if (
        version.source_blob_workspace_id is None
        or version.source_blob_id is None
        or version.source_blob_epoch is None
    ):
        return None
    return repository_factory(session).get(
        BlobReference(
            workspace_id=version.source_blob_workspace_id,
            blob_id=version.source_blob_id,
            epoch=BlobEpoch(version.source_blob_epoch),
        )
    )


def _lock_conversion_lineage(
    session: Session,
    *,
    item_id: UUID,
    version_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
) -> (
    tuple[
        LibraryItem,
        LibraryItemVersion,
        LibraryRun,
        LibraryAttempt,
    ]
    | None
):
    """Lock promotion lineage in one global order and refresh stale ORM state."""

    item = session.scalar(
        select(LibraryItem)
        .where(LibraryItem.id == item_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    version = session.scalar(
        select(LibraryItemVersion)
        .where(LibraryItemVersion.id == version_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    run = session.scalar(
        select(LibraryRun)
        .where(LibraryRun.id == run_id, LibraryRun.stage == "conversion")
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    attempt = session.scalar(
        select(LibraryAttempt)
        .where(
            LibraryAttempt.id == attempt_id,
            LibraryAttempt.stage == "conversion",
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        item is None
        or version is None
        or run is None
        or attempt is None
        or version.library_item_id != item.id
        or run.library_item_version_id != version.id
        or attempt.library_item_version_id != version.id
        or attempt.run_id != run.id
    ):
        return None
    return item, version, run, attempt


def _selected_run_context(
    session: Session,
    *,
    workspace_id: UUID,
    version: LibraryItemVersion,
) -> tuple[LibraryAttempt, LibraryRun] | None:
    if version.selected_conversion_attempt_id is None:
        return None
    attempt = get_in_workspace(
        session,
        LibraryAttempt,
        workspace_id=workspace_id,
        resource_id=version.selected_conversion_attempt_id,
    )
    if attempt is None or attempt.stage != "conversion":
        return None
    run = get_in_workspace(
        session,
        LibraryRun,
        workspace_id=workspace_id,
        resource_id=attempt.run_id,
    )
    if run is None or run.stage != "conversion":
        return None
    return attempt, run


def _validated_provider_chain(run: LibraryRun) -> list[dict[str, Any]]:
    chain = run.provider_chain_json
    if not isinstance(chain, list) or not chain:
        raise ValueError("Library conversion provider snapshot is invalid")
    converter_ids: set[str] = set()
    for entry in chain:
        if not isinstance(entry, dict) or frozenset(entry) != _PROVIDER_SNAPSHOT_KEYS:
            raise ValueError("Library conversion provider snapshot is invalid")
        if any(
            type(entry.get(field)) is not str or not entry[field]
            for field in _PROVIDER_REQUIRED_STRING_FIELDS
        ):
            raise ValueError("Library conversion provider snapshot is invalid")
        if any(
            entry.get(field) is not None and type(entry[field]) is not str
            for field in _PROVIDER_OPTIONAL_STRING_FIELDS
        ):
            raise ValueError("Library conversion provider snapshot is invalid")
        if (
            isinstance(entry.get("priority"), bool)
            or not isinstance(entry.get("priority"), int)
            or type(entry.get("network_required")) is not bool
            or type(entry.get("deterministic")) is not bool
            or (
                entry.get("max_input_bytes") is not None
                and (
                    isinstance(entry["max_input_bytes"], bool)
                    or not isinstance(entry["max_input_bytes"], int)
                    or entry["max_input_bytes"] <= 0
                )
            )
            or entry.get("execution_mode") not in {"builtin", "subprocess", "sidecar"}
            or entry.get("output_schema_version") != run.output_schema_version
        ):
            raise ValueError("Library conversion provider snapshot is invalid")
        converter_id = entry["converter_id"]
        if converter_id in converter_ids:
            raise ValueError("Library conversion provider snapshot is invalid")
        converter_ids.add(converter_id)
    return chain


def _limits_from_run(run: LibraryRun) -> ConversionLimits:
    budget = run.conversion_detail.per_provider_budget_json
    raw = budget.get("limits") if isinstance(budget, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("Library conversion limit snapshot is invalid")
    try:
        return ConversionLimits(**raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Library conversion limit snapshot is invalid") from exc


def _validate_fallback_snapshot(run: LibraryRun) -> dict[str, str]:
    matrix = run.conversion_detail.error_fallback_matrix_json
    if not isinstance(matrix, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or value not in _FALLBACK_ACTIONS
        for key, value in matrix.items()
    ):
        raise ValueError("Library conversion fallback snapshot is invalid")
    return matrix


def _fallback_action(run: LibraryRun, error_code: str) -> str:
    matrix = _validate_fallback_snapshot(run)
    action = matrix.get(error_code, matrix.get("default", "stop"))
    return action


def _max_transport_retries(run: LibraryRun) -> int:
    budget = run.conversion_detail.per_provider_budget_json
    value = budget.get("max_transport_retries") if isinstance(budget, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
        raise ValueError("Library conversion retry snapshot is invalid")
    return value


def _total_budget_max_wall_seconds(run: LibraryRun) -> float:
    budget = run.conversion_detail.total_budget_json
    if not isinstance(budget, dict):
        raise ValueError("Library conversion total budget snapshot is invalid")
    max_attempts = budget.get("max_provider_attempts")
    max_wall_seconds = budget.get("max_wall_seconds")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts <= 0
        or max_attempts != len(run.provider_chain_json)
        or isinstance(max_wall_seconds, bool)
        or not isinstance(max_wall_seconds, (int, float))
        or not math.isfinite(max_wall_seconds)
        or max_wall_seconds <= 0
    ):
        raise ValueError("Library conversion total budget snapshot is invalid")
    return float(max_wall_seconds)


def _validate_total_budget(run: LibraryRun) -> None:
    max_wall_seconds = _total_budget_max_wall_seconds(run)
    elapsed = (utc_now() - run.created_at).total_seconds()
    if elapsed > max_wall_seconds:
        raise ConversionError(
            "conversion_total_timeout",
            "The conversion run exceeded its total time budget.",
            retryable=False,
            fallback_allowed=False,
        )


def _provider_snapshot(descriptor) -> dict[str, Any]:
    return {
        "converter_id": descriptor.converter_id,
        "converter_version": descriptor.converter_version,
        "spi_version": descriptor.spi_version,
        "priority": descriptor.priority,
        "execution_mode": descriptor.execution_mode,
        "network_required": descriptor.network_required,
        "deterministic": descriptor.deterministic,
        "license_expression": descriptor.license_expression,
        "distribution_digest": descriptor.distribution_digest,
        "sbom_sha256": descriptor.sbom_sha256,
        "signature_identity": descriptor.signature_identity,
        "sandbox_profile": descriptor.sandbox_profile,
        "max_input_bytes": descriptor.max_input_bytes,
        "output_schema_version": CONVERSION_OUTPUT_SCHEMA_VERSION,
    }


def _validate_attempt_recipe(
    *,
    attempt: LibraryAttempt,
    run: LibraryRun,
    version: LibraryItemVersion,
    chain_entry: dict[str, Any],
) -> None:
    """Reject an attempt whose persisted execution identity was tampered with.

    The run snapshot is the immutable recipe authority. Revalidating every
    denormalized attempt field prevents corrupted rows from executing one
    provider while recording another provider's provenance.
    """

    expected_config_hash = _stable_hash(
        {
            "provider": chain_entry,
            "budget": run.conversion_detail.per_provider_budget_json,
            "policy_revision": run.policy_revision,
        }
    )
    expected = (
        str(chain_entry.get("converter_id")),
        str(chain_entry.get("converter_version")),
        str(chain_entry.get("spi_version")),
        expected_config_hash,
        version.content_hash,
        run.output_schema_version,
    )
    actual = (
        attempt.provider_id,
        attempt.provider_version,
        attempt.provider_spi_version,
        attempt.config_hash,
        attempt.input_sha256,
        attempt.output_schema_version,
    )
    if (
        actual != expected
        or chain_entry.get("output_schema_version") != run.output_schema_version
    ):
        raise ConversionError(
            "converter_recipe_invalid",
            "The persisted converter recipe failed integrity validation.",
            retryable=False,
            fallback_allowed=False,
        )


_CONVERSION_ARTIFACT_KIND_BY_MEDIA_TYPE: dict[str, str] = {
    "text/markdown": "markdown",
    "text/plain": "text",
    "text/csv": "table",
    "text/tab-separated-values": "table",
    "application/json": "metadata",
    "application/pdf": "pdf",
}


def _artifact_kind_for_media_type(media_type: str) -> str:
    # LibraryArtifact.kind is new for conversion (no prior SPI field fed it) --
    # this mapping is invented, not carried over from existing behavior.
    kind = _CONVERSION_ARTIFACT_KIND_BY_MEDIA_TYPE.get(media_type)
    if kind is not None:
        return kind
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("audio/"):
        return "audio"
    if media_type.startswith("video/"):
        return "video"
    return "text"


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _retry_audit_json_evidence(value: object) -> dict[str, int | str]:
    """Return bounded integrity evidence without duplicating arbitrary JSON."""

    return {
        "entry_count": len(value) if isinstance(value, (dict, list)) else 0,
        "sha256": _stable_hash(value),
    }


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "ConversionExecution",
    "LIBRARY_CONVERSION_JOB_TYPE",
    "LibraryConversionOrchestrator",
    "LibraryConversionRetryNotAvailableError",
    "LibraryConversionRetryNotFoundError",
    "MAX_DURABLE_CONVERSION_OUTPUTS",
    "RetriedConversion",
    "ScheduledConversion",
    "get_or_create_library_blob",
]
