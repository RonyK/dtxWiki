"""SQL boundary for mutable Context drafts and immutable merge decisions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from uuid import UUID

from sqlalchemy import func

from backend.app.context_version_storage import build_context_merger
from backend.context_versions.domain import (
    MergeCandidate,
    MergeSnapshot,
    PageMetadata,
    VersionNode,
)
from backend.context_versions.history import HistoryGraph
from backend.context_versions.merge import prefer_ours, result_hash
from backend.models.base import utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPageVersion, ContextVersionParent
from backend.repositories.workspace_scoped import (
    delete_in_workspace,
    get_in_workspace,
    select_in_workspace,
)
from backend.services.context_version_refs import context_version_ref
from backend.services.read_errors import (
    ReadNotFoundError,
    ReadValidationError,
    StaleBaseVersionError,
)
from backend.utils.block_identity import compute_block_map_with_ids
from backend.utils.content_hash import content_hash
from backend.utils.uuid import parse_uuid_or_none


def metadata_dict(value: PageMetadata) -> dict:
    data = asdict(value)
    data["tags"] = list(value.tags)
    data["aliases"] = list(value.aliases)
    data["placement_parent_node_id"] = (
        str(value.placement_parent_node_id) if value.placement_parent_node_id else None
    )
    return data


def metadata_for(page, version=None) -> PageMetadata:
    data = (
        (getattr(version, "page_metadata", None) or {}) if version is not None else {}
    )
    return PageMetadata(
        data.get("title", page.title),
        data.get("page_type", page.page_type),
        tuple(data.get("tags", page.tags or []) or []),
        tuple(data.get("aliases", page.aliases or []) or []),
        parse_uuid_or_none(data.get("placement_parent_node_id"))
        if "placement_parent_node_id" in data
        else page.placement_parent_node_id,
    )


def parents_for(session, version) -> tuple[UUID, ...]:
    additional = session.scalars(
        select_in_workspace(
            ContextVersionParent,
            workspace_id=version.workspace_id,
            conditions=(
                ContextVersionParent.page_id == version.page_id,
                ContextVersionParent.version_id == version.id,
            ),
        ).order_by(ContextVersionParent.ordinal)
    ).all()
    return tuple(
        ([version.parent_version_id] if version.parent_version_id else [])
        + [row.parent_version_id for row in additional]
    )


def set_parents(session, version, parents) -> None:
    ids = list(dict.fromkeys(parents))
    for parent_id in ids:
        parent = session.scalar(
            select_in_workspace(
                ContextPageVersion,
                workspace_id=version.workspace_id,
                conditions=(
                    ContextPageVersion.page_id == version.page_id,
                    ContextPageVersion.id == parent_id,
                ),
            )
        )
        if parent is None or not parent.commit_oid or parent_id == version.id:
            raise ReadValidationError("Draft parents must be commits from this page")
    version.parent_version_id = ids[0] if ids else None
    session.execute(
        delete_in_workspace(
            ContextVersionParent,
            workspace_id=version.workspace_id,
            conditions=(
                ContextVersionParent.page_id == version.page_id,
                ContextVersionParent.version_id == version.id,
            ),
        )
    )
    for ordinal, parent_id in enumerate(ids[1:], 1):
        session.add(
            ContextVersionParent(
                workspace_id=version.workspace_id,
                page_id=version.page_id,
                version_id=version.id,
                ordinal=ordinal,
                parent_version_id=parent_id,
            )
        )
    session.flush()


def snapshot(page, version) -> MergeSnapshot:
    return MergeSnapshot(
        page.workspace_id,
        page.id,
        version.id,
        version.body_md,
        metadata_for(page, version),
        version.last_saved_at or version.created_at,
        version.block_map,
    )


def history_for(session, page):
    rows = session.scalars(
        select_in_workspace(
            ContextPageVersion,
            workspace_id=page.workspace_id,
            conditions=(
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.version_status != "discarded",
            ),
        )
    ).all()
    extras = session.scalars(
        select_in_workspace(
            ContextVersionParent,
            workspace_id=page.workspace_id,
            conditions=(ContextVersionParent.page_id == page.id,),
        )
    ).all()
    by_child = {}
    for row in sorted(extras, key=lambda row: row.ordinal):
        by_child.setdefault(row.version_id, []).append(row.parent_version_id)
    nodes = [
        VersionNode(
            page.workspace_id,
            page.id,
            row.id,
            tuple(
                ([row.parent_version_id] if row.parent_version_id else [])
                + by_child.get(row.id, [])
            ),
        )
        for row in rows
    ]
    return HistoryGraph(nodes), {row.id: row for row in rows}


def _candidate(value):
    block_map = value.block_map or compute_block_map_with_ids(
        value.body_md,
        previous_body_md=None,
        previous_block_map=None,
        identity_seed=str(value.version_id),
    )
    return MergeCandidate(
        value.body_md,
        value.metadata,
        block_map,
        result_hash(value.body_md, value.metadata, block_map),
    )


@dataclass(frozen=True)
class DraftMergePreview:
    version_id: UUID
    editing_revision: int
    published_version_id: UUID | None
    parent_version_ids: tuple[UUID, ...]
    kind: str
    clean: MergeCandidate | None
    keep_both: MergeCandidate
    latest: MergeCandidate
    metadata_conflicts: tuple[str, ...] = ()
    base_conflicted: bool = False
    latest_side: str = "ours"
    conflict_bodies: dict[str, str] | None = None
    # The commit merged into the draft: the published version for publication,
    # the named commit for bring-in, the draft itself for two saves of it.
    target_version_id: UUID | None = None
    # The facts `prefer_ours` compared, shown to the person choosing (REST).
    sides: dict | None = None

    def choose(self, on_conflict=None):
        if on_conflict not in {None, "keep_both", "latest"}:
            raise ReadValidationError("on_conflict must be keep_both or latest")
        if on_conflict is not None:
            return getattr(self, on_conflict)
        if self.clean is None:
            error = StaleBaseVersionError(
                "Context changes conflict; choose keep_both or latest"
            )
            error.error_code = "context_merge_conflict"
            raise error
        return self.clean

    def payload(self):
        def dump(name, candidate):
            result = {
                "body_md": candidate.body_md,
                "page_metadata": metadata_dict(candidate.metadata),
                "block_map": candidate.block_map,
                "result_hash": candidate.result_hash,
                "conflicts": [asdict(item) for item in candidate.conflicts],
                "offset_unit": "unicode_code_points",
            }
            original = (self.conflict_bodies or {}).get(name)
            if (
                candidate.conflicts
                and original is not None
                and original != candidate.body_md
            ):
                result["conflict_body_md"] = original
            return result

        return {
            "version_id": str(self.version_id),
            "editing_revision": self.editing_revision,
            "published_version_id": str(self.published_version_id)
            if self.published_version_id
            else None,
            "parent_version_ids": [str(key) for key in self.parent_version_ids],
            "kind": self.kind,
            "result": dump("result", self.clean) if self.clean else None,
            "keep_both": dump("keep_both", self.keep_both),
            "latest": dump("latest", self.latest),
            "metadata_conflicts": list(self.metadata_conflicts),
            "base_conflicted": self.base_conflicted,
            "latest_side": self.latest_side,
            "offset_unit": "unicode_code_points",
        }

    def rest_payload(self, page_id: UUID, *, content_hash: str, references_hash: str):
        """REST projection: the choices only for a conflict, plus both sides."""
        data = self.payload()
        conflict = self.kind == "conflict"
        return {
            **data,
            "page_id": str(page_id),
            "content_hash": content_hash,
            "references_hash": references_hash,
            "target_version_id": str(self.target_version_id)
            if self.target_version_id
            else None,
            "keep_both": data["keep_both"] if conflict else None,
            "latest": data["latest"] if conflict else None,
            "sides": self.sides if self.kind != "fast_forward" else None,
        }


def merge_side(version, value: MergeSnapshot, *, saved_by=None) -> dict:
    """One side of a merge as the person sees it: who saved it, when, and its
    single-valued metadata. `saved_by` overrides the row's last saver."""
    if saved_by is None:
        actor_id = version.actor_id or version.created_by_actor_id
        saved_by = (
            {
                "actor_kind": version.actor_type
                if version.actor_id
                else version.created_by_actor_kind,
                "actor_id": str(actor_id),
                "display_name": version.actor_display_name,
            }
            if actor_id
            else None
        )
    return {
        "version_id": str(value.version_id),
        "saved_by": saved_by,
        "last_saved_at": value.last_saved_at.isoformat()
        if value.last_saved_at
        else None,
        "metadata": {
            "title": value.metadata.title,
            "page_type": value.metadata.page_type,
            "placement_parent_node_id": str(value.metadata.placement_parent_node_id)
            if value.metadata.placement_parent_node_id
            else None,
        },
    }


