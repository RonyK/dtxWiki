from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.domain.input_policy import validate_text
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.schemas.context_edit import (
    DeleteBlockOperation,
    EditOperation,
    InsertAfterOperation,
    InsertRowOperation,
    ReplaceBlockOperation,
    ResolveConflictOperation,
    without_legacy_operation_fields,
)
from backend.schemas.context_reconciliation import (
    ContextReconciliationBlock,
    ContextReconciliationBranch,
    ContextReconciliationCleanChange,
    ContextReconciliationConflict,
    ContextReconciliationDetail,
    ContextReconciliationOperationEffect,
    ContextReconciliationPage,
    ContextReconciliationResolution,
    ContextReconciliationSide,
    ContextReconciliationVersion,
)
from backend.services.audit_service import AuditService
from backend.services.context_editing import ContextEditError, apply_operations
from backend.services.context_push import (
    authored_sides_by_target,
    build_conflict_details,
    changed_ids_between_maps,
    hash_map,
    reconciliation_target_sets,
    resolve_target_id_between_maps,
    target_descriptor,
    target_ids_for_operations,
    text_by_id,
)
from backend.services.context_visibility import editable_context_version_id
from backend.services.db_write_intent import acquire_write_intent
from backend.services.lint.autofix import safe_normalize_markdown_legacy_v1
from backend.services.read_errors import (
    ReadNotFoundError,
    ReadServiceError,
    ReadValidationError,
)
from backend.utils.block_identity import (
    BlockMapTransitionError,
    validated_block_map_for_version,
)
from backend.utils.block_map import compute_blocks
from backend.utils.content_hash import content_hash as calculate_content_hash
from backend.utils.markdown_parser import parse_markdown

_DIVERGED_MANIFEST_SCHEMA = "context_push_diverged_manifest.v1"
_BODY_TRANSFORM_SCHEMA = "context_body_transform.v1"
_EDIT_OPERATIONS_ADAPTER = TypeAdapter(list[EditOperation])


def build_reconciliation_normalization_proof(
    original: str, final: str
) -> dict[str, object]:
    """Record the exact save-time transformation, without executable fixers.

    A single replacement between the common prefix and suffix is linear to
    construct and stores at most one body's worth of replacement text.  The
    source hash binds it to the authored operations; the final hash binds it
    to the persisted branch.  Reconciliation can therefore verify a branch
    after a fixer is changed, disabled, or removed.
    """

    start = 0
    while start < min(len(original), len(final)) and original[start] == final[start]:
        start += 1
    original_end = len(original)
    final_end = len(final)
    while (
        original_end > start
        and final_end > start
        and original[original_end - 1] == final[final_end - 1]
    ):
        original_end -= 1
        final_end -= 1
    return {
        "applied": original != final,
        "pre_normalize_body_sha256": _sha256_text(original),
        "final_body_sha256": _sha256_text(final),
        "transform": {
            "schema": _BODY_TRANSFORM_SCHEMA,
            "start": start,
            "end": original_end,
            "replacement": final[start:final_end],
        },
    }


def _replay_body_transform(body: str, proof: object) -> str:
    if not isinstance(proof, dict) or set(proof) != {
        "schema", "start", "end", "replacement"
    }:
        raise ValueError("invalid body transformation proof")
    start, end, replacement = proof["start"], proof["end"], proof["replacement"]
    if (
        proof["schema"] != _BODY_TRANSFORM_SCHEMA
        or type(start) is not int
        or type(end) is not int
        or not 0 <= start <= end <= len(body)
        or not isinstance(replacement, str)
    ):
        raise ValueError("invalid body transformation range")
    return body[:start] + replacement + body[end:]


def _replay_normalization_proof(body: str, proof: object) -> str:
    if (
        not isinstance(proof, dict)
        or not isinstance(proof.get("applied"), bool)
        or not _is_sha256(proof.get("pre_normalize_body_sha256"))
        or not _is_sha256(proof.get("final_body_sha256"))
        or _sha256_text(body) != proof["pre_normalize_body_sha256"]
    ):
        raise ValueError("normalization source proof is invalid")
    final = _replay_body_transform(body, proof["transform"])
    if (
        proof["applied"] != (final != body)
        or _sha256_text(final) != proof["final_body_sha256"]
    ):
        raise ValueError("normalization result proof is invalid")
    return final


@dataclass(frozen=True)
class VerifiedReconciliationManifest:
    base: ContextPageVersion
    original_head: ContextPageVersion
    base_map: list[dict]
    branch_map: list[dict]
    active_operations: list[EditOperation]


