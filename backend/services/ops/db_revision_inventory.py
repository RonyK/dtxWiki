"""Neutral Phase-0 database revision evidence collection."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, get_args

_REVISION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
#: The two revisions published in 0.1.1. Their files are frozen (see
#: backend/migrations/frozen_revisions.json); every later revision descends
#: from ``0002_epic1`` in one linear chain.
RELEASED_BASE_REVISION = "0001_initial_release"
RELEASED_EPIC1_REVISION = "0002_epic1"
#: Identifiers of the pre-rebaseline chain. A database stamped with one of
#: them needs an operator-led conversion, so the classifier never accepts
#: them, and no new revision may reuse one (tests/packaging enforces that).
RETIRED_REVISION_IDS = frozenset(
    {
        "0003_oauth_consent_lifetimes",
        "0004_workspace_member_foundation",
        "0005_library_multiformat_conversion",
        "0005_audit_workspace_delete",
        "0006_review_summary_code",
        "0007_context_orphan_demotion",
        "0008_mcp_access_lifecycle",
        "0009_media_blob_foundation",
        "0010_context_public_exposure",
        "0011_library_preview_lifecycle",
        "0012_context_legacy_cutover",
        "0012_graph_view_observations",
        "0013_context_content_hash_fix",
        "0014_job_failure_notification",
        "0015_consolidated",
    }
)
_UNSAFE_CONSISTENCIES = {
    "multiple_heads",
    "partial_schema",
    "stamp_marker_conflict",
    "unknown_revision",
    "manifest_revision_conflict",
    "manifest_changed_during_probe",
    "manifest_snapshot_incomplete",
    "unversioned",
}

# ``0002_epic1`` was republished as the one and only Epic-1 migration after an
# earlier, much smaller migration used the same revision identifier. The
# Alembic stamp alone therefore cannot prove that a database has the current
# schema. Keep this fingerprint made only of final-schema objects that are
# absent from both the immutable 0001 baseline and the retired 0002 shape.
#
# The content-hash repair in the consolidated migration is intentionally not
# represented here: it is a data transform, not a stable schema marker. Its
# semantics remain covered by migration tests rather than a startup table scan.
FINAL_EPIC1_0002_MARKERS = (
    "table:workspace_job_hint",
    "column:agent_input_bundle_item.workspace_id",
    "column:agent_page_version.workspace_id",
    "column:ask_turn.workspace_id",
    "column:ask_turn_citation.workspace_id",
    "column:change_operation.workspace_id",
    "column:chunk_embedding.workspace_id",
    "column:context_citation.workspace_id",
    "column:context_claim.workspace_id",
    "column:context_page_version.workspace_id",
    "column:library_artifact.workspace_id",
    # A-1: library_conversion_run/
    # attempt and library_preview_run/attempt/artifact are retired in favor
    # of the unified library_run/library_attempt (library_artifact keeps its
    # name). library_run/library_attempt never existed under any earlier
    # revision shape, so their workspace_id columns are unambiguous
    # fingerprints of the current final schema.
    "column:library_attempt.workspace_id",
    "column:library_run.workspace_id",
    "column:library_item_version.workspace_id",
    "column:library_projection_outbox.workspace_id",
    "column:library_semantic_overlay.workspace_id",
    "column:mcp_chat_artifact_link.workspace_id",
    "column:mcp_chat_tool_call_artifact.workspace_id",
    "column:page_provenance.workspace_id",
    "column:proposal_source_item.workspace_id",
    "column:review_decision.workspace_id",
    "column:context_page_version.block_map",
    "column:context_page_version.reconcile_manifest",
    "column:context_page_version.merged_into_version_id",
    "column:mcp_chat_artifact_link.last_read_version_id",
    "column:mcp_chat_artifact_link.last_read_root_hash",
    "column:mcp_chat_artifact_link.last_read_at",
    # Chats edit receipts (the MCP edit-failure fix): a deployed 0002 without
    # it must stop at preflight, not fail every traced MCP call at runtime.
    "column:mcp_tool_call.result_detail",
    "column:library_attempt_conversion_detail.manual_retry_disposition",
    "column:library_attempt_conversion_detail.disposition_reason",
    "column:email_outbox.source_job_type",
    "table:media_blob_reference",
    "column:media_blob_reference.workspace_id",
    "column:media_blob_reference.owner_kind",
    "column:media_blob_reference.owner_id",
    "column:media_blob_reference.blob_id",
    "column:media_blob_reference.blob_epoch",
    "table:media_blob_gc_hold",
    "column:media_blob_gc_hold.workspace_id",
    "column:media_blob_gc_hold.producer_kind",
    "column:media_blob_gc_hold.producer_id",
    "column:media_blob_gc_hold.producer_generation",
    "column:media_blob_gc_hold.blob_id",
    "column:media_blob_gc_hold.blob_epoch",
    "table:media_blob_gc_candidate",
    "column:media_blob_gc_candidate.workspace_id",
    "column:media_blob_gc_candidate.blob_id",
    "column:media_blob_gc_candidate.blob_epoch",
    "column:media_blob_gc_candidate.reason",
    "column:media_blob_gc_candidate.eligible_at",
)

ProbeFailureCategory = Literal[
    "unavailable",
    "invalid_revision",
    "snapshot_incomplete",
    "checkpoint_binding_mismatch",
    "checkpoint_binding_unverifiable",
    "contract_violation",
]
_PROBE_FAILURE_CATEGORIES = frozenset(get_args(ProbeFailureCategory))


@dataclass(frozen=True)
class MarkerSnapshot:
    state: Literal["complete", "absent", "partial"]
    present: tuple[str, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class DatabaseSnapshot:
    dialect: Literal["sqlite", "postgresql"]
    locator: dict[str, object]
    read_only_mechanism: str
    read_only_verified: bool
    alembic_table_present: bool
    alembic_revisions: tuple[str, ...]
    library_0005: MarkerSnapshot
    context_0005: MarkerSnapshot
    context_0006: MarkerSnapshot
    context_0008: MarkerSnapshot
    media_0009: MarkerSnapshot
    public_exposure_0010: MarkerSnapshot
    final_epic1_0002: MarkerSnapshot


@dataclass(frozen=True)
class Sha256FileBinding:
    byte_size: int
    hexdigest: str = field(repr=False)


@dataclass(frozen=True)
class DatabaseCheckpointBinding:
    path: Path = field(repr=False)
    expected_main: Sha256FileBinding = field(repr=False)


@dataclass(frozen=True)
class LiveProbeRequest:
    target: str | Path = field(repr=False)


@dataclass(frozen=True)
class CheckpointProbeRequest:
    checkpoint: DatabaseCheckpointBinding = field(repr=False)


RevisionProbeRequest = LiveProbeRequest | CheckpointProbeRequest


class ProbeFailure(RuntimeError):
    """A sanitized probe failure whose code is safe for operator output."""

    def __init__(
        self,
        code: str,
        *,
        category: ProbeFailureCategory = "unavailable",
    ) -> None:
        if not isinstance(category, str) or category not in _PROBE_FAILURE_CATEGORIES:
            code = "database_probe_contract_violation"
            category = "contract_violation"
        super().__init__(code)
        self.code = code
        self.category = category

    @property
    def unsafe_evidence(self) -> bool:
        return self.category in {
            "invalid_revision",
            "checkpoint_binding_mismatch",
            "checkpoint_binding_unverifiable",
            "contract_violation",
        }


class BackupEvidenceFailure(RuntimeError):
    """A sanitized backup-evidence adapter failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LiveTargetRequest:
    name: str
    source_kind: str
    source_reference: str
    database_target: str | Path = field(repr=False)