def preview_merge(session, page, version, *, commit_version_id=None, prepare=None):
    history, rows = history_for(session, page)
    target_id = commit_version_id or page.current_published_version_id
    # Both sides must be versions in this page's history; any other id (a
    # discarded row, another page's version, nothing at all) is not found.
    if version.id not in rows or (target_id is not None and target_id not in rows):
        raise ReadNotFoundError("context page version not found")
    metadata_conflicts = ()
    base_conflicted = False
    latest_side = "ours"
    sides = None
    if (
        target_id is None
        or target_id == version.id
        or history.is_ancestor(target_id, version.id)
    ):
        candidate = _candidate(snapshot(page, version))
        clean = both = latest = candidate
        kind = "fast_forward"
    else:
        target = rows[target_id]
        if not target.commit_oid:
            raise ReadValidationError("Merge target must be a commit from this page")
        # Hydrate only this pair's ancestors, excluding unrelated history bodies.
        ancestors = set()
        pending = [version.id, target.id]
        while pending:
            key = pending.pop()
            if key in ancestors:
                continue
            ancestors.add(key)
            pending.extend(history.node(key).parents)
        snapshots = {key: snapshot(page, rows[key]) for key in ancestors}
        outcome = build_context_merger().merge_from_history(
            history,
            snapshots,
            version.id,
            target.id,
            published_side="theirs",
            identity_seed=f"context-merge:{version.id}:{target.id}:{version.editing_revision}",
        )
        clean, both, latest = outcome.clean, outcome.keep_both, outcome.latest
        metadata_conflicts = outcome.metadata_conflicts
        base_conflicted = outcome.base_conflicted
        latest_side = (
            "ours"
            if prefer_ours(snapshots[version.id], snapshots[target.id])
            else "theirs"
        )
        sides = {
            "ours": merge_side(version, snapshots[version.id]),
            "theirs": merge_side(target, snapshots[target.id]),
        }
        kind = "conflict" if clean is None else "automatic_merge"
    conflict_bodies = {"keep_both": both.body_md, "latest": latest.body_md}
    if clean is not None:
        conflict_bodies["result"] = clean.body_md
    if prepare is not None and kind == "fast_forward":
        clean = both = latest = prepare(clean)
    elif prepare is not None:
        both = prepare(both)
        latest = prepare(latest)
        clean = prepare(clean) if clean else None
    return DraftMergePreview(
        version.id,
        version.editing_revision,
        page.current_published_version_id,
        parents_for(session, version),
        kind,
        clean,
        both,
        latest,
        metadata_conflicts=metadata_conflicts,
        base_conflicted=base_conflicted,
        latest_side=latest_side,
        conflict_bodies=conflict_bodies,
        target_version_id=target_id,
        sides=sides,
    )