@dataclass(frozen=True)
class ReconciliationSnapshot:
    page: ContextPage
    branch: ContextPageVersion
    base: ContextPageVersion
    original_head: ContextPageVersion
    head: ContextPageVersion
    base_map: list[dict]
    branch_map: list[dict]
    head_map: list[dict]
    active_operations: list[EditOperation]
    conflict_ids: frozenset[str]
    already_satisfied_ids: frozenset[str]
    head_advance_count: int


type SideDict = dict[str, object]


def reconciliation_branch_is_available(version: ContextPageVersion) -> bool:
    manifest = version.reconcile_manifest
    return (
        version.graph_status == "branched"
        and version.version_status == "draft"
        and version.change_set_id is not None
        and isinstance(manifest, dict)
        and manifest.get("schema") == _DIVERGED_MANIFEST_SCHEMA
        and manifest.get("branch_version_id") == str(version.id)
    )


def verified_reconciliation_branch_is_available(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
    actor_kind: str | None = None,
    actor_id: str | None = None,
) -> bool:
    """Fail-closed availability for list/history surfaces.

    Pass ``actor_kind``/``actor_id`` (the viewing actor) so an "available"
    badge cannot promise a detail view that would then refuse to disclose a
    different author's private editing head -- see
    ``load_reconciliation_snapshot``'s docstring.
    """

    if not reconciliation_branch_is_available(version):
        return False
    try:
        load_reconciliation_snapshot(
            session,
            page=page,
            branch_id=version.id,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
    except ReadServiceError:
        return False
    return True


def load_verified_reconciliation_manifest(
    session: Session,
    *,
    page: ContextPage,
    branch: ContextPageVersion,
) -> VerifiedReconciliationManifest:
    """Verify that durable reconciliation provenance recreates ``branch``.

    The manifest is an executable write contract.  Merely validating operation
    syntax would let a corrupted clean operation be displayed from the stored
    branch body while commit later applied different bytes.  Reapply both the
    original operation list and the active clean/conflict partition to the
    durable base, then prove the exact pre-normalization and final bodies.
    """

    manifest = branch.reconcile_manifest
    try:
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != _DIVERGED_MANIFEST_SCHEMA
            or manifest.get("branch_version_id") != str(branch.id)
        ):
            raise ValueError("manifest identity mismatch")
        base_identity = manifest["base"]
        head_identity = manifest["head"]
        branch_identity = manifest["branch"]
        normalization = manifest["normalization"]
        if not all(
            isinstance(value, dict)
            for value in (
                base_identity,
                head_identity,
                branch_identity,
                normalization,
            )
        ):
            raise ValueError("manifest provenance object is invalid")
        base = session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.workspace_id == page.workspace_id,
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.id == UUID(base_identity["version_id"]),
            )
        )
        original_head = session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.workspace_id == page.workspace_id,
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.id == UUID(head_identity["version_id"]),
            )
        )
        original_operations = _EDIT_OPERATIONS_ADAPTER.validate_python(
            without_legacy_operation_fields(manifest["original_operations"])
        )
        clean_operations = _EDIT_OPERATIONS_ADAPTER.validate_python(
            without_legacy_operation_fields(manifest["clean_operations"])
        )
        conflict_operations = _EDIT_OPERATIONS_ADAPTER.validate_python(
            without_legacy_operation_fields(manifest["conflict_operations"])
        )
        active_operations = [*clean_operations, *conflict_operations]
        if not original_operations or not active_operations:
            raise ValueError("empty manifest operation set")
        if not isinstance(manifest["conflicts"], list) or not manifest["conflicts"]:
            raise ValueError("empty manifest conflict set")
        normalized = normalization["applied"]
        pre_normalize_sha = normalization["pre_normalize_body_sha256"]
        final_sha = normalization["final_body_sha256"]
        if (
            not isinstance(normalized, bool)
            or not _is_sha256(pre_normalize_sha)
            or not _is_sha256(final_sha)
        ):
            raise ValueError("manifest normalization proof is invalid")
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ReadValidationError(
            "diverged branch conflict manifest is invalid",
            error_code="reconcile_target_invalid",
        ) from exc

    if (
        base is None
        or original_head is None
        or base.workspace_id != page.workspace_id
        or original_head.workspace_id != page.workspace_id
        or base.page_id != page.id
        or original_head.page_id != page.id
        or base.graph_status != "linear"
        or original_head.graph_status != "linear"
        or branch.workspace_id != page.workspace_id
        or branch.page_id != page.id
        or branch.parent_version_id != base.id
        or base_identity.get("content_hash") != base.content_hash
        or head_identity.get("content_hash") != original_head.content_hash
        or branch_identity.get("version_id") != str(branch.id)
        or branch_identity.get("content_hash") != branch.content_hash
        or branch_identity.get("parent_version_id") != str(branch.parent_version_id)
        or calculate_content_hash(base.body_md) != base.content_hash
        or calculate_content_hash(original_head.body_md) != original_head.content_hash
        or calculate_content_hash(branch.body_md) != branch.content_hash
        or _sha256_text(branch.body_md) != final_sha
    ):
        raise ReadValidationError(
            "diverged branch conflict manifest provenance is invalid",
            error_code="reconcile_target_invalid",
        )
    try:
        base_map = validated_block_map_for_version(base.block_map, base.body_md)
        branch_map = validated_block_map_for_version(branch.block_map, branch.body_md)
        original_replay, _original_edits = apply_operations(
            base.body_md,
            None,
            original_operations,
            block_map=base_map,
        )
        active_replay, _active_edits = apply_operations(
            base.body_md,
            None,
            active_operations,
            block_map=base_map,
        )
    except (BlockMapTransitionError, ContextEditError, ValueError) as exc:
        raise ReadValidationError(
            "diverged branch manifest cannot be replayed from its durable base",
            error_code="reconcile_target_invalid",
        ) from exc
    if _sha256_text(original_replay) != pre_normalize_sha:
        raise ReadValidationError(
            "diverged branch manifest operations do not recreate the branch",
            error_code="reconcile_target_invalid",
        )
    try:
        if "transform" in normalization:
            original_final = _replay_normalization_proof(
                original_replay, normalization
            )
            # Normalization-only operations are intentionally omitted from
            # the active partition. Its distinct source must independently
            # prove the same persisted result, without rerunning today's fixes.
            replayed_final = _replay_normalization_proof(
                active_replay, normalization["active"]
            )
        else:
            # Older manifests only recorded whether the original format-only
            # normalizer changed the body. Keep that exact algorithm for them;
            # current policy, fixers, and formatting semantics cannot replay it.
            original_final = (
                safe_normalize_markdown_legacy_v1(original_replay)
                if normalized and original_replay
                else original_replay
            )
            replayed_final = (
                safe_normalize_markdown_legacy_v1(active_replay)
                if normalized and active_replay
                else active_replay
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReadValidationError(
            "diverged branch body transformation proof is invalid",
            error_code="reconcile_target_invalid",
        ) from exc
    if (
        original_final != branch.body_md
        or replayed_final != branch.body_md
        or _sha256_text(replayed_final) != final_sha
    ):
        raise ReadValidationError(
            "diverged branch normalization proof does not recreate the branch",
            error_code="reconcile_target_invalid",
        )
    return VerifiedReconciliationManifest(
        base=base,
        original_head=original_head,
        base_map=base_map,
        branch_map=branch_map,
        active_operations=(
            _stored_authored_operations(
                active_operations, base_map, branch_map, branch.body_md
            )
            if normalized
            else active_operations
        ),
    )


def _stored_authored_operations(
    operations: list[EditOperation],
    base_map: list[dict],
    branch_map: list[dict],
    body: str,
) -> list[EditOperation]:
    """Use the saved contribution when replaying a verified branch.

    Raw authored operations prove the manifest's origin, but their content
    predates save-time fixing. Restoring or carrying them must use the bytes
    displayed from the branch even when today's policy disables that fixer.
    """

    lookup = text_by_id(body, branch_map)
    base_ids = set(hash_map(base_map))
    parsed = parse_markdown(body)
    result: list[EditOperation] = []
    for operation in operations:
        content: str | None = None
        if isinstance(operation, ReplaceBlockOperation):
            authored_id = resolve_target_id_between_maps(
                base_map, branch_map, operation.block
            )
            entry = lookup.get(authored_id)
            content = entry[3] if entry is not None else None
        elif isinstance(operation, InsertAfterOperation):
            anchor = resolve_target_id_between_maps(
                base_map, branch_map, operation.block
            )
            anchor_index = next(
                (i for i, block in enumerate(branch_map) if block["id"] == anchor),
                None,
            )
            inserted: list[dict] = []
            if anchor_index is not None:
                for block in branch_map[anchor_index + 1 :]:
                    if block["id"] in base_ids:
                        break
                    inserted.append(block)
            if inserted:
                first, last = lookup[inserted[0]["id"]], lookup[inserted[-1]["id"]]
                start = parsed.char_offset(first[1] - 1)
                end = parsed.char_offset(last[1] - 1) + len(last[3])
                content = body[start:end]
        elif isinstance(operation, InsertRowOperation):
            sides = authored_sides_by_target(
                operations=[operation],
                base_block_map=base_map,
                authored_body_md=body,
                authored_block_map=branch_map,
            )
            content = sides.get(operation.row, {}).get("text")
        elif isinstance(operation, DeleteBlockOperation):
            result.append(operation)
            continue
        else:
            raise ReadValidationError(
                "diverged branch has an unaddressable authored operation",
                error_code="reconcile_target_invalid",
            )
        if not isinstance(content, str) or not content.strip():
            raise ReadValidationError(
                "diverged branch has no durable authored operation content",
                error_code="reconcile_target_invalid",
            )
        result.append(operation.model_copy(update={"content": content}))
    return result


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def load_reconciliation_snapshot(
    session: Session,
    *,
    page: ContextPage,
    branch_id: UUID,
    actor_kind: str | None = None,
    actor_id: str | None = None,
) -> ReconciliationSnapshot:
    """Load and verify every server-owned input to the workbench.

    The branch manifest is correctness provenance, not presentation metadata.
    Any malformed/cross-page identity fails closed without returning a partial
    map or allowing the client to infer one from Markdown.

    ``head`` is resolved from ``editable_context_version_id(page)``, the same
    page-global pointer that -- since P8.1 -- can durably name a DIFFERENT
    author's private "editing" row (see ``page_service._editable_version_id_
    for_actor``'s docstring for the general shape of this problem). Every
    other reconciliation-detail field is safe to disclose regardless of actor
    (``branch`` is required to be a submitted, shared "draft" row -- see the
    availability check above -- never someone's private editing row), but
    ``head``'s body/block-map feed directly into the returned conflict/diff
    projection. When ``actor_kind``/``actor_id`` are given and the resolved
    head is a different author's "editing" row, treat it exactly like "no
    reconcilable head" (fail closed) rather than disclosing it. Callers that
    only need a boolean/summary signal (list-surface availability checks) may
    omit the actor and accept the unmasked page-global head.
    """

    branch = session.scalar(
        select(ContextPageVersion).where(
            ContextPageVersion.workspace_id == page.workspace_id,
            ContextPageVersion.page_id == page.id,
            ContextPageVersion.id == branch_id,
        )
    )
    if (
        branch is None
        or branch.workspace_id != page.workspace_id
        or branch.page_id != page.id
        or branch.graph_status != "branched"
        or branch.version_status != "draft"
        or branch.change_set_id is None
    ):
        raise ReadNotFoundError("reconciliation not found")
    verified_manifest = load_verified_reconciliation_manifest(
        session,
        page=page,
        branch=branch,
    )
    base = verified_manifest.base
    original_head = verified_manifest.original_head
    active_operations = verified_manifest.active_operations
    head_id = editable_context_version_id(page)
    head = (
        session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.workspace_id == page.workspace_id,
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.id == head_id,
            )
        )
        if head_id is not None
        else None
    )
    # Only masked when an actor is actually supplied -- a caller that omits
    # both (list/summary availability checks with no per-finding disclosure)
    # keeps seeing the unmasked page-global head, matching their existing
    # behavior; a caller that DOES supply an actor (the detail projection)
    # must never resolve `head` to a different author's private row.
    head_is_foreign_private_edit = (
        head is not None
        and head.version_status == "editing"
        and (actor_kind is not None or actor_id is not None)
        and (
            head.created_by_actor_kind != actor_kind
            or head.created_by_actor_id != actor_id
        )
    )
    if (
        head is None
        or head.workspace_id != page.workspace_id
        or head.page_id != page.id
        or head.graph_status != "linear"
        or head_is_foreign_private_edit
    ):
        raise ReadValidationError(
            "page has no reconcilable linear head",
            error_code="reconcile_target_invalid",
        )
    head_advance_count = _lineage_distance(
        session,
        ancestor=original_head,
        descendant=head,
    )
    base_map = verified_manifest.base_map
    branch_map = verified_manifest.branch_map
    try:
        head_map = validated_block_map_for_version(head.block_map, head.body_md)
    except BlockMapTransitionError as exc:
        raise ReadValidationError(
            "current reconciliation head block map is invalid",
            error_code="reconcile_target_invalid",
        ) from exc
    active_target_ids = target_ids_for_operations(active_operations)
    if active_target_ids is None or any(
        target_descriptor(base_map, target_id) is None
        for target_id in active_target_ids
    ):
        raise ReadValidationError(
            "diverged branch manifest contains an unaddressable operation",
            error_code="reconcile_target_invalid",
        )
    target_sets = reconciliation_target_sets(
        operations=active_operations,
        base_block_map=base_map,
        authored_body_md=branch.body_md,
        authored_block_map=branch_map,
        head_block_map=head_map,
        base_body_md=base.body_md,
        head_body_md=head.body_md,
    )
    return ReconciliationSnapshot(
        page=page,
        branch=branch,
        base=base,
        original_head=original_head,
        head=head,
        base_map=base_map,
        branch_map=branch_map,
        head_map=head_map,
        active_operations=active_operations,
        conflict_ids=target_sets.conflict_ids,
        already_satisfied_ids=target_sets.already_satisfied_ids,
        head_advance_count=head_advance_count,
    )