@dataclass(frozen=True)
class ManifestRequest:
    name: str
    path: Path


@dataclass(frozen=True)
class InventoryRequest:
    live_targets: tuple[LiveTargetRequest, ...] = ()
    manifests: tuple[ManifestRequest, ...] = ()


@dataclass(frozen=True)
class VerifiedManifestEvidence:
    schema: int
    alembic_revision: str | None
    manifest_dir: Path = field(repr=False)
    database_checkpoint: DatabaseCheckpointBinding | None = field(repr=False)
    verification_token: str = field(repr=False)


@dataclass(frozen=True)
class TargetResult:
    name: str
    source: dict[str, str]
    status: str
    locator: dict[str, object] | None
    read_only: dict[str, object] | None
    alembic: dict[str, object] | None
    markers: dict[str, object] | None
    consistency: str | None
    historical_application: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ManifestResult:
    name: str
    path: str
    status: str
    schema: int | None
    alembic_revision: str | None
    integrity_verified: bool
    database_checkpoint_included: bool | None
    database_stamp_verified: bool
    database_revisions: tuple[str, ...]
    consistency: str | None
    historical_application: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class InventoryResult:
    schema_version: int
    generated_at: str
    command_version: str
    code: dict[str, object]
    summary: dict[str, object]
    targets: tuple[TargetResult, ...]
    manifest_evidence: tuple[ManifestResult, ...]
    exit_code: int = field(repr=False)

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("exit_code", None)
        return value