_PUBLISHED_PIN = object()


def verify_preview(preview, request, candidate, *, expected_target=_PUBLISHED_PIN):
    """Bind a write to the preview its caller saw.

    The other-side pin is the published version for publication and approval,
    and the named commit for bring-in; both compare with the preview's target.
    """
    revision = getattr(request, "expected_editing_revision", None)
    if expected_target is _PUBLISHED_PIN:
        expected_target = getattr(request, "expected_published_version_id", None)
    expected_hash = getattr(request, "result_hash", None)
    if (
        (revision is not None and revision != preview.editing_revision)
        or (expected_hash is not None and expected_target != preview.target_version_id)
        or (expected_hash is not None and expected_hash != candidate.result_hash)
    ):
        raise StaleBaseVersionError(
            "The merge preview changed; request a new preview",
            latest_version_id=preview.version_id,
        )


def merge_write_candidate(
    session,
    page,
    version,
    request,
    *,
    prepare,
    commit_version_id=None,
    expected_target=_PUBLISHED_PIN,
):
    """Select and verify the exact prepared result used by policy and storage."""
    preview = preview_merge(
        session, page, version, commit_version_id=commit_version_id, prepare=prepare
    )
    candidate = preview.choose(getattr(request, "on_conflict", None))
    body = getattr(request, "result_body_md", None)
    metadata = getattr(request, "result_page_metadata", None)
    if body is not None or metadata is not None:
        candidate = prepare(
            edit_merge_candidate(candidate, body_md=body, page_metadata=metadata)
        )
    verify_preview(preview, request, candidate, expected_target=expected_target)
    return preview, candidate