def resolution_operations(
    snapshot: ReconciliationSnapshot,
    resolutions: list[ContextReconciliationResolution],
) -> list[ResolveConflictOperation]:
    current_hashes = hash_map(snapshot.head_map)
    operations: list[ResolveConflictOperation] = []
    for resolution in resolutions:
        current_id = resolve_target_id_between_maps(
            snapshot.base_map,
            snapshot.head_map,
            resolution.block_id,
        )
        expected = current_hashes.get(current_id) if current_id is not None else None
        # The browser received this hash from GET. Checking it again makes a
        # stale block-level decision fail even if a malformed client attempts
        # to submit its own map/diff. The page-level expected HEAD remains the
        # atomic CAS.
        if resolution.expected_hash != expected:
            raise ReadValidationError(
                "resolution expected_hash does not match the current server map",
                error_code="reconcile_target_invalid",
            )
        operations.append(
            ResolveConflictOperation(
                op="resolve_conflict",
                block=resolution.block_id,
                resolution=resolution.choice,
                expected=expected,
                content=resolution.content,
            )
        )
    return operations


def project_reconciliation_detail(
    session: Session,
    snapshot: ReconciliationSnapshot,
) -> ContextReconciliationDetail:
    raw_conflicts = build_conflict_details(
        conflicting_ids=snapshot.conflict_ids,
        base_body_md=snapshot.base.body_md,
        base_block_map=snapshot.base_map,
        head_body_md=snapshot.head.body_md,
        head_block_map=snapshot.head_map,
        head_version_id=snapshot.head.id,
        head_actor_kind=snapshot.head.actor_type,
        head_actor_display_name=snapshot.head.actor_display_name,
        head_created_at=snapshot.head.created_at,
        operations=snapshot.active_operations,
        authored_body_md=snapshot.branch.body_md,
        authored_block_map=snapshot.branch_map,
    )
    conflicts = [_project_conflict(session, snapshot, raw) for raw in raw_conflicts]
    clean_changes = _project_clean_changes(snapshot)
    return ContextReconciliationDetail(
        page=ContextReconciliationPage(
            id=snapshot.page.id,
            slug=snapshot.page.slug,
            title=snapshot.page.title,
        ),
        branch=_branch_ref(snapshot.branch),
        base=_version_ref(snapshot.base),
        original_head=_version_ref(snapshot.original_head),
        head=_version_ref(snapshot.head),
        head_advance_count=snapshot.head_advance_count,
        blocks=_project_blocks(snapshot),
        conflicts=conflicts,
        clean_changes=clean_changes,
    )


