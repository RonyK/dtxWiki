"""Persisted and fenced Library Preview scheduling and execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from tempfile import SpooledTemporaryFile
from typing import Any, BinaryIO, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

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
    LibraryArtifactPreviewDetail,
    LibraryAttempt,
    LibraryAttemptPreviewDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryLogicalCharge,
    LibraryRun,
    LibraryRunPreviewDetail,
)
from backend.models.media import SecurityBlockedBlobRow
from backend.repositories.media_blob_reference import (
    BlobReferenceOwnerKind,
    SqlAlchemyBlobReferenceLedger,
    lock_workspace_blob_reference_intent,
)
from backend.repositories.workspace_scoped import get_in_workspace
from backend.services.db_write_intent import acquire_write_intent
from backend.services.job_queue import JobQueueService
from backend.services.library_blob_storage import (
    FilesystemBlobHandle,
    FilesystemLibraryBlobStore,
    LibraryBlobStorageError,
)
from backend.services.preview_contracts import renderer_accepts_variant
from backend.services.preview_execution import (
    PreviewExecutionBoundary,
)
from backend.services.preview_provider_registry import (
    PreviewProviderRegistry,
    PreviewProviderRegistryError,
    snapshot_hash,
)
from backend.spi.preview import (
    PREVIEW_OUTPUT_SCHEMA_VERSION,
    PREVIEW_RENDERER_CONTRACT_VERSION,
    JsonValue,
    PreviewDelivery,
    PreviewInputKind,
    PreviewLimits,
    PreviewOutputSink,
    PreviewProviderDescriptor,
    PreviewProviderError,
    PreviewRequest,
    PreviewResult,
    PreviewVariantKind,
    PreviewVariantReceipt,
    UnsupportedPreviewError,
    thaw_json,
)

MAX_PREVIEW_TRANSPORT_RETRIES = 1
LIBRARY_PREVIEW_JOB_TYPE = "library_preview"
_SINK_COPY_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ScheduledPreview:
    run: LibraryRun
    attempt: LibraryAttempt | None
    job: Job | None = None


@dataclass(frozen=True, slots=True)
class PreviewExecution:
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
class _InputFacts:
    input_kind: PreviewInputKind
    conversion_attempt_id: UUID | None
    artifact_id: UUID | None
    sha256: str
    byte_size: int
    media_type: str
    format_id: str
    original_filename: str
    handle: _OpaquePreviewInput


class _OpaquePreviewInput:
    """Narrow adapter that hides the concrete store handle and backing path."""

    def __init__(self, handle: FilesystemBlobHandle) -> None:
        self.__handle = handle

    @property
    def byte_size(self) -> int:
        return self.__handle.byte_size

    def open_binary(self) -> BinaryIO:
        return self.__handle.open_binary()

    def copy_to(self, destination) -> int:
        return self.__handle.copy_to(destination)


@dataclass(slots=True)
class _BufferedVariant:
    receipt: PreviewVariantReceipt
    stream: BinaryIO

    def close(self) -> None:
        self.stream.close()


@dataclass(frozen=True, slots=True)
class _InstalledVariant:
    receipt: PreviewVariantReceipt
    available: AvailableBlobRecord


class _BufferedPreviewSink(PreviewOutputSink):
    """Bound provider output before any quota reservation or publication."""

    def __init__(
        self,
        *,
        limits: PreviewLimits,
        descriptor: PreviewProviderDescriptor,
    ) -> None:
        self._limits = limits
        self._descriptor = descriptor
        self._variants: dict[UUID, _BufferedVariant] = {}
        self._variant_keys: set[tuple[PreviewVariantKind, int]] = set()
        self._total_bytes = 0

    def write_variant(
        self,
        *,
        source: BinaryIO,
        expected_sha256: str,
        expected_byte_size: int,
        kind: PreviewVariantKind,
        media_type: str,
        renderer_contract: str,
        delivery: PreviewDelivery,
        rank: int,
        ordinal: int = 0,
        filename: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
        page_count: int | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> PreviewVariantReceipt:
        if (
            type(kind) is not str
            or kind
            not in {
                "markdown",
                "text",
                "table",
                "pdf",
                "image",
                "audio",
                "video",
                "metadata",
            }
            or type(media_type) is not str
            or type(renderer_contract) is not str
            or type(delivery) is not str
            or type(rank) is not int
            or rank < 0
            or type(ordinal) is not int
            or ordinal < 0
            or len(self._variants) >= self._limits.max_variants
            or type(expected_byte_size) is not int
            or expected_byte_size <= 0
            or expected_byte_size > self._limits.max_variant_bytes
            or self._total_bytes + expected_byte_size
            > self._limits.max_total_output_bytes
            or (kind, ordinal) in self._variant_keys
            or renderer_contract not in self._descriptor.output_renderer_contracts
            or media_type not in self._descriptor.output_media_types
            or not renderer_accepts_variant(
                renderer_contract,
                kind=kind,
                media_type=media_type,
                delivery=delivery,
            )
            or (width is None) != (height is None)
            or (width is not None and kind not in {"image", "video"})
            or (page_count is not None and kind != "pdf")
            or (duration_ms is not None and kind not in {"audio", "video"})
            or (width is not None and (type(width) is not int or width <= 0))
            or (height is not None and (type(height) is not int or height <= 0))
            or (
                width is not None
                and height is not None
                and width * height > self._limits.max_pixels
            )
            or (
                page_count is not None
                and (
                    type(page_count) is not int
                    or page_count <= 0
                    or page_count > self._limits.max_pages
                )
            )
            or (
                duration_ms is not None
                and (
                    type(duration_ms) is not int
                    or duration_ms <= 0
                    or duration_ms > self._limits.max_duration_seconds * 1_000
                )
            )
        ):
            raise PreviewProviderError(
                "preview_output_invalid",
                "The Preview provider output exceeded its approved contract.",
            )
        try:
            digest = Sha256Digest(expected_sha256)
        except (TypeError, ValueError) as exc:
            raise PreviewProviderError(
                "preview_output_invalid",
                "The Preview provider output digest was invalid.",
            ) from exc
        stream = SpooledTemporaryFile(
            max_size=min(self._limits.max_variant_bytes, 8 * 1024 * 1024),
            mode="w+b",
        )
        actual_digest = hashlib.sha256()
        actual_size = 0
        try:
            while True:
                chunk = source.read(_SINK_COPY_CHUNK_BYTES)
                if type(chunk) is not bytes or len(chunk) > _SINK_COPY_CHUNK_BYTES:
                    raise PreviewProviderError(
                        "preview_output_invalid",
                        "The Preview provider output stream was invalid.",
                    )
                if not chunk:
                    break
                actual_size += len(chunk)
                if actual_size > expected_byte_size:
                    raise PreviewProviderError(
                        "preview_output_invalid",
                        "The Preview provider output size changed while streaming.",
                    )
                actual_digest.update(chunk)
                stream.write(chunk)
            if (
                actual_size != expected_byte_size
                or actual_digest.hexdigest() != digest.value
            ):
                raise PreviewProviderError(
                    "preview_output_invalid",
                    "The Preview provider output failed integrity validation.",
                )
            stream.seek(0)
            receipt = PreviewVariantReceipt(
                receipt_id=uuid4(),
                kind=kind,
                media_type=media_type,
                renderer_contract=renderer_contract,
                delivery=delivery,
                byte_size=actual_size,
                sha256=digest.value,
                rank=rank,
                ordinal=ordinal,
                filename=filename,
                width=width,
                height=height,
                duration_ms=duration_ms,
                page_count=page_count,
                metadata=metadata or {},
            )
            self._variants[receipt.receipt_id] = _BufferedVariant(receipt, stream)
            self._variant_keys.add((kind, ordinal))
            self._total_bytes += actual_size
            return receipt
        except Exception:
            stream.close()
            raise

    def validated_outputs(
        self,
        result: PreviewResult,
    ) -> tuple[_BufferedVariant, ...]:
        result_ids = tuple(receipt.receipt_id for receipt in result.variants)
        if set(result_ids) != set(self._variants) or len(result_ids) != len(
            self._variants
        ):
            raise PreviewProviderError(
                "preview_output_invalid",
                "The Preview provider returned an inconsistent output set.",
            )
        return tuple(self._variants[receipt_id] for receipt_id in result_ids)

    def close(self) -> None:
        for variant in self._variants.values():
            variant.close()
        self._variants.clear()
        self._variant_keys.clear()


class LibraryPreviewOrchestrator:
    """Schedule immutable provider recipes and promote generated variants by CAS."""

    def __init__(
        self,
        *,
        provider_registry: PreviewProviderRegistry,
        store_for: Callable[[UUID], FilesystemLibraryBlobStore],
        blob_repository_factory: LibraryBlobRepositoryFactory,
        media_installer_factory: Callable[[Session, UUID], InstallValidatedBlob],
        logical_quota_factory: LibraryLogicalQuotaFactory,
        execution_boundary: PreviewExecutionBoundary,
        durable_media_installer_factory: Callable[
            [sessionmaker[Session], UUID], InstallValidatedBlob
        ]
        | None = None,
        job_queue: JobQueueService | None = None,
        default_limits: PreviewLimits | None = None,
        preferred_chains: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.provider_registry = provider_registry
        # D-5: resolved per call, not cached -- see LibraryContentService.
        self.store_for = store_for
        self.blob_repository_factory = blob_repository_factory
        self.media_installer_factory = media_installer_factory
        self.durable_media_installer_factory = durable_media_installer_factory
        self.logical_quota_factory = logical_quota_factory
        self.job_queue = job_queue or JobQueueService()
        self.execution_boundary = execution_boundary
        self.default_limits = default_limits
        self.preferred_chains = dict(preferred_chains or {})

    def schedule(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version_id: UUID,
        limits: PreviewLimits | None = None,
        actor_kind: str,
        actor_id: str,
        reason: Literal["initial", "manual_regenerate", "dependency_change"] = (
            "initial"
        ),
        input_artifact_id: UUID | None = None,
        recipe: dict[str, JsonValue] | None = None,
        policy_revision: str = "preview-v1",
    ) -> ScheduledPreview:
        limits = limits or self.default_limits
        if limits is None:
            raise ValueError("Preview limits are not configured")
        acquire_write_intent(session)
        seed_version = get_in_workspace(
            session,
            LibraryItemVersion,
            workspace_id=workspace_id,
            resource_id=version_id,
        )
        item = session.scalar(
            select(LibraryItem)
            .where(
                LibraryItem.id
                == (seed_version.library_item_id if seed_version is not None else None)
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        version = session.scalar(
            select(LibraryItemVersion)
            .where(LibraryItemVersion.id == version_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if item is None or item.workspace_id != workspace_id or version is None:
            raise ValueError("Preview source association is invalid")
        facts = self._resolve_input(
            session=session,
            workspace_id=workspace_id,
            version=version,
            input_artifact_id=input_artifact_id,
        )
        if facts.byte_size > limits.max_input_bytes:
            raise ValueError("Preview input exceeds the requested limit")
        chain = self._executable_provider_chain(
            format_id=facts.format_id,
            media_type=facts.media_type,
            input_kind=facts.input_kind,
            byte_size=facts.byte_size,
        )
        recipe_snapshot = _json_snapshot(recipe or {})
        input_dependency = {
            "input_kind": facts.input_kind,
            "input_conversion_attempt_id": _str_or_none(facts.conversion_attempt_id),
            "input_artifact_id": _str_or_none(facts.artifact_id),
            "input_sha256": facts.sha256,
        }
        idempotency_key = ":".join(
            (
                "preview-v1",
                facts.sha256,
                (
                    "source"
                    if facts.input_kind == "source"
                    else f"{facts.conversion_attempt_id}:{facts.artifact_id}"
                ),
                snapshot_hash(chain),
                snapshot_hash(recipe_snapshot),
                PREVIEW_OUTPUT_SCHEMA_VERSION,
                PREVIEW_RENDERER_CONTRACT_VERSION,
            )
        )
        existing = session.scalar(
            select(LibraryRun).where(
                LibraryRun.library_item_version_id == version.id,
                LibraryRun.stage == "preview_generation",
                LibraryRun.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            attempt = session.scalar(
                select(LibraryAttempt)
                .where(
                    LibraryAttempt.run_id == existing.id,
                    LibraryAttempt.stage == "preview_generation",
                )
                .order_by(LibraryAttempt.chain_index.asc())
                .limit(1)
            )
            job = (
                self._enqueue_attempt(
                    session=session,
                    workspace_id=workspace_id,
                    attempt=attempt,
                )
                if attempt is not None and attempt.status == "queued"
                else None
            )
            return ScheduledPreview(existing, attempt, job)

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
        run = LibraryRun(
            workspace_id=version.workspace_id,
            library_item_version_id=version.id,
            stage="preview_generation",
            run_seq=run_seq,
            reason=reason,
            status="queued" if chain else "unavailable",
            requested_by_actor_type=actor_kind,
            requested_by_actor_id=_uuid_or_none(actor_id),
            provider_chain_json=chain,
            policy_revision=policy_revision,
            output_schema_version=PREVIEW_OUTPUT_SCHEMA_VERSION,
            idempotency_key=idempotency_key,
            finished_at=utc_now() if not chain else None,
        )
        run.preview_detail = LibraryRunPreviewDetail(
            recipe_json=recipe_snapshot,
            limits_json=asdict(limits),
            input_dependency_json=input_dependency,
            renderer_contract_version=PREVIEW_RENDERER_CONTRACT_VERSION,
            expected_selected_preview_attempt_id=version.selected_preview_attempt_id,
        )
        session.add(run)
        session.flush()
        if not chain:
            if version.selected_preview_attempt_id is None:
                version.preview_status = "unavailable"
            return ScheduledPreview(run, None, None)
        attempt = self._new_attempt(
            run=run,
            snapshot=chain[0],
            chain_index=0,
            input_kind=facts.input_kind,
            input_conversion_attempt_id=facts.conversion_attempt_id,
            input_artifact_id=facts.artifact_id,
            input_sha256=facts.sha256,
            parent_attempt_id=None,
        )
        session.add(attempt)
        if version.selected_preview_attempt_id is None:
            version.preview_status = "queued"
        session.flush()
        job = self._enqueue_attempt(
            session=session,
            workspace_id=workspace_id,
            attempt=attempt,
        )
        return ScheduledPreview(run, attempt, job)

    def invalidate_for_conversion_promotion(
        self,
        *,
        session: Session,
        item: LibraryItem,
        version: LibraryItemVersion,
        conversion_attempt_id: UUID,
    ) -> ScheduledPreview | None:
        """Fence a semantic-input Preview and schedule its exact replacement.

        Source-only Preview variants are independent of semantic conversion and
        intentionally survive same-version conversion promotion.
        """

        selected_id = version.selected_preview_attempt_id
        if selected_id is None:
            return None
        selected = get_in_workspace(
            session,
            LibraryAttempt,
            workspace_id=item.workspace_id,
            resource_id=selected_id,
        )
        selected_detail = selected.preview_detail if selected is not None else None
        if selected_detail is not None and selected_detail.input_kind == "source":
            return None
        if (
            selected_detail is not None
            and selected_detail.input_attempt_id == conversion_attempt_id
        ):
            return None

        version.selected_preview_attempt_id = None
        version.preview_status = "unavailable"
        if item.current_preview_version_id == version.id:
            item.current_preview_version_id = None
        session.add_all([item, version])

        if self.default_limits is None:
            return None
        role_priority = {
            "preview": 0,
            "markdown": 1,
            "search_text": 2,
            "metadata": 3,
            "asset": 4,
            "location_map": 5,
        }
        artifacts = tuple(
            session.scalars(
                select(LibraryArtifact).where(
                    LibraryArtifact.library_item_version_id == version.id,
                    LibraryArtifact.stage == "conversion",
                    LibraryArtifact.attempt_id == conversion_attempt_id,
                    LibraryArtifact.content_sha256.is_not(None),
                    LibraryArtifact.byte_size.is_not(None),
                    LibraryArtifact.media_type.is_not(None),
                )
            )
        )
        for artifact in sorted(
            artifacts,
            key=lambda row: (
                role_priority.get(row.role, len(role_priority)),
                row.ordinal,
                str(row.id),
            ),
        ):
            chain = self._executable_provider_chain(
                format_id=version.format_id or "binary",
                media_type=artifact.media_type,
                input_kind="conversion_artifact",
                byte_size=artifact.byte_size,
            )
            if not chain:
                continue
            return self.schedule(
                session=session,
                workspace_id=item.workspace_id,
                version_id=version.id,
                limits=self.default_limits,
                actor_kind="system",
                actor_id="library-converter",
                reason="dependency_change",
                input_artifact_id=artifact.id,
            )
        return None

    def _executable_provider_chain(
        self,
        *,
        format_id: str,
        media_type: str,
        input_kind: PreviewInputKind,
        byte_size: int,
    ) -> list[dict[str, Any]]:
        chain = self.provider_registry.provider_chain_snapshot(
            format_id=format_id,
            media_type=media_type,
            input_kind=input_kind,
            preferred_ids=self.preferred_chains.get(format_id, ()),
            ready_only=True,
        )
        executable: list[dict[str, Any]] = []
        for snapshot in chain:
            descriptor = self.provider_registry.descriptor(str(snapshot["provider_id"]))
            if (
                descriptor.max_input_bytes is not None
                and byte_size > descriptor.max_input_bytes
            ) or not self.execution_boundary.can_execute(descriptor):
                continue
            executable.append(snapshot)
        return executable

    def execute_attempt(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt_id: UUID,
        execution_generation: int,
    ) -> PreviewExecution:
        acquire_write_intent(session)
        lock_workspace_blob_reference_intent(
            session,
            workspace_id,
            allow_deleting=False,
        )
        lineage = self._lock_lineage(
            session,
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        if lineage is None:
            raise ValueError("Preview attempt was not found")
        item, version, run, attempt = lineage
        if item.workspace_id != workspace_id:
            raise ValueError("Preview attempt Workspace is invalid")
        if (
            attempt.execution_generation != execution_generation
            or attempt.status not in {"queued", "running"}
        ):
            return self._execution(attempt, run, "stale")
        if (
            version.selected_preview_attempt_id
            != run.preview_detail.expected_selected_preview_attempt_id
        ):
            return self._cancel_stale(attempt, run, version)
        try:
            facts = self._resolve_input_from_attempt(
                session=session,
                workspace_id=workspace_id,
                version=version,
                attempt=attempt,
            )
            snapshot = run.provider_chain_json[attempt.chain_index]
            plugin = self.provider_registry.plugin_for_snapshot(snapshot)
            descriptor = self.provider_registry.descriptor(attempt.provider_id)
            limits = PreviewLimits(**run.preview_detail.limits_json)
            provider_limit = descriptor.max_input_bytes
            if provider_limit is not None and facts.byte_size > provider_limit:
                raise UnsupportedPreviewError(
                    "Preview input exceeds the provider's immutable safety limit."
                )
            if not self.execution_boundary.can_execute(descriptor):
                raise PreviewProviderError(
                    "preview_executor_unavailable",
                    "The Preview provider has no approved execution boundary.",
                    fallback_allowed=False,
                )
        except PreviewProviderError as exc:
            return self._handle_error(
                session=session,
                workspace_id=workspace_id,
                version=version,
                run=run,
                attempt=attempt,
                error=exc,
            )
        except (
            IndexError,
            KeyError,
            TypeError,
            PreviewProviderRegistryError,
            LibraryBlobStorageError,
            ValueError,
        ):
            return self._handle_error(
                session=session,
                workspace_id=workspace_id,
                version=version,
                run=run,
                attempt=attempt,
                error=PreviewProviderError(
                    "preview_dependency_unavailable",
                    "The Preview input or provider snapshot is no longer available.",
                    fallback_allowed=False,
                ),
            )
        except Exception:
            return self._handle_error(
                session=session,
                workspace_id=workspace_id,
                version=version,
                run=run,
                attempt=attempt,
                error=PreviewProviderError(
                    "preview_provider_failed",
                    "The Preview provider failed safely.",
                    fallback_allowed=True,
                ),
            )

        attempt.status = "running"
        attempt.started_at = attempt.started_at or utc_now()
        run.status = "running"
        if version.selected_preview_attempt_id is None:
            version.preview_status = "generating"
        session.flush()
        sink = _BufferedPreviewSink(limits=limits, descriptor=descriptor)
        try:
            request = PreviewRequest(
                workspace_id=workspace_id,
                library_item_id=item.id,
                version_id=version.id,
                input_kind=facts.input_kind,
                input_conversion_attempt_id=facts.conversion_attempt_id,
                input_artifact_id=facts.artifact_id,
                source=facts.handle,
                input_sha256=facts.sha256,
                byte_size=facts.byte_size,
                format_id=facts.format_id,
                media_type=facts.media_type,
                original_filename=facts.original_filename,
                limits=limits,
                recipe=run.preview_detail.recipe_json,
                trace_id=f"preview:{attempt.id}:{execution_generation}",
            )
            result = self.execution_boundary.execute(
                plugin=plugin,
                descriptor=descriptor,
                request=request,
                sink=sink,
            )
            outputs = sink.validated_outputs(result)
            if not self._dependency_matches(
                session,
                workspace_id=workspace_id,
                version=version,
                attempt=attempt,
            ):
                return self._cancel_stale(attempt, run, version)
            with session.begin_nested():
                artifact_ids = self._reserve_quota(
                    session=session,
                    workspace_id=workspace_id,
                    attempt=attempt,
                    outputs=outputs,
                )
                artifacts = self._install_and_persist(
                    session=session,
                    workspace_id=workspace_id,
                    version=version,
                    attempt=attempt,
                    outputs=outputs,
                    artifact_ids=artifact_ids,
                )
        except LibraryLogicalQuotaExceededError:
            return self._handle_error(
                session=session,
                workspace_id=workspace_id,
                version=version,
                run=run,
                attempt=attempt,
                error=PreviewProviderError(
                    "policy_rejected",
                    "The Library derived-media quota was exceeded.",
                    fallback_allowed=False,
                ),
            )
        except PreviewProviderError as exc:
            return self._handle_error(
                session=session,
                workspace_id=workspace_id,
                version=version,
                run=run,
                attempt=attempt,
                error=exc,
            )
        except Exception:
            return self._handle_error(
                session=session,
                workspace_id=workspace_id,
                version=version,
                run=run,
                attempt=attempt,
                error=PreviewProviderError(
                    "preview_provider_failed",
                    "The Preview provider failed safely.",
                    fallback_allowed=True,
                ),
            )
        finally:
            sink.close()

        now = utc_now()
        attempt.status = "completed"
        attempt.finished_at = now
        attempt.error_code = None
        attempt.safe_error_summary = None
        attempt.provenance_json = {
            "provider_id": attempt.provider_id,
            "provider_version": attempt.provider_version,
            "provider_distribution_digest": (
                attempt.preview_detail.provider_distribution_digest
            ),
            "input_kind": attempt.preview_detail.input_kind,
            "input_sha256": attempt.input_sha256,
            "artifact_ids": [str(artifact.id) for artifact in artifacts],
        }
        attempt.metrics_json = {
            "variant_count": len(artifacts),
            "output_bytes": sum(artifact.byte_size for artifact in artifacts),
        }
        run.status = "completed"
        run.finished_at = now
        version.selected_preview_attempt_id = attempt.id
        version.preview_status = (
            "ready_partial" if result.completeness == "partial" else "ready"
        )
        if self._may_advance_item_preview_pointer(
            session,
            workspace_id=workspace_id,
            item=item,
            version=version,
        ):
            item.current_preview_version_id = version.id
        session.flush()
        return PreviewExecution(
            attempt_id=attempt.id,
            run_id=run.id,
            version_id=version.id,
            status=version.preview_status,  # type: ignore[arg-type]
            selected=True,
        )

    def mark_attempt_running(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt_id: UUID,
        execution_generation: int,
    ) -> bool:
        """Commit a short running marker before provider execution begins."""

        acquire_write_intent(session)
        lineage = self._lock_lineage(
            session,
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        if lineage is None:
            raise ValueError("Preview attempt was not found")
        _item, version, run, attempt = lineage
        if _item.workspace_id != workspace_id:
            raise ValueError("Preview attempt Workspace is invalid")
        if (
            attempt.execution_generation != execution_generation
            or attempt.status not in {"queued", "running"}
        ):
            return False
        if (
            version.selected_preview_attempt_id
            != run.preview_detail.expected_selected_preview_attempt_id
            or not self._dependency_matches(
                session,
                workspace_id=workspace_id,
                version=version,
                attempt=attempt,
            )
        ):
            self._cancel_stale(attempt, run, version)
            return False
        now = utc_now()
        attempt.status = "running"
        attempt.started_at = attempt.started_at or now
        run.status = "running"
        if version.selected_preview_attempt_id is None:
            version.preview_status = "generating"
        session.add_all([attempt, run, version])
        return True

    def execute_attempt_durable(
        self,
        *,
        session_factory: sessionmaker[Session],
        workspace_id: UUID,
        attempt_id: UUID,
        execution_generation: int,
    ) -> PreviewExecution:
        """Generate outside DB UoWs, then install and promote in short UoWs."""

        if self.durable_media_installer_factory is None:
            raise RuntimeError("durable Preview media installer is not configured")

        preflight_error: PreviewProviderError | None = None
        with session_factory() as read_session:
            attempt = get_in_workspace(
                read_session,
                LibraryAttempt,
                workspace_id=workspace_id,
                resource_id=attempt_id,
            )
            if attempt is None or attempt.stage != "preview_generation":
                raise ValueError("Preview attempt was not found")
            run = get_in_workspace(
                read_session,
                LibraryRun,
                workspace_id=workspace_id,
                resource_id=attempt.run_id,
            )
            if run is not None and run.stage != "preview_generation":
                run = None
            version = get_in_workspace(
                read_session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=attempt.library_item_version_id,
            )
            item = (
                get_in_workspace(
                    read_session,
                    LibraryItem,
                    workspace_id=workspace_id,
                    resource_id=version.library_item_id,
                )
                if version is not None
                else None
            )
            if run is None or version is None or item is None:
                raise ValueError("Preview lineage is incomplete")
            if item.workspace_id != workspace_id:
                raise ValueError("Preview attempt Workspace is invalid")
            if attempt.execution_generation != execution_generation:
                return self._execution(attempt, run, "stale")
            if attempt.status == "completed":
                selected = version.selected_preview_attempt_id == attempt.id
                return self._execution(
                    attempt,
                    run,
                    version.preview_status if selected else "stale",
                    selected=selected,
                )
            if attempt.status not in {"queued", "running"}:
                return self._execution(attempt, run, "stale")
            if (
                version.selected_preview_attempt_id
                != run.preview_detail.expected_selected_preview_attempt_id
            ):
                preflight_error = PreviewProviderError(
                    "preview_dependency_stale",
                    "The Preview input dependency changed.",
                    fallback_allowed=False,
                )
                facts = None
            else:
                try:
                    facts = self._resolve_input_from_attempt(
                        session=read_session,
                        workspace_id=workspace_id,
                        version=version,
                        attempt=attempt,
                    )
                    snapshot = dict(run.provider_chain_json[attempt.chain_index])
                    descriptor = self.provider_registry.descriptor(attempt.provider_id)
                    plugin = self.provider_registry.plugin_for_snapshot(snapshot)
                    limits = PreviewLimits(**dict(run.preview_detail.limits_json))
                    provider_limit = descriptor.max_input_bytes
                    if provider_limit is not None and facts.byte_size > provider_limit:
                        raise UnsupportedPreviewError(
                            "Preview input exceeds the provider's immutable "
                            "safety limit."
                        )
                    if not self.execution_boundary.can_execute(descriptor):
                        raise PreviewProviderError(
                            "preview_executor_unavailable",
                            "The Preview provider has no approved execution boundary.",
                            fallback_allowed=False,
                        )
                except PreviewProviderError as exc:
                    preflight_error = exc
                    facts = None
                except (
                    IndexError,
                    KeyError,
                    TypeError,
                    PreviewProviderRegistryError,
                    LibraryBlobStorageError,
                    ValueError,
                ):
                    preflight_error = PreviewProviderError(
                        "preview_dependency_unavailable",
                        "The Preview input or provider snapshot is no longer "
                        "available.",
                        fallback_allowed=False,
                    )
                    facts = None

            item_id = item.id
            version_id = version.id
            run_id = run.id
            recipe = dict(run.preview_detail.recipe_json)

        if preflight_error is not None:
            if preflight_error.code == "preview_dependency_stale":
                return self._record_durable_stale(
                    session_factory=session_factory,
                    workspace_id=workspace_id,
                    attempt_id=attempt_id,
                    execution_generation=execution_generation,
                )
            return self._record_durable_error(
                session_factory=session_factory,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                execution_generation=execution_generation,
                error=preflight_error,
            )
        assert facts is not None

        sink = _BufferedPreviewSink(limits=limits, descriptor=descriptor)
        try:
            try:
                request = PreviewRequest(
                    workspace_id=workspace_id,
                    library_item_id=item_id,
                    version_id=version_id,
                    input_kind=facts.input_kind,
                    input_conversion_attempt_id=facts.conversion_attempt_id,
                    input_artifact_id=facts.artifact_id,
                    source=facts.handle,
                    input_sha256=facts.sha256,
                    byte_size=facts.byte_size,
                    format_id=facts.format_id,
                    media_type=facts.media_type,
                    original_filename=facts.original_filename,
                    limits=limits,
                    recipe=recipe,
                    trace_id=f"preview:{attempt_id}:{execution_generation}",
                )
                result = self.execution_boundary.execute(
                    plugin=plugin,
                    descriptor=descriptor,
                    request=request,
                    sink=sink,
                )
                outputs = sink.validated_outputs(result)
            except PreviewProviderError as exc:
                return self._record_durable_error(
                    session_factory=session_factory,
                    workspace_id=workspace_id,
                    attempt_id=attempt_id,
                    execution_generation=execution_generation,
                    error=exc,
                )
            except Exception:
                return self._record_durable_error(
                    session_factory=session_factory,
                    workspace_id=workspace_id,
                    attempt_id=attempt_id,
                    execution_generation=execution_generation,
                    error=PreviewProviderError(
                        "preview_provider_failed",
                        "The Preview provider failed safely.",
                        fallback_allowed=True,
                    ),
                )

            try:
                with session_factory.begin() as quota_session:
                    self._ensure_quota_capacity(
                        session=quota_session,
                        workspace_id=workspace_id,
                        attempt_id=attempt_id,
                        outputs=outputs,
                    )
            except LibraryLogicalQuotaExceededError:
                return self._record_durable_error(
                    session_factory=session_factory,
                    workspace_id=workspace_id,
                    attempt_id=attempt_id,
                    execution_generation=execution_generation,
                    error=PreviewProviderError(
                        "policy_rejected",
                        "The Library derived-media quota was exceeded.",
                        fallback_allowed=False,
                    ),
                )

            canonical_media_type_by_digest: dict[Sha256Digest, str] = {}
            with session_factory() as lookup_session:
                repository = self.blob_repository_factory(lookup_session)
                for output in outputs:
                    digest = Sha256Digest(output.receipt.sha256)
                    if digest in canonical_media_type_by_digest:
                        continue
                    existing = repository.find(
                        WorkspaceBlobIdentity(workspace_id, digest)
                    )
                    canonical_media_type_by_digest[digest] = (
                        existing.media_type
                        if existing is not None
                        else output.receipt.media_type
                    )

            installer = self.durable_media_installer_factory(
                session_factory, workspace_id
            )
            installed_rows: list[_InstalledVariant] = []
            try:
                for output in outputs:
                    receipt = output.receipt
                    digest = Sha256Digest(receipt.sha256)
                    output.stream.seek(0)
                    available = installer.execute_stream(
                        workspace_id=workspace_id,
                        actor_id=attempt_id,
                        session_key=f"library-preview:{attempt_id}",
                        idempotency_key=(
                            f"library-preview:{attempt_id}:"
                            f"{receipt.kind}:{receipt.ordinal}"
                        ),
                        stream=output.stream,
                        expected_sha256=receipt.sha256,
                        expected_byte_size=receipt.byte_size,
                        max_bytes=receipt.byte_size,
                        canonical_media_type=canonical_media_type_by_digest[digest],
                        format_id=f"preview-{receipt.kind}",
                        detector_id="library-preview-output",
                        detector_version=descriptor.provider_version,
                        registry_version=descriptor.spi_version,
                        validation_schema_version=PREVIEW_OUTPUT_SCHEMA_VERSION,
                        metadata={
                            "kind": receipt.kind,
                            "renderer_contract": receipt.renderer_contract,
                            "provider_id": descriptor.provider_id,
                            "provider_version": descriptor.provider_version,
                        },
                        association_hold=BlobAssociationHold(
                            producer_kind=(
                                BlobAssociationHoldProducerKind.LIBRARY_PREVIEW_ATTEMPT
                            ),
                            producer_id=attempt_id,
                            producer_generation=execution_generation,
                        ),
                    )
                    if (
                        available.reference.workspace_id != workspace_id
                        or available.sha256 != digest
                        or available.byte_size != receipt.byte_size
                    ):
                        raise RuntimeError(
                            "Shared Media changed immutable Preview facts"
                        )
                    installed_rows.append(_InstalledVariant(receipt, available))
            except Exception:
                return self._record_durable_error(
                    session_factory=session_factory,
                    workspace_id=workspace_id,
                    attempt_id=attempt_id,
                    execution_generation=execution_generation,
                    error=PreviewProviderError(
                        "preview_materialization_failed",
                        "The Preview output could not be stored safely.",
                        retryable=True,
                        fallback_allowed=False,
                    ),
                )

            with session_factory.begin() as write_session:
                acquire_write_intent(write_session)
                lock_workspace_blob_reference_intent(
                    write_session,
                    workspace_id,
                    allow_deleting=False,
                )
                lineage = self._lock_lineage(
                    write_session,
                    workspace_id=workspace_id,
                    attempt_id=attempt_id,
                )
                if lineage is None:
                    return PreviewExecution(
                        attempt_id=attempt_id,
                        run_id=run_id,
                        version_id=version_id,
                        status="stale",
                    )
                item, version, run, current_attempt = lineage
                if (
                    item.workspace_id != workspace_id
                    or current_attempt.execution_generation != execution_generation
                    or current_attempt.status not in {"queued", "running"}
                ):
                    return self._execution(current_attempt, run, "stale")
                if (
                    version.selected_preview_attempt_id
                    != run.preview_detail.expected_selected_preview_attempt_id
                    or not self._dependency_matches(
                        write_session,
                        workspace_id=workspace_id,
                        version=version,
                        attempt=current_attempt,
                    )
                ):
                    return self._cancel_stale(current_attempt, run, version)
                try:
                    with write_session.begin_nested():
                        artifact_ids = self._reserve_quota(
                            session=write_session,
                            workspace_id=workspace_id,
                            attempt=current_attempt,
                            outputs=outputs,
                        )
                        artifacts = self._persist_installed(
                            session=write_session,
                            workspace_id=workspace_id,
                            version=version,
                            attempt=current_attempt,
                            installed=tuple(installed_rows),
                            artifact_ids=artifact_ids,
                        )
                except LibraryLogicalQuotaExceededError:
                    return self._handle_error(
                        session=write_session,
                        workspace_id=workspace_id,
                        version=version,
                        run=run,
                        attempt=current_attempt,
                        error=PreviewProviderError(
                            "policy_rejected",
                            "The Library derived-media quota was exceeded.",
                            fallback_allowed=False,
                        ),
                    )

                now = utc_now()
                current_attempt.status = "completed"
                current_attempt.started_at = current_attempt.started_at or now
                current_attempt.finished_at = now
                current_attempt.error_code = None
                current_attempt.safe_error_summary = None
                current_attempt.provenance_json = {
                    "provider_id": current_attempt.provider_id,
                    "provider_version": current_attempt.provider_version,
                    "provider_distribution_digest": (
                        current_attempt.preview_detail.provider_distribution_digest
                    ),
                    "input_kind": current_attempt.preview_detail.input_kind,
                    "input_sha256": current_attempt.input_sha256,
                    "artifact_ids": [str(artifact.id) for artifact in artifacts],
                }
                current_attempt.metrics_json = {
                    "variant_count": len(artifacts),
                    "output_bytes": sum(artifact.byte_size for artifact in artifacts),
                }
                run.status = "completed"
                run.finished_at = now
                version.selected_preview_attempt_id = current_attempt.id
                version.preview_status = (
                    "ready_partial" if result.completeness == "partial" else "ready"
                )
                if self._may_advance_item_preview_pointer(
                    write_session,
                    workspace_id=workspace_id,
                    item=item,
                    version=version,
                ):
                    item.current_preview_version_id = version.id
                write_session.add_all([current_attempt, run, version, item])
                return PreviewExecution(
                    attempt_id=current_attempt.id,
                    run_id=run.id,
                    version_id=version.id,
                    status=version.preview_status,  # type: ignore[arg-type]
                    selected=True,
                )
        finally:
            sink.close()

    def _record_durable_error(
        self,
        *,
        session_factory: sessionmaker[Session],
        workspace_id: UUID,
        attempt_id: UUID,
        execution_generation: int,
        error: PreviewProviderError,
    ) -> PreviewExecution:
        with session_factory.begin() as session:
            acquire_write_intent(session)
            lineage = self._lock_lineage(
                session,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
            )
            if lineage is None:
                raise ValueError("Preview attempt was not found")
            item, version, run, attempt = lineage
            if item.workspace_id != workspace_id:
                raise ValueError("Preview attempt Workspace is invalid")
            if (
                attempt.execution_generation != execution_generation
                or attempt.status not in {"queued", "running"}
            ):
                return self._execution(attempt, run, "stale")
            return self._handle_error(
                session=session,
                workspace_id=workspace_id,
                version=version,
                run=run,
                attempt=attempt,
                error=error,
            )

    def _record_durable_stale(
        self,
        *,
        session_factory: sessionmaker[Session],
        workspace_id: UUID,
        attempt_id: UUID,
        execution_generation: int,
    ) -> PreviewExecution:
        with session_factory.begin() as session:
            acquire_write_intent(session)
            lineage = self._lock_lineage(
                session,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
            )
            if lineage is None:
                raise ValueError("Preview attempt was not found")
            item, version, run, attempt = lineage
            if item.workspace_id != workspace_id:
                raise ValueError("Preview attempt Workspace is invalid")
            if (
                attempt.execution_generation != execution_generation
                or attempt.status not in {"queued", "running"}
            ):
                return self._execution(attempt, run, "stale")
            return self._cancel_stale(attempt, run, version)

    def mark_terminal_failure(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt_id: UUID,
        execution_generation: int,
    ) -> None:
        acquire_write_intent(session)
        lineage = self._lock_lineage(
            session,
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        if lineage is None:
            return
        _item, version, run, attempt = lineage
        if (
            attempt.execution_generation != execution_generation
            or attempt.status not in {"queued", "running"}
        ):
            return
        now = utc_now()
        attempt.status = "failed"
        attempt.error_code = "preview_worker_failed"
        attempt.safe_error_summary = "The Preview worker failed safely."
        attempt.finished_at = now
        run.status = "failed"
        run.finished_at = now
        if version.selected_preview_attempt_id is None:
            version.preview_status = "failed"
        session.add_all([attempt, run, version])

    def _resolve_input(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version: LibraryItemVersion,
        input_artifact_id: UUID | None,
    ) -> _InputFacts:
        if input_artifact_id is not None:
            artifact = get_in_workspace(
                session,
                LibraryArtifact,
                workspace_id=workspace_id,
                resource_id=input_artifact_id,
            )
            if (
                artifact is None
                or artifact.stage != "conversion"
                or artifact.library_item_version_id != version.id
                or version.selected_conversion_attempt_id is None
                or artifact.attempt_id
                != version.selected_conversion_attempt_id
                or artifact.content_sha256 is None
                or artifact.byte_size is None
                or artifact.media_type is None
            ):
                raise ValueError("Preview conversion input is not canonical")
            blob = self._artifact_blob(
                session=session,
                workspace_id=workspace_id,
                artifact=artifact,
            )
            return _InputFacts(
                input_kind="conversion_artifact",
                conversion_attempt_id=artifact.attempt_id,
                artifact_id=artifact.id,
                sha256=blob.sha256.value,
                byte_size=blob.byte_size,
                media_type=artifact.media_type,
                format_id=version.format_id or "binary",
                original_filename=version.original_filename or "library.bin",
                handle=self._open_blob(blob, workspace_id=workspace_id),
            )
        blob = self._source_blob(
            session=session,
            workspace_id=workspace_id,
            version=version,
        )
        return _InputFacts(
            input_kind="source",
            conversion_attempt_id=None,
            artifact_id=None,
            sha256=blob.sha256.value,
            byte_size=blob.byte_size,
            media_type=version.detected_media_type or blob.media_type,
            format_id=version.format_id or "binary",
            original_filename=version.original_filename or "library.bin",
            handle=self._open_blob(blob, workspace_id=workspace_id),
        )

    def _resolve_input_from_attempt(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version: LibraryItemVersion,
        attempt: LibraryAttempt,
    ) -> _InputFacts:
        detail = attempt.preview_detail
        facts = self._resolve_input(
            session=session,
            workspace_id=workspace_id,
            version=version,
            input_artifact_id=detail.input_artifact_id,
        )
        if (
            facts.input_kind != detail.input_kind
            or facts.conversion_attempt_id != detail.input_attempt_id
            or facts.artifact_id != detail.input_artifact_id
            or facts.sha256 != attempt.input_sha256
        ):
            raise ValueError("Preview input dependency changed")
        return facts

    def _source_blob(self, *, session, workspace_id, version):
        if (
            version.source_blob_workspace_id != workspace_id
            or version.source_blob_id is None
            or version.source_blob_epoch is None
        ):
            raise ValueError("Preview source blob is unavailable")
        reference = BlobReference(
            workspace_id=workspace_id,
            blob_id=version.source_blob_id,
            epoch=BlobEpoch(version.source_blob_epoch),
        )
        if _security_blocked(session, reference):
            raise ValueError("Preview source blob is security blocked")
        blob = self.blob_repository_factory(session).get(reference)
        if (
            blob is None
            or blob.sha256.value != version.content_hash.lower()
            or version.byte_size != blob.byte_size
        ):
            raise ValueError("Preview source blob integrity mismatch")
        return blob

    def _artifact_blob(self, *, session, workspace_id, artifact):
        if (
            artifact.blob_workspace_id != workspace_id
            or artifact.blob_id is None
            or artifact.blob_epoch is None
            or artifact.content_sha256 is None
            or artifact.byte_size is None
        ):
            raise ValueError("Preview artifact input is unavailable")
        reference = BlobReference(
            workspace_id=workspace_id,
            blob_id=artifact.blob_id,
            epoch=BlobEpoch(artifact.blob_epoch),
        )
        if _security_blocked(session, reference):
            raise ValueError("Preview artifact input is security blocked")
        blob = self.blob_repository_factory(session).get(reference)
        if (
            blob is None
            or blob.sha256.value != artifact.content_sha256.lower()
            or blob.byte_size != artifact.byte_size
        ):
            raise ValueError("Preview artifact input integrity mismatch")
        return blob

    def _open_blob(self, blob, *, workspace_id: UUID) -> _OpaquePreviewInput:
        handle = self.store_for(workspace_id).open_handle(
            blob.storage_key,
            byte_size=blob.byte_size,
        )
        with handle.open_binary():
            pass
        return _OpaquePreviewInput(handle)

    def _new_attempt(
        self,
        *,
        run: LibraryRun,
        snapshot: dict[str, Any],
        chain_index: int,
        input_kind: PreviewInputKind,
        input_conversion_attempt_id: UUID | None,
        input_artifact_id: UUID | None,
        input_sha256: str,
        parent_attempt_id: UUID | None,
    ) -> LibraryAttempt:
        attempt = LibraryAttempt(
            workspace_id=run.workspace_id,
            run_id=run.id,
            library_item_version_id=run.library_item_version_id,
            stage="preview_generation",
            provider_id=snapshot["provider_id"],
            provider_version=snapshot["provider_version"],
            provider_spi_version=snapshot["spi_version"],
            config_hash=snapshot_hash(run.preview_detail.recipe_json),
            input_sha256=input_sha256,
            output_schema_version=run.output_schema_version,
            status="queued",
            chain_index=chain_index,
            transport_retry_count=0,
            execution_generation=1,
            parent_attempt_id=parent_attempt_id,
            warnings_json=[],
            provenance_json={},
            metrics_json={},
        )
        attempt.preview_detail = LibraryAttemptPreviewDetail(
            provider_distribution_digest=snapshot["distribution_digest"],
            input_kind=input_kind,
            input_attempt_id=input_conversion_attempt_id,
            input_artifact_id=input_artifact_id,
            renderer_contract_version=run.preview_detail.renderer_contract_version,
        )
        return attempt

    def _reserve_quota(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt: LibraryAttempt,
        outputs: tuple[_BufferedVariant, ...],
    ) -> dict[UUID, UUID]:
        artifact_ids = self._artifact_ids(attempt.id, outputs)
        charged = set(
            session.scalars(
                select(LibraryLogicalCharge.association_id).where(
                    LibraryLogicalCharge.workspace_id == workspace_id,
                    LibraryLogicalCharge.quota_kind == "derived",
                    LibraryLogicalCharge.association_id.in_(artifact_ids.values()),
                    LibraryLogicalCharge.state == "charged",
                )
            )
        )
        self.logical_quota_factory(session).reserve(
            workspace_id,
            quota_kind="derived",
            associations={
                artifact_ids[output.receipt.receipt_id]: output.receipt.byte_size
                for output in outputs
                if artifact_ids[output.receipt.receipt_id] not in charged
            },
        )
        return artifact_ids

    def _ensure_quota_capacity(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        attempt_id: UUID,
        outputs: tuple[_BufferedVariant, ...],
    ) -> None:
        artifact_ids = self._artifact_ids(attempt_id, outputs)
        charged = set(
            session.scalars(
                select(LibraryLogicalCharge.association_id).where(
                    LibraryLogicalCharge.workspace_id == workspace_id,
                    LibraryLogicalCharge.quota_kind == "derived",
                    LibraryLogicalCharge.association_id.in_(artifact_ids.values()),
                    LibraryLogicalCharge.state == "charged",
                )
            )
        )
        self.logical_quota_factory(session).ensure_capacity(
            workspace_id,
            quota_kind="derived",
            additional_bytes=sum(
                output.receipt.byte_size
                for output in outputs
                if artifact_ids[output.receipt.receipt_id] not in charged
            ),
        )

    @staticmethod
    def _artifact_ids(
        attempt_id: UUID,
        outputs: tuple[_BufferedVariant, ...],
    ) -> dict[UUID, UUID]:
        return {
            output.receipt.receipt_id: uuid5(
                NAMESPACE_URL,
                f"library-preview-artifact:{attempt_id}:"
                f"{output.receipt.kind}:{output.receipt.ordinal}",
            )
            for output in outputs
        }

    def _install_and_persist(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version: LibraryItemVersion,
        attempt: LibraryAttempt,
        outputs: tuple[_BufferedVariant, ...],
        artifact_ids: dict[UUID, UUID],
    ) -> tuple[LibraryArtifact, ...]:
        installer: InstallValidatedBlob = self.media_installer_factory(
            session, workspace_id
        )
        repository = self.blob_repository_factory(session)
        artifacts: list[LibraryArtifact] = []
        for output in outputs:
            receipt = output.receipt
            existing_blob = repository.find(
                WorkspaceBlobIdentity(workspace_id, Sha256Digest(receipt.sha256))
            )
            if (
                existing_blob is not None
                and existing_blob.media_type != receipt.media_type
            ):
                raise PreviewProviderError(
                    "preview_media_type_conflict",
                    "Preview output conflicts with an existing immutable media type.",
                    fallback_allowed=False,
                )
            output.stream.seek(0)
            available = installer.execute_stream(
                workspace_id=workspace_id,
                actor_id=attempt.id,
                session_key=f"library-preview:{attempt.id}",
                idempotency_key=(
                    f"library-preview:{attempt.id}:{receipt.kind}:{receipt.ordinal}"
                ),
                stream=output.stream,
                expected_sha256=receipt.sha256,
                expected_byte_size=receipt.byte_size,
                max_bytes=receipt.byte_size,
                canonical_media_type=receipt.media_type,
                format_id=f"preview-{receipt.kind}",
                detector_id="library-preview-output",
                detector_version=attempt.provider_version,
                registry_version=attempt.provider_spi_version,
                validation_schema_version=attempt.output_schema_version,
                metadata={
                    "kind": receipt.kind,
                    "renderer_contract": receipt.renderer_contract,
                    "provider_id": attempt.provider_id,
                    "provider_version": attempt.provider_version,
                },
            )
            if (
                available.reference.workspace_id != workspace_id
                or available.sha256.value != receipt.sha256
                or available.byte_size != receipt.byte_size
            ):
                raise RuntimeError("Shared Media changed immutable Preview facts")
            artifact_id = artifact_ids[receipt.receipt_id]
            existing = get_in_workspace(
                session,
                LibraryArtifact,
                workspace_id=workspace_id,
                resource_id=artifact_id,
            )
            if existing is not None:
                if (
                    existing.stage != "preview_generation"
                    or existing.attempt_id != attempt.id
                    or existing.content_sha256 != receipt.sha256
                    or existing.blob_id != available.reference.blob_id
                    or existing.blob_epoch != available.reference.epoch.value
                ):
                    raise ValueError("Preview artifact identity conflict")
                SqlAlchemyBlobReferenceLedger(session).register(
                    owner_kind=BlobReferenceOwnerKind.LIBRARY_ARTIFACT,
                    owner_id=existing.id,
                    blob=available.reference,
                )
                artifacts.append(existing)
                continue
            artifact = LibraryArtifact(
                id=artifact_id,
                workspace_id=version.workspace_id,
                library_item_version_id=version.id,
                attempt_id=attempt.id,
                stage="preview_generation",
                blob_workspace_id=available.reference.workspace_id,
                blob_id=available.reference.blob_id,
                blob_epoch=available.reference.epoch.value,
                role="preview",
                kind=receipt.kind,
                media_type=receipt.media_type,
                content_sha256=receipt.sha256,
                byte_size=receipt.byte_size,
                ordinal=receipt.ordinal,
            )
            artifact.preview_detail = LibraryArtifactPreviewDetail(
                renderer_contract=receipt.renderer_contract,
                delivery=receipt.delivery,
                rank=receipt.rank,
                filename=receipt.filename,
                width=receipt.width,
                height=receipt.height,
                duration_ms=receipt.duration_ms,
                page_count=receipt.page_count,
                metadata_json=thaw_json(receipt.metadata),
            )
            SqlAlchemyBlobReferenceLedger(session).register(
                owner_kind=BlobReferenceOwnerKind.LIBRARY_ARTIFACT,
                owner_id=artifact.id,
                blob=available.reference,
            )
            session.add(artifact)
            artifacts.append(artifact)
        session.flush()
        return tuple(artifacts)

    def _persist_installed(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version: LibraryItemVersion,
        attempt: LibraryAttempt,
        installed: tuple[_InstalledVariant, ...],
        artifact_ids: dict[UUID, UUID],
    ) -> tuple[LibraryArtifact, ...]:
        artifacts: list[LibraryArtifact] = []
        for output in installed:
            receipt = output.receipt
            available = output.available
            artifact_id = artifact_ids[receipt.receipt_id]
            existing = get_in_workspace(
                session,
                LibraryArtifact,
                workspace_id=workspace_id,
                resource_id=artifact_id,
            )
            if existing is not None:
                if (
                    existing.stage != "preview_generation"
                    or existing.attempt_id != attempt.id
                    or existing.content_sha256 != receipt.sha256
                    or existing.blob_workspace_id != available.reference.workspace_id
                    or existing.blob_id != available.reference.blob_id
                    or existing.blob_epoch != available.reference.epoch.value
                ):
                    raise ValueError("Preview artifact identity conflict")
                SqlAlchemyBlobReferenceLedger(session).register(
                    owner_kind=BlobReferenceOwnerKind.LIBRARY_ARTIFACT,
                    owner_id=existing.id,
                    blob=available.reference,
                )
                artifacts.append(existing)
                continue
            artifact = LibraryArtifact(
                id=artifact_id,
                workspace_id=version.workspace_id,
                library_item_version_id=version.id,
                attempt_id=attempt.id,
                stage="preview_generation",
                blob_workspace_id=available.reference.workspace_id,
                blob_id=available.reference.blob_id,
                blob_epoch=available.reference.epoch.value,
                role="preview",
                kind=receipt.kind,
                media_type=receipt.media_type,
                content_sha256=receipt.sha256,
                byte_size=receipt.byte_size,
                ordinal=receipt.ordinal,
            )
            artifact.preview_detail = LibraryArtifactPreviewDetail(
                renderer_contract=receipt.renderer_contract,
                delivery=receipt.delivery,
                rank=receipt.rank,
                filename=receipt.filename,
                width=receipt.width,
                height=receipt.height,
                duration_ms=receipt.duration_ms,
                page_count=receipt.page_count,
                metadata_json=thaw_json(receipt.metadata),
            )
            SqlAlchemyBlobReferenceLedger(session).register(
                owner_kind=BlobReferenceOwnerKind.LIBRARY_ARTIFACT,
                owner_id=artifact.id,
                blob=available.reference,
            )
            session.add(artifact)
            artifacts.append(artifact)
        session.flush()
        return tuple(artifacts)

    def _dependency_matches(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        version: LibraryItemVersion,
        attempt: LibraryAttempt,
    ) -> bool:
        detail = attempt.preview_detail
        if detail is None:
            return False
        if detail.input_kind == "source":
            return (
                detail.input_attempt_id is None
                and detail.input_artifact_id is None
                and version.content_hash.lower() == attempt.input_sha256
            )
        if (
            version.selected_conversion_attempt_id != detail.input_attempt_id
            or detail.input_artifact_id is None
        ):
            return False
        artifact = get_in_workspace(
            session,
            LibraryArtifact,
            workspace_id=workspace_id,
            resource_id=detail.input_artifact_id,
        )
        return (
            artifact is not None
            and artifact.library_item_version_id == version.id
            and artifact.attempt_id == detail.input_attempt_id
            and artifact.content_sha256 is not None
            and artifact.content_sha256.lower() == attempt.input_sha256
        )

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
            job_type=LIBRARY_PREVIEW_JOB_TYPE,
            trigger_type="library_preview",
            idempotency_key=f"{attempt.id}:{attempt.execution_generation}",
            target_ref={
                "library_preview_attempt_id": str(attempt.id),
                "library_preview_run_id": str(attempt.run_id),
                "library_item_version_id": str(attempt.library_item_version_id),
                "execution_generation": attempt.execution_generation,
            },
        )

    def _handle_error(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version: LibraryItemVersion,
        run: LibraryRun,
        attempt: LibraryAttempt,
        error: PreviewProviderError,
    ) -> PreviewExecution:
        now = utc_now()
        attempt.error_code = error.code
        attempt.safe_error_summary = error.safe_message
        attempt.finished_at = now
        if (
            error.retryable
            and attempt.transport_retry_count < MAX_PREVIEW_TRANSPORT_RETRIES
        ):
            attempt.transport_retry_count += 1
            attempt.execution_generation += 1
            attempt.status = "queued"
            attempt.finished_at = None
            run.status = "queued"
            session.flush()
            self._enqueue_attempt(
                session=session,
                workspace_id=workspace_id,
                attempt=attempt,
            )
            return self._execution(attempt, run, "requeued")

        timed_out = error.code in {"preview_timeout", "timed_out"}
        attempt.status = (
            "unsupported"
            if error.code == "unsupported"
            else "timed_out"
            if timed_out
            else "failed"
        )
        next_index = attempt.chain_index + 1
        if error.fallback_allowed and next_index < len(run.provider_chain_json):
            next_attempt = self._new_attempt(
                run=run,
                snapshot=run.provider_chain_json[next_index],
                chain_index=next_index,
                input_kind=attempt.preview_detail.input_kind,  # type: ignore[arg-type]
                input_conversion_attempt_id=attempt.preview_detail.input_attempt_id,
                input_artifact_id=attempt.preview_detail.input_artifact_id,
                input_sha256=attempt.input_sha256,
                parent_attempt_id=attempt.id,
            )
            session.add(next_attempt)
            run.status = "queued"
            if version.selected_preview_attempt_id is None:
                version.preview_status = "queued"
            session.flush()
            self._enqueue_attempt(
                session=session,
                workspace_id=workspace_id,
                attempt=next_attempt,
            )
            return PreviewExecution(
                attempt_id=attempt.id,
                run_id=run.id,
                version_id=version.id,
                status="fallback_queued",
                next_attempt_id=next_attempt.id,
            )
        run.status = "timed_out" if timed_out else "failed"
        run.finished_at = now
        if version.selected_preview_attempt_id is None:
            version.preview_status = "timed_out" if timed_out else "failed"
        session.flush()
        return self._execution(
            attempt,
            run,
            "timed_out" if timed_out else "failed",
        )

    @staticmethod
    def _cancel_stale(
        attempt: LibraryAttempt,
        run: LibraryRun,
        version: LibraryItemVersion,
    ) -> PreviewExecution:
        now = utc_now()
        attempt.status = "cancelled"
        attempt.error_code = "preview_dependency_stale"
        attempt.safe_error_summary = "The Preview input dependency changed."
        attempt.finished_at = now
        run.status = "cancelled"
        run.finished_at = now
        if version.selected_preview_attempt_id is None:
            version.preview_status = "unavailable"
        return PreviewExecution(
            attempt_id=attempt.id,
            run_id=run.id,
            version_id=version.id,
            status="stale",
        )

    @staticmethod
    def _may_advance_item_preview_pointer(
        session: Session,
        *,
        workspace_id: UUID,
        item: LibraryItem,
        version: LibraryItemVersion,
    ) -> bool:
        if item.current_preview_version_id is None:
            return True
        current = get_in_workspace(
            session,
            LibraryItemVersion,
            workspace_id=workspace_id,
            resource_id=item.current_preview_version_id,
        )
        return (
            current is not None
            and current.library_item_id == item.id
            and version.version_no >= current.version_no
        )

    @staticmethod
    def _lock_lineage(
        session: Session,
        *,
        workspace_id: UUID,
        attempt_id: UUID,
    ):
        seed_attempt = get_in_workspace(
            session,
            LibraryAttempt,
            workspace_id=workspace_id,
            resource_id=attempt_id,
        )
        if seed_attempt is not None and seed_attempt.stage != "preview_generation":
            seed_attempt = None
        seed_version = (
            get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=seed_attempt.library_item_version_id,
            )
            if seed_attempt is not None
            else None
        )
        if seed_attempt is None or seed_version is None:
            return None
        item = session.scalar(
            select(LibraryItem)
            .where(LibraryItem.id == seed_version.library_item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        version = session.scalar(
            select(LibraryItemVersion)
            .where(LibraryItemVersion.id == seed_version.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        run = session.scalar(
            select(LibraryRun)
            .where(
                LibraryRun.id == seed_attempt.run_id,
                LibraryRun.stage == "preview_generation",
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        attempt = session.scalar(
            select(LibraryAttempt)
            .where(
                LibraryAttempt.id == attempt_id,
                LibraryAttempt.stage == "preview_generation",
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            item is None
            or version is None
            or run is None
            or attempt is None
            or run.library_item_version_id != version.id
            or attempt.run_id != run.id
            or attempt.library_item_version_id != version.id
        ):
            return None
        return item, version, run, attempt

    @staticmethod
    def _execution(
        attempt,
        run,
        status: str,
        *,
        selected: bool = False,
        next_attempt_id: UUID | None = None,
    ) -> PreviewExecution:
        return PreviewExecution(
            attempt_id=attempt.id,
            run_id=run.id,
            version_id=attempt.library_item_version_id,
            status=status,  # type: ignore[arg-type]
            selected=selected,
            next_attempt_id=next_attempt_id,
        )


def _json_snapshot(value: object) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Preview recipe must be finite JSON") from exc
    if type(decoded) is not dict:
        raise ValueError("Preview recipe must be a JSON object")
    return decoded


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (TypeError, ValueError, AttributeError):
        return None


def _str_or_none(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _security_blocked(session: Session, reference: BlobReference) -> bool:
    return (
        session.get(
            SecurityBlockedBlobRow,
            {
                "workspace_id": reference.workspace_id,
                "blob_id": reference.blob_id,
                "blob_epoch": reference.epoch.value,
            },
        )
        is not None
    )


__all__ = [
    "LibraryPreviewOrchestrator",
    "MAX_PREVIEW_TRANSPORT_RETRIES",
    "PreviewExecution",
    "ScheduledPreview",
]