def direct_publication_risk(
    session,
    page,
    candidate,
    *,
    workspace,
    reviews,
    actor_provenance,
    risk_resolver,
    lint_registry=None,
):
    """Apply the existing direct-write policy to the selected result itself."""
    from backend.services.auto_approval import evaluate_context_write
    from backend.services.review_risk_policy import REVIEW_RISK_SIGNALS

    signals = {
        key: any((review.risk_signals or {}).get(key) for review in reviews)
        for key in REVIEW_RISK_SIGNALS
    }
    gate = evaluate_context_write(
        session,
        workspace_id=page.workspace_id,
        needs_review=signals["caller_needs_review"],
        body=candidate.body_md,
        lint_registry=lint_registry,
    )
    signals["lint_error"] = "lint_error" in gate.vetoes
    signals["decision_page_type"] = candidate.metadata.page_type == "decision"
    return risk_resolver.resolve(
        workspace=workspace,
        event="direct_page_write_audit",
        signals={**signals, "ai_authored": True},
        actor_provenance=actor_provenance,
    )


def record_draft_save(
    session,
    page,
    version,
    *,
    actor_kind,
    actor_id,
    reason="page.autosave.draft",
    lint_registry=None,
    refresh_review_risk=True,
):
    change_set = ChangeSet(
        workspace_id=page.workspace_id,
        reason=reason,
        trigger_type="manual_edit",
        actor_type=actor_kind,
        actor_id=parse_uuid_or_none(actor_id),
        status="applied",
        risk_level="low",
    )
    session.add(change_set)
    session.flush()
    session.add(
        ChangeOperation(
            workspace_id=page.workspace_id,
            change_set_id=change_set.id,
            target_type="context_page",
            target_id=page.id,
            operation="save_draft",
            diff_summary=reason,
            diff_ref=context_version_ref(version.id),
        )
    )
    if version.change_set_id is None:
        version.change_set_id = change_set.id
    version.actor_type, version.actor_id = actor_kind, parse_uuid_or_none(actor_id)
    page.updated_at = utc_now()
    from backend.services.audit_service import AuditService

    AuditService().emit(
        session=session,
        workspace_id=page.workspace_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        action="context_page.draft.save",
        target_kind="context_page",
        target_id=page.id,
        payload={
            "version_id": str(version.id),
            "base_version_id": str(version.parent_version_id)
            if version.parent_version_id
            else None,
            "editing_revision": version.editing_revision,
            "change_set_id": str(change_set.id),
        },
    )
    if refresh_review_risk:
        refresh_draft_review_risk(session, page, version, lint_registry=lint_registry)
    return change_set