def _lineage_distance(
    session: Session,
    *,
    ancestor: ContextPageVersion,
    descendant: ContextPageVersion,
) -> int:
    if descendant.id == ancestor.id:
        return 0
    cache_key = (
        "context_reconciliation_linear_lineage",
        session.get_transaction(),
        ancestor.workspace_id,
        ancestor.page_id,
    )
    lineage = session.info.get(cache_key)
    if lineage is None:
        lineage = {
            version_id: (parent_version_id, graph_status)
            for version_id, parent_version_id, graph_status in session.execute(
                select(
                    ContextPageVersion.id,
                    ContextPageVersion.parent_version_id,
                    ContextPageVersion.graph_status,
                ).where(
                    ContextPageVersion.workspace_id == ancestor.workspace_id,
                    ContextPageVersion.page_id == ancestor.page_id,
                )
            )
        }
        session.info[cache_key] = lineage
    distance = 0
    cursor_id = descendant.id
    seen: set[UUID] = set()
    while cursor_id != ancestor.id:
        if cursor_id in seen:
            raise ReadValidationError(
                "reconciliation head lineage contains a cycle",
                error_code="reconcile_target_invalid",
            )
        seen.add(cursor_id)
        cursor = lineage.get(cursor_id)
        if cursor is None or cursor[0] is None or cursor[1] != "linear":
            raise ReadValidationError(
                "reconciliation head lineage is invalid",
                error_code="reconcile_target_invalid",
            )
        cursor_id = cursor[0]
        distance += 1
    return distance