class RevisionInventoryProbe(Protocol):
    def probe(self, request: RevisionProbeRequest) -> DatabaseSnapshot: ...


class MigrationGraphSource(Protocol):
    def current_heads(self) -> tuple[str, ...]: ...

    def known_revisions(self) -> frozenset[str]: ...


class BackupEvidenceSource(Protocol):
    def verify(self, path: Path) -> VerifiedManifestEvidence: ...

    def reverify(self, evidence: VerifiedManifestEvidence) -> None: ...


def collect_inventory(
    request: InventoryRequest,
    *,
    probe: RevisionInventoryProbe,
    migration_graph: MigrationGraphSource,
    backup_evidence: BackupEvidenceSource,
    command_version: str,
    now: datetime | None = None,
) -> InventoryResult:
    code_heads = migration_graph.current_heads()
    known_revisions = migration_graph.known_revisions()
    targets = tuple(
        _collect_target(item, probe, known_revisions) for item in request.live_targets
    )
    manifests = tuple(
        _collect_manifest(item, probe, backup_evidence, known_revisions)
        for item in request.manifests
    )
    unsafe = (
        len(code_heads) != 1
        or any(item.consistency in _UNSAFE_CONSISTENCIES for item in targets)
        or any(item.consistency in _UNSAFE_CONSISTENCIES for item in manifests)
    )
    incomplete = any(item.status != "ok" for item in targets) or any(
        item.status != "ok" for item in manifests
    )
    exit_code = 4 if unsafe else 3 if incomplete else 0
    generated = now or datetime.now(UTC)
    return InventoryResult(
        schema_version=1,
        generated_at=generated.isoformat().replace("+00:00", "Z"),
        command_version=command_version,
        code={
            "alembic_heads": list(code_heads),
            "known_alternate_revisions": [
                "0005_library_multiformat_conversion",
                "0012_graph_view_observations",
            ],
        },
        summary={
            "targets_requested": len(request.live_targets) + len(request.manifests),
            "targets_ok": sum(item.status == "ok" for item in targets)
            + sum(item.status == "ok" for item in manifests),
            "targets_error": sum(item.status != "ok" for item in targets)
            + sum(item.status != "ok" for item in manifests),
            "targets_inconclusive": sum(
                item.consistency in _UNSAFE_CONSISTENCIES for item in targets
            )
            + sum(item.consistency in _UNSAFE_CONSISTENCIES for item in manifests),
            "implementation_start_evidence_complete": False,
        },
        targets=targets,
        manifest_evidence=manifests,
        exit_code=exit_code,
    )


def _collect_target(
    item: LiveTargetRequest,
    probe: RevisionInventoryProbe,
    known_revisions: Collection[str],
) -> TargetResult:
    source = {"kind": item.source_kind, "reference": item.source_reference}
    try:
        snapshot = probe.probe(LiveProbeRequest(target=item.database_target))
    except ProbeFailure as exc:
        consistency = "unknown_revision" if exc.unsafe_evidence else None
        return TargetResult(
            name=item.name,
            source=source,
            status="error",
            locator=None,
            read_only=None,
            alembic=None,
            markers=None,
            consistency=consistency,
            historical_application="unknown",
            errors=(exc.code,),
        )
    consistency = classify_revision_consistency(
        snapshot, known_revisions=known_revisions
    )
    return TargetResult(
        name=item.name,
        source=source,
        status="ok",
        locator=snapshot.locator,
        read_only={
            "mechanism": snapshot.read_only_mechanism,
            "verified": snapshot.read_only_verified,
        },
        alembic={
            "table_present": snapshot.alembic_table_present,
            "row_count": len(snapshot.alembic_revisions),
            "revisions": list(snapshot.alembic_revisions),
        },
        markers={
            "library_0005": _marker_dict(snapshot.library_0005),
            "context_0005": _marker_dict(snapshot.context_0005),
            "context_0006": _marker_dict(snapshot.context_0006),
            "context_0007": {"schema_detectable": False, "data_only": True},
            "context_0008": _marker_dict(snapshot.context_0008),
            "media_0009": _marker_dict(snapshot.media_0009),
            "public_exposure_0010": _marker_dict(
                snapshot.public_exposure_0010
            ),
            "final_epic1_0002": _marker_dict(snapshot.final_epic1_0002),
        },
        consistency=consistency,
        historical_application="unknown",
        errors=(),
    )