def refresh_draft_review_risk(session, page, version, *, lint_registry=None):
    """Keep pending approval policy evidence bound to the mutable draft.

    Preserve the request's explicit human-review intent and author signals,
    then recalculate content-derived flags and measured risk from current
    bytes, metadata and references using the existing policy resolver.
    """
    from backend.models.workspace import Workspace
    from backend.services.auto_approval import evaluate_context_write
    from backend.services.context_write_service import (
        ContextWriteService,
        _measure_update_event,
    )
    from backend.services.document_references import document_references_hash
    from backend.services.review_risk_policy import (
        ReviewRiskResolver,
        review_risk_snapshot,
    )

    reviews = ContextWriteService._reviews_for_version(session, version, only_open=True)
    if not reviews:
        return
    workspace = session.get(Workspace, page.workspace_id)
    if workspace is None:
        return
    parent = (
        get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=page.workspace_id,
            resource_id=version.parent_version_id,
        )
        if version.parent_version_id
        else None
    )
    before = parent.body_md if parent is not None else ""
    measured_event = _measure_update_event(before, version.body_md)
    reference_change = (
        (
            document_references_hash(session, parent)
            != document_references_hash(session, version)
        )
        if parent is not None
        else bool(
            document_references_hash(session, version)
            != document_references_hash(session, None)
        )
    )
    gate = evaluate_context_write(
        session,
        workspace_id=page.workspace_id,
        needs_review=False,
        body=version.body_md,
        lint_registry=lint_registry,
    )
    for review in reviews:
        old_signals = dict(review.risk_signals or {})
        signals = {
            key: value
            for key, value in old_signals.items()
            if key
            not in {
                "reason_codes",
                "lint_error",
                "human_review_required",
                "manual_citation",
                "decision_page_type",
            }
        }
        signals.update(
            {
                "review_required": True,
                "manual_citation": reference_change,
                "decision_page_type": metadata_for(page, version).page_type
                == "decision",
                **gate.force_signals(),
            }
        )
        resolution = ReviewRiskResolver().resolve(
            workspace=workspace,
            event=measured_event,
            signals=signals,
            floor_level=(review.risk_policy_snapshot or {}).get("default"),
        )
        review.risk_level = resolution.risk_level
        for key, value in review_risk_snapshot(resolution).items():
            setattr(review, key, value)
        change_set = get_in_workspace(
            session,
            ChangeSet,
            workspace_id=page.workspace_id,
            resource_id=review.change_set_id,
        )
        if change_set is not None:
            change_set.risk_level = resolution.risk_level


def merge_draft_references(session, *, source, head, target, self_origins=None):
    """Apply the existing bibliography three-way merge to a mutable result."""
    from backend.services.document_reference_merge import (
        merge_authored_document_references,
    )
    from backend.services.document_references import (
        replace_authored_document_references,
    )

    merged = merge_authored_document_references(session, source=source, head=head)
    replace_authored_document_references(
        session, version=target, rows=merged.rows, self_origins=self_origins
    )