def _version_ref(version: ContextPageVersion) -> ContextReconciliationVersion:
    return ContextReconciliationVersion(
        version_id=version.id,
        version_no=version.version_no,
        content_hash=version.content_hash,
        created_at=version.created_at,
    )


def _branch_ref(version: ContextPageVersion) -> ContextReconciliationBranch:
    assert version.parent_version_id is not None
    return ContextReconciliationBranch(
        version_id=version.id,
        version_no=version.version_no,
        content_hash=version.content_hash,
        created_at=version.created_at,
        actor_kind=version.actor_type,
        actor_display_name=version.actor_display_name,
        base_version_id=version.parent_version_id,
    )


def _side(
    value: SideDict | None,
    *,
    version: ContextPageVersion | None = None,
) -> ContextReconciliationSide:
    value = value or {}
    return ContextReconciliationSide(
        hash=value.get("hash") if isinstance(value.get("hash"), str) else None,
        text=value.get("text") if isinstance(value.get("text"), str) else None,
        actor_kind=version.actor_type if version is not None else None,
        actor_display_name=(
            version.actor_display_name if version is not None else None
        ),
        at=version.created_at if version is not None else None,
        version_id=version.id if version is not None else None,
    )


def _project_conflict(
    session: Session,
    snapshot: ReconciliationSnapshot,
    raw: dict,
) -> ContextReconciliationConflict:
    block_id = raw["block_id"]
    descriptor = target_descriptor(snapshot.base_map, block_id)
    if descriptor is None:
        raise ReadValidationError(
            "reconciliation conflict target is absent from the base map",
            error_code="reconcile_target_invalid",
        )
    resolution_target_id = raw.get("resolution_target_id")
    identity_retired = resolution_target_id is None
    insert_operations = [
        operation
        for operation in snapshot.active_operations
        if isinstance(operation, InsertAfterOperation) and operation.block == block_id
    ]
    head_effects: list[ContextReconciliationOperationEffect] = []
    yours_effects: list[ContextReconciliationOperationEffect] = []
    if insert_operations:
        head_effects.append(
            ContextReconciliationOperationEffect(
                op="insert_after",
                content=_gap_text_after_target(snapshot, block_id),
            )
        )
        yours_effects.extend(
            ContextReconciliationOperationEffect(
                op="insert_after",
                content=operation.content,
            )
            for operation in insert_operations
        )
    return ContextReconciliationConflict(
        block_id=block_id,
        resolution_target_id=resolution_target_id,
        kind=raw["kind"],
        granularity=("table_row" if descriptor["kind"] == "table_row" else "block"),
        parent_block_id=descriptor["parent_block_id"],
        row_index=(
            descriptor["ordinal"] if descriptor["kind"] == "table_row" else None
        ),
        line_span=tuple(raw["line_span"]) if raw.get("line_span") else None,
        base=_side(raw.get("base"), version=snapshot.base),
        head=_side(raw.get("head"), version=snapshot.head),
        yours=_side(raw.get("yours"), version=snapshot.branch),
        head_effects=head_effects,
        yours_effects=yours_effects,
        identity_retired=identity_retired,
        restore_yours_available=_restore_yours_available(
            session,
            snapshot,
            block_id,
        ),
    )