def classify_revision_consistency(
    snapshot: DatabaseSnapshot,
    *,
    known_revisions: Collection[str] | None = None,
) -> str:
    """Compare one Alembic stamp with the only supported migration shapes.

    Retired intermediate and alternate revision identifiers are deliberately
    not inferred from marker subsets. Once the graph is consolidated to
    0001 -> 0002, those stamps require an explicit operator-led conversion and
    remain unknown even when their schema resembles part of the final shape.

    ``known_revisions`` is the script directory the caller migrates with; the
    adapters that can read one pass it (``AlembicMigrationGraphSource``, the
    migration preflight, Alembic's env.py). A revision after ``0002_epic1`` is
    accepted when it is in that directory: released revisions are frozen, so
    unlike the republished ``0002_epic1`` its stamp names one schema. Such a
    database descends from the final 0002 shape, so a stamp over a schema
    that carries none of the 0002 fingerprint is still a conflict. Marker
    groups are not checked further, because a later revision may legitimately
    drop or reshape objects the fingerprint names. Without a script directory
    only the released pair is known.
    """

    if len(snapshot.alembic_revisions) > 1:
        return "multiple_heads"

    marker_states = (
        snapshot.library_0005.state,
        snapshot.context_0005.state,
        snapshot.context_0006.state,
        snapshot.context_0008.state,
        snapshot.media_0009.state,
        snapshot.public_exposure_0010.state,
        snapshot.final_epic1_0002.state,
    )
    if len(snapshot.alembic_revisions) != 1:
        if "partial" in marker_states:
            return "partial_schema"
        return (
            "stamp_marker_conflict"
            if any(state == "complete" for state in marker_states)
            else "unversioned"
        )

    revision = snapshot.alembic_revisions[0]
    if not is_revision_id(revision) or revision in RETIRED_REVISION_IDS:
        return "unknown_revision"
    if revision not in {RELEASED_BASE_REVISION, RELEASED_EPIC1_REVISION}:
        if known_revisions is None or revision not in known_revisions:
            return "unknown_revision"
        if snapshot.final_epic1_0002.state == "absent":
            return "stamp_marker_conflict"
        return "consistent"

    expected_state = (
        "absent" if revision == RELEASED_BASE_REVISION else "complete"
    )
    # Preserve the more diagnostic partial-schema result at the immutable 0001
    # boundary. At republished 0002 every missing/partial fingerprint member is
    # specifically a stamp-to-schema conflict.
    if revision == RELEASED_BASE_REVISION and "partial" in marker_states:
        return "partial_schema"
    if any(state != expected_state for state in marker_states):
        return "stamp_marker_conflict"
    return "consistent"