def materialize_publication(
    session, page, version, preview, candidate, *, writer, actor_kind, actor_id
):
    from backend.services.context_lifecycle import record_context_decision

    reference_merge = None
    if preview.kind != "fast_forward" and page.current_published_version_id:
        from backend.services.document_reference_merge import (
            merge_authored_document_references,
        )

        head = get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=page.workspace_id,
            resource_id=page.current_published_version_id,
        )
        reference_merge = merge_authored_document_references(
            session, source=version, head=head
        )
    if preview.kind == "fast_forward" and version.version_status == "draft":
        target = version
    elif (
        preview.kind == "fast_forward"
        and version.commit_oid
        and candidate.result_hash == _candidate(snapshot(page, version)).result_hash
    ):
        return version
    else:
        if version.version_status == "draft":
            record_context_decision(
                session,
                action="close",
                page=page,
                version=version,
                context_version_writer=writer,
                actor_kind=actor_kind,
                actor_id=actor_id,
                reason="Draft included in published merge",
            )
        target = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=session.scalar(
                select_in_workspace(
                    ContextPageVersion,
                    workspace_id=page.workspace_id,
                    conditions=(ContextPageVersion.page_id == page.id,),
                ).with_only_columns(func.max(ContextPageVersion.version_no))
            )
            + 1,
            parent_version_id=page.current_published_version_id,
            version_status="draft",
            body_md=candidate.body_md,
            content_hash=content_hash(candidate.body_md),
            block_map=deepcopy(candidate.block_map),
            page_metadata=metadata_dict(candidate.metadata),
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            created_by_actor_kind=actor_kind,
            created_by_actor_id=actor_id,
            rendering_profile=version.rendering_profile,
            last_saved_at=utc_now(),
        )
        session.add(target)
        session.flush()
        set_parents(
            session,
            target,
            [version.id]
            if preview.kind == "fast_forward"
            else [page.current_published_version_id, version.id],
        )
        from backend.services.context_version_copy import (
            copy_context_version_references,
        )

        copy_context_version_references(session, source=version, target=target)
    if reference_merge is not None:
        from backend.services.document_references import (
            replace_authored_document_references,
        )

        replace_authored_document_references(
            session, version=target, rows=reference_merge.rows
        )
    target.body_md = candidate.body_md
    target.content_hash = content_hash(candidate.body_md)
    target.block_map = deepcopy(candidate.block_map)
    target.page_metadata = metadata_dict(candidate.metadata)
    return target


def edit_merge_candidate(candidate, *, body_md=None, page_metadata=None):
    """Apply a reviewer's explicit result edit before preparation and hashing."""
    from backend.schemas.page import PageMetadataPatch

    metadata = candidate.metadata
    if page_metadata is not None:
        edit = PageMetadataPatch.model_validate(page_metadata)
        # Placement is the one field whose null is a value (Context / Inbox):
        # it changes when the edit sets it, as the metadata patch writer reads
        # it (context_write_service). The other fields change when not null.
        metadata = PageMetadata(
            edit.title if edit.title is not None else metadata.title,
            edit.page_type if edit.page_type is not None else metadata.page_type,
            tuple(edit.tags) if edit.tags is not None else metadata.tags,
            tuple(edit.aliases) if edit.aliases is not None else metadata.aliases,
            edit.placement_parent_node_id
            if "placement_parent_node_id" in edit.model_fields_set
            else metadata.placement_parent_node_id,
        )
    body = candidate.body_md if body_md is None else body_md
    block_map = (
        candidate.block_map
        if body == candidate.body_md
        else compute_block_map_with_ids(
            body,
            previous_body_md=candidate.body_md,
            previous_block_map=candidate.block_map,
            identity_seed=f"context-result:{candidate.result_hash}",
        )
    )
    return replace(
        candidate,
        body_md=body,
        metadata=metadata,
        block_map=block_map,
        result_hash=result_hash(body, metadata, block_map),
    )


class ContextTransactionFactory:
    """Let a public writer participate in its caller's existing transaction."""

    def __init__(self, session):
        self.session = session

    def begin(self):
        from contextlib import nullcontext

        if not self.session.in_transaction():
            raise RuntimeError("Context writer requires an active caller transaction")
        return nullcontext(self.session)

    def __call__(self):
        return self.begin()