def _gap_text_after_target(snapshot: ReconciliationSnapshot, target_id: str) -> str:
    """Project every current HEAD block in an insert_after target's gap."""

    base_ids = [entry["id"] for entry in snapshot.base_map]
    head_ids = [entry["id"] for entry in snapshot.head_map]
    try:
        base_index = base_ids.index(target_id)
    except ValueError:
        return ""
    head_target_id = resolve_target_id_between_maps(
        snapshot.base_map,
        snapshot.head_map,
        target_id,
    )
    if head_target_id is None:
        return ""
    try:
        head_index = head_ids.index(head_target_id)
    except ValueError:
        return ""
    next_survivor = next(
        (
            candidate
            for candidate in base_ids[base_index + 1 :]
            if candidate in head_ids
        ),
        None,
    )
    gap_end = (
        head_ids.index(next_survivor) if next_survivor is not None else len(head_ids)
    )
    if gap_end <= head_index + 1:
        return ""
    fresh_blocks = compute_blocks(snapshot.head.body_md)
    if len(fresh_blocks) != len(snapshot.head_map):
        return ""
    parsed = parse_markdown(snapshot.head.body_md)
    start = parsed.char_offset(fresh_blocks[head_index + 1].line_start)
    end = (
        parsed.char_offset(fresh_blocks[gap_end].line_start)
        if gap_end < len(fresh_blocks)
        else len(snapshot.head.body_md)
    )
    return snapshot.head.body_md[start:end].rstrip("\r\n")