def _collect_manifest(
    item: ManifestRequest,
    probe: RevisionInventoryProbe,
    backup_evidence: BackupEvidenceSource,
    known_revisions: Collection[str],
) -> ManifestResult:
    display_path = str(item.path.absolute())
    try:
        verified_manifest = backup_evidence.verify(item.path)
    except BackupEvidenceFailure:
        return ManifestResult(
            name=item.name,
            path=display_path,
            status="error",
            schema=None,
            alembic_revision=None,
            integrity_verified=False,
            database_checkpoint_included=None,
            database_stamp_verified=False,
            database_revisions=(),
            consistency=None,
            historical_application="unknown",
            errors=("manifest_invalid",),
        )

    schema = verified_manifest.schema
    revision = verified_manifest.alembic_revision
    includes_database = verified_manifest.database_checkpoint is not None
    database_revisions: tuple[str, ...] = ()
    database_stamp_verified = False
    consistency: str | None = None
    historical_application = (
        "recorded_in_integrity_verified_manifest_only" if revision else "unknown"
    )
    errors: tuple[str, ...] = ()
    status = "ok"

    if includes_database:
        database_checkpoint = verified_manifest.database_checkpoint
        assert database_checkpoint is not None
        snapshot: DatabaseSnapshot | None = None
        probe_failure: ProbeFailure | None = None
        try:
            snapshot = probe.probe(
                CheckpointProbeRequest(checkpoint=database_checkpoint)
            )
        except ProbeFailure as exc:
            probe_failure = exc
        try:
            backup_evidence.reverify(verified_manifest)
        except BackupEvidenceFailure:
            return ManifestResult(
                name=item.name,
                path=display_path,
                status="error",
                schema=schema,
                alembic_revision=revision,
                integrity_verified=False,
                database_checkpoint_included=True,
                database_stamp_verified=False,
                database_revisions=(),
                consistency="manifest_changed_during_probe",
                historical_application="unknown",
                errors=("manifest_changed_during_probe",),
            )
        if probe_failure is not None:
            snapshot_incomplete = probe_failure.category == "snapshot_incomplete"
            invalid_revision = probe_failure.category == "invalid_revision"
            contract_violation = probe_failure.category == "contract_violation"
            binding_mismatch = probe_failure.category == "checkpoint_binding_mismatch"
            binding_unverifiable = (
                probe_failure.category == "checkpoint_binding_unverifiable"
            )
            if binding_mismatch:
                failed_consistency = "manifest_snapshot_incomplete"
                failure_code = "manifest_database_checkpoint_digest_mismatch"
            elif binding_unverifiable:
                failed_consistency = "manifest_snapshot_incomplete"
                failure_code = "manifest_database_checkpoint_digest_unverifiable"
            elif contract_violation:
                failed_consistency = "unknown_revision"
                failure_code = "manifest_database_probe_contract_violation"
            elif invalid_revision:
                failed_consistency = "unknown_revision"
                failure_code = "manifest_database_invalid_alembic_revision"
            elif snapshot_incomplete:
                failed_consistency = "manifest_snapshot_incomplete"
                failure_code = "manifest_database_wal_snapshot_unverifiable"
            else:
                failed_consistency = None
                failure_code = "manifest_database_probe_failed"
            return ManifestResult(
                name=item.name,
                path=display_path,
                status="error",
                schema=schema,
                alembic_revision=revision,
                integrity_verified=True,
                database_checkpoint_included=True,
                database_stamp_verified=False,
                database_revisions=(),
                consistency=failed_consistency,
                historical_application="unknown",
                errors=(failure_code,),
            )
        assert snapshot is not None
        database_revisions = snapshot.alembic_revisions
        consistency = classify_revision_consistency(
            snapshot, known_revisions=known_revisions
        )
        if revision is not None and database_revisions != (revision,):
            status = "error"
            consistency = "manifest_revision_conflict"
            errors = ("manifest_revision_mismatch",)
        elif consistency in _UNSAFE_CONSISTENCIES:
            status = "error"
            errors = (f"manifest_database_{consistency}",)
        elif len(database_revisions) != 1:
            status = "error"
            errors = ("manifest_database_revision_unavailable",)
        else:
            database_stamp_verified = True
            historical_application = (
                "observed_in_integrity_verified_database_checkpoint"
            )
    else:
        status = "error"
        errors = ("manifest_database_checkpoint_missing",)

    return ManifestResult(
        name=item.name,
        path=display_path,
        status=status,
        schema=schema,
        alembic_revision=revision,
        integrity_verified=True,
        database_checkpoint_included=includes_database,
        database_stamp_verified=database_stamp_verified,
        database_revisions=database_revisions,
        consistency=consistency,
        historical_application=historical_application,
        errors=errors,
    )


def _marker_dict(marker: MarkerSnapshot) -> dict[str, object]:
    return {
        "state": marker.state,
        "present": list(marker.present),
        "missing": list(marker.missing),
    }


def is_revision_id(value: object) -> bool:
    return isinstance(value, str) and _REVISION_ID.fullmatch(value) is not None