def _restore_yours_available(
    session: Session,
    snapshot: ReconciliationSnapshot,
    target_id: str,
) -> bool:
    from backend.services.context_write_service import ContextWriteService

    current_hashes = hash_map(snapshot.head_map)
    decisions: list[ResolveConflictOperation] = []
    for conflict_id in sorted(snapshot.conflict_ids):
        current_id = resolve_target_id_between_maps(
            snapshot.base_map,
            snapshot.head_map,
            conflict_id,
        )
        decisions.append(
            ResolveConflictOperation(
                op="resolve_conflict",
                block=conflict_id,
                resolution=(
                    "restore_yours" if conflict_id == target_id else "keep_head"
                ),
                expected=(
                    current_hashes.get(current_id) if current_id is not None else None
                ),
            )
        )
    try:
        ContextWriteService._resolve_reconcile_operations(
            session=session,
            page=snapshot.page,
            reconcile_base_version=snapshot.head,
            reconciles=[snapshot.branch.id],
            operations=decisions,
            excluded_clean_change_ids=[],
        )
    except ReadValidationError:
        return False
    return True


def _project_clean_changes(
    snapshot: ReconciliationSnapshot,
) -> list[ContextReconciliationCleanChange]:
    head_lookup = text_by_id(snapshot.head.body_md, snapshot.head_map)
    authored = authored_sides_by_target(
        operations=snapshot.active_operations,
        base_block_map=snapshot.base_map,
        authored_body_md=snapshot.branch.body_md,
        authored_block_map=snapshot.branch_map,
    )
    active_ids = target_ids_for_operations(snapshot.active_operations) or frozenset()
    changes: list[ContextReconciliationCleanChange] = []
    for target_id in sorted(
        active_ids - snapshot.conflict_ids - snapshot.already_satisfied_ids
    ):
        descriptor = target_descriptor(snapshot.base_map, target_id)
        if descriptor is None:
            continue
        current_id = resolve_target_id_between_maps(
            snapshot.base_map,
            snapshot.head_map,
            target_id,
        )
        head_entry = head_lookup.get(current_id) if current_id is not None else None
        changes.append(
            ContextReconciliationCleanChange(
                block_id=target_id,
                resolution_target_id=current_id,
                kind=descriptor["kind"],
                state="branch_only",
                head=(
                    _side(
                        {
                            "hash": hash_map(snapshot.head_map).get(current_id),
                            "text": head_entry[3],
                        },
                        version=snapshot.head,
                    )
                    if head_entry is not None
                    else None
                ),
                yours=_side(authored.get(target_id), version=snapshot.branch),
            )
        )
    changed_ids = changed_ids_between_maps(
        snapshot.base_map,
        snapshot.head_map,
        base_body_md=snapshot.base.body_md,
        candidate_body_md=snapshot.head.body_md,
    )
    for target_id in sorted(changed_ids - snapshot.conflict_ids - active_ids):
        descriptor = target_descriptor(
            snapshot.head_map, target_id
        ) or target_descriptor(
            snapshot.base_map,
            target_id,
        )
        if descriptor is None:
            continue
        head_entry = head_lookup.get(target_id)
        changes.append(
            ContextReconciliationCleanChange(
                block_id=target_id,
                resolution_target_id=(target_id if head_entry is not None else None),
                kind=descriptor["kind"],
                state="head_only",
                head=(
                    _side(
                        {
                            "hash": hash_map(snapshot.head_map).get(target_id),
                            "text": head_entry[3],
                        },
                        version=snapshot.head,
                    )
                    if head_entry is not None
                    else None
                ),
                yours=None,
            )
        )
    return changes


def _project_blocks(
    snapshot: ReconciliationSnapshot,
) -> list[ContextReconciliationBlock]:
    head_lookup = text_by_id(snapshot.head.body_md, snapshot.head_map)
    branch_lookup = text_by_id(snapshot.branch.body_md, snapshot.branch_map)
    base_ids = set(hash_map(snapshot.base_map))
    active_ids = target_ids_for_operations(snapshot.active_operations) or frozenset()
    changed_ids = changed_ids_between_maps(
        snapshot.base_map,
        snapshot.head_map,
        base_body_md=snapshot.base.body_md,
        candidate_body_md=snapshot.head.body_md,
    )
    conflict_display_ids = set(snapshot.conflict_ids)
    for conflict_id in snapshot.conflict_ids:
        for candidate_map in (snapshot.head_map, snapshot.branch_map):
            resolved_id = resolve_target_id_between_maps(
                snapshot.base_map,
                candidate_map,
                conflict_id,
            )
            if resolved_id is not None:
                conflict_display_ids.add(resolved_id)
    satisfied_display_ids = set(snapshot.already_satisfied_ids)
    for satisfied_id in snapshot.already_satisfied_ids:
        for candidate_map in (snapshot.head_map, snapshot.branch_map):
            resolved_id = resolve_target_id_between_maps(
                snapshot.base_map,
                candidate_map,
                satisfied_id,
            )
            if resolved_id is not None:
                satisfied_display_ids.add(resolved_id)
    rows: list[ContextReconciliationBlock] = []
    seen: set[str] = set()

    def add(block_map: list[dict], lookup, source: str) -> None:
        for entry in block_map:
            candidates = [(entry["id"], entry["kind"], None, None)]
            candidates.extend(
                (row["id"], "table_row", entry["id"], index)
                for index, row in enumerate(entry.get("rows") or ())
            )
            for target_id, kind, parent_id, row_index in candidates:
                if target_id in seen:
                    continue
                seen.add(target_id)
                if target_id in conflict_display_ids:
                    state = "conflict"
                elif target_id in satisfied_display_ids:
                    state = "same"
                elif target_id in active_ids or (
                    source == "branch" and target_id not in base_ids
                ):
                    state = "branch_only"
                elif target_id in changed_ids or (
                    source == "head" and target_id not in base_ids
                ):
                    state = "head_only"
                else:
                    state = "same"
                text_entry = lookup.get(target_id)
                label = (
                    text_entry[3].strip().splitlines()[0][:120]
                    if text_entry is not None and text_entry[3].strip()
                    else kind
                )
                rows.append(
                    ContextReconciliationBlock(
                        block_id=target_id,
                        kind=kind,
                        granularity="table_row" if kind == "table_row" else "block",
                        parent_block_id=parent_id,
                        row_index=row_index,
                        state=state,
                        label=label,
                    )
                )

    add(snapshot.head_map, head_lookup, "head")
    add(snapshot.branch_map, branch_lookup, "branch")
    return rows


def discard_reconciliation_branch(
    session: Session,
    *,
    page: ContextPage,
    branch: ContextPageVersion,
    reason: str,
    actor_kind: str,
    actor_id: str,
    audit_service: AuditService,
) -> list[UUID]:
    """Explicitly retire one active branch and its review workflow."""
    if reason is not None:
        validate_text(reason, field="reason", max_length=2000)

    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ReadValidationError(
            "discard reason must not be blank",
            error_code="validation_error",
        )
    workspace_id = page.workspace_id
    page_id = page.id
    branch_id = branch.id
    # Reconciliation already holds locks in page -> branch order.  Discard must
    # use the identical order and refresh both rows under those locks; otherwise
    # a stale ORM object can overwrite a concurrently reconciled terminal state.
    acquire_write_intent(session)
    locked_page = session.scalar(
        select(ContextPage)
        .where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.id == page_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    locked_branch = session.scalar(
        select(ContextPageVersion)
        .where(
            ContextPageVersion.workspace_id == workspace_id,
            ContextPageVersion.page_id == page_id,
            ContextPageVersion.id == branch_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if locked_page is None or locked_branch is None:
        raise ReadValidationError(
            "only an active reconciliation branch can be discarded",
            error_code="reconcile_target_invalid",
        )
    page = locked_page
    branch = locked_branch
    if (
        branch.workspace_id != page.workspace_id
        or branch.page_id != page.id
        or branch.graph_status != "branched"
        or branch.version_status != "draft"
        or branch.id == editable_context_version_id(page)
    ):
        raise ReadValidationError(
            "only an active reconciliation branch can be discarded",
            error_code="reconcile_target_invalid",
        )
    branch.graph_status = "discarded"
    session.add(branch)
    reconciliation_rows = (
        session.execute(
            select(ReviewItem, ChangeSet)
            .join(ChangeSet, ChangeSet.id == ReviewItem.change_set_id)
            .join(
                ChangeOperation,
                ChangeOperation.change_set_id == ChangeSet.id,
            )
            .where(
                ReviewItem.workspace_id == page.workspace_id,
                ChangeSet.workspace_id == page.workspace_id,
                ReviewItem.review_type == "context_reconciliation",
                ReviewItem.status.in_(("pending", "changes_requested")),
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page.id,
                ChangeOperation.diff_ref == f"context_page_version:{branch.id}",
            )
        )
        .unique()
        .all()
    )
    for review, change_set in reconciliation_rows:
        review.status = "superseded"
        review.claimed_by = None
        review.claimed_at = None
        change_set.status = "superseded"
    audit_service.emit(
        session=session,
        workspace_id=page.workspace_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        action="context_page.draft.diverged_discarded",
        target_kind="context_page_version",
        target_id=branch.id,
        payload={
            "page_id": str(page.id),
            "version_id": str(branch.id),
            "review_item_ids": [
                str(review.id) for review, _change_set in reconciliation_rows
            ],
            "change_set_ids": [
                str(change_set.id) for _review, change_set in reconciliation_rows
            ],
            "resolution": "explicit_discard",
            "reason": normalized_reason,
        },
    )
    return [review.id for review, _change_set in reconciliation_rows]
