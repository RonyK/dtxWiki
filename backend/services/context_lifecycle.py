"""Canonical Context version publication and history projections.

The database keeps the original compact version-status enum.  This module is
the one place that turns those durable rows plus page pointers into the richer
history vocabulary used by readers, and the one place that advances a
published head.  Keeping both decisions together prevents browser writes,
review approval, and Context/MCP publication from drifting apart again.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.repositories.workspace_scoped import get_in_workspace, select_in_workspace
from backend.services.context_media_legacy_cutover import (
    require_no_visible_legacy_media,
)
from backend.services.public_page_exposure import (
    clear_public_page_exposure,
    reconcile_public_exposure_after_publication,
)
from backend.services.read_errors import StaleContextDraftError
from backend.utils.content_recency import normalize_content_recency
from backend.utils.page_frontmatter import parse_page_frontmatter
from backend.utils.uuid import parse_uuid_or_none

CONTEXT_HISTORY_STATUSES = (
    "state_inconsistent",
    "current_draft",
    "published_snapshot",
    "alternative_draft",
    "discarded_draft",
    "superseded_snapshot",
    "superseded_draft",
)

UNARCHIVE_REASON = "page restored from archive"

_PUBLISHED_STATUSES = frozenset({"published_snapshot", "superseded_snapshot"})
_GRAPH_STATUSES = frozenset({"linear", "branched", "superseded", "discarded"})


@dataclass(frozen=True, slots=True)
class ContextVersionProjection:
    """Additive, actor-safe history state for one Context version."""

    history_status: str
    graph_status: str
    is_visible_head: bool
    is_published_head: bool
    is_draft_head: bool
    is_alternative: bool
    is_discarded: bool
    # Which invariant(s) made the row `state_inconsistent` -- empty otherwise.
    # Stable codes, no content: a caller can tell "my save worked but the page
    # pointers disagree" from "my save did not happen".
    inconsistency_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextHistoryViolation:
    """One persisted history invariant violation found by the operator audit."""

    code: str
    page_id: UUID
    version_id: UUID | None
    repairable: bool


@dataclass(frozen=True, slots=True)
class ContextHistoryAuditReport:
    """Deterministic, non-mutating report used by migration recovery tooling."""

    pages_scanned: int
    versions_scanned: int
    violations: tuple[ContextHistoryViolation, ...]

    @property
    def repairable_violation_count(self) -> int:
        return sum(violation.repairable for violation in self.violations)

    @property
    def blocking_violation_count(self) -> int:
        return sum(not violation.repairable for violation in self.violations)


@dataclass(frozen=True, slots=True)
class ContextHistoryRepairResult:
    """Result of an atomic operator-requested history repair."""

    before: ContextHistoryAuditReport
    after: ContextHistoryAuditReport
    repaired_pages: int
    repaired_versions: int
    # Pages the entry gate excluded because at least one of their own
    # violations was not repairable. Zero whenever every touched page cleared.
    unrepaired_pages: int = 0


class ContextHistoryRepairBlockedError(RuntimeError):
    """Raised when persisted pointers do not prove a safe automatic repair."""

    def __init__(self, report: ContextHistoryAuditReport) -> None:
        super().__init__(
            "context history repair is blocked by ambiguous or invalid pointers"
        )
        self.report = report


class ContextHistoryRepairFailedError(RuntimeError):
    """Raised when canonical repair does not restore every audited invariant."""

    def __init__(self, report: ContextHistoryAuditReport) -> None:
        super().__init__("context history repair left invariant violations")
        self.report = report


def project_context_version(page, version, *, visible_version_id, expose_draft_state):
    published = version.id == page.current_published_version_id
    draft = expose_draft_state and version.version_status == "draft"
    return ContextVersionProjection(
        history_status=(
            "current_draft"
            if draft
            else "published_snapshot"
            if published
            else "closed"
            if version.version_status == "closed"
            else "discarded_draft"
            if version.version_status == "discarded"
            else "superseded_snapshot"
        ),
        graph_status="linear",
        is_visible_head=version.id == visible_version_id,
        is_published_head=published,
        is_draft_head=draft,
        is_alternative=False,
        is_discarded=version.version_status == "discarded",
    )


# Context writes apply page metadata directly until the page is first
# published, so each draft written before then also records the full values
# it was written with. Publishing or restoring an earlier draft uses them.
METADATA_SNAPSHOT_OPERATION = "metadata_snapshot"
METADATA_SNAPSHOT_TARGET = "context_page_metadata"
METADATA_SNAPSHOT_FIELDS = (
    "title",
    "page_type",
    "tags",
    "aliases",
    "placement_parent_node_id",
)


def live_draft_chain(session: Session, page: ContextPage) -> list[ContextPageVersion]:
    """The linear drafts from the latest draft down to the published head.

    Newest first, the published head itself excluded. Empty when the page has
    no draft or the chain is broken; branched alternatives are never on it.
    """

    chain: list[ContextPageVersion] = []
    version_id = page.latest_draft_version_id
    while version_id is not None and version_id != page.current_published_version_id:
        version = get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=page.workspace_id,
            resource_id=version_id,
        )
        if (
            version is None
            or version.page_id != page.id
            or any(member.id == version.id for member in chain)
            or version.version_status not in ("draft", "editing")
            or version.graph_status != "linear"
        ):
            return []
        chain.append(version)
        version_id = version.parent_version_id
    return chain if version_id == page.current_published_version_id else []


def page_metadata_snapshot(page: ContextPage) -> dict[str, object]:
    return {
        "title": page.title,
        "page_type": page.page_type,
        "tags": list(page.tags or []),
        "aliases": list(page.aliases or []),
        "placement_parent_node_id": (
            str(page.placement_parent_node_id)
            if page.placement_parent_node_id is not None
            else None
        ),
    }


def apply_page_metadata_snapshot(
    page: ContextPage, snapshot: dict[str, object]
) -> set[str]:
    """Restore recorded metadata; returns the fields whose value changed."""

    current = page_metadata_snapshot(page)
    changed = {
        field for field in METADATA_SNAPSHOT_FIELDS if current[field] != snapshot[field]
    }
    page.title = snapshot["title"]
    page.page_type = snapshot["page_type"]
    page.tags = list(snapshot["tags"] or [])
    page.aliases = list(snapshot["aliases"] or [])
    placement = snapshot["placement_parent_node_id"]
    page.placement_parent_node_id = UUID(placement) if placement else None
    return changed


def metadata_snapshot_records(
    session: Session, page: ContextPage, version_ids: Iterable[UUID]
) -> dict[UUID, tuple[dict[str, object], UUID]]:
    """Recorded metadata and the change set of the selected snapshot operation.

    A version written by a browser may have no own change_set_id. The snapshot
    operation still identifies the ledger that can carry its metadata when an
    earlier draft is first published.
    """

    refs = {
        f"context_page_version:{version_id}": version_id for version_id in version_ids
    }
    if not refs:
        return {}
    snapshots: dict[UUID, tuple[dict[str, object], UUID]] = {}
    for diff_ref, summary, change_set_id in session.execute(
        select_in_workspace(
            ChangeOperation,
            workspace_id=page.workspace_id,
            criteria=[
                ChangeOperation.target_type == METADATA_SNAPSHOT_TARGET,
                ChangeOperation.target_id == page.id,
                ChangeOperation.operation == METADATA_SNAPSHOT_OPERATION,
                ChangeOperation.diff_ref.in_(refs),
            ],
        )
        .with_only_columns(
            ChangeOperation.diff_ref,
            ChangeOperation.diff_summary,
            ChangeOperation.change_set_id,
        )
        .order_by(ChangeOperation.created_at.asc(), ChangeOperation.id.asc())
    ).all():
        try:
            value = json.loads(summary or "")
        except ValueError:
            continue
        if isinstance(value, dict) and set(METADATA_SNAPSHOT_FIELDS) <= value.keys():
            snapshots[refs[diff_ref]] = (value, change_set_id)
    return snapshots


def metadata_snapshots(
    session: Session, page: ContextPage, version_ids: Iterable[UUID]
) -> dict[UUID, dict[str, object]]:
    """The recorded pre-publication metadata of each version that has one."""

    return {
        version_id: record[0]
        for version_id, record in metadata_snapshot_records(
            session, page, version_ids
        ).items()
    }


def effective_metadata_snapshot_records(
    session: Session, page: ContextPage, version_ids: Iterable[UUID]
) -> dict[UUID, tuple[dict[str, object], UUID]]:
    """Recorded metadata plus any edit staged on the same draft since.

    Only drafts with a recorded snapshot appear: without one, a draft's
    values cannot be told apart from the page's current ones. The selected
    snapshot operation's change set is retained alongside the merged values.
    """

    from backend.services.page_service import page_metadata_patches_for_versions

    snapshots = metadata_snapshot_records(session, page, version_ids)
    patches = page_metadata_patches_for_versions(
        session, page=page, version_ids=tuple(snapshots)
    )
    effective: dict[UUID, tuple[dict[str, object], UUID]] = {}
    for version_id, (snapshot, change_set_id) in snapshots.items():
        merged = dict(snapshot)
        for field, value in patches.get(version_id, {}).items():
            if field in METADATA_SNAPSHOT_FIELDS:
                merged[field] = (
                    str(value)
                    if field == "placement_parent_node_id" and value is not None
                    else value
                )
        effective[version_id] = (merged, change_set_id)
    return effective


def effective_metadata_snapshots(
    session: Session, page: ContextPage, version_ids: Iterable[UUID]
) -> dict[UUID, dict[str, object]]:
    """The effective metadata values, without their source change sets."""

    return {
        version_id: record[0]
        for version_id, record in effective_metadata_snapshot_records(
            session, page, version_ids
        ).items()
    }


def record_metadata_snapshot(
    session: Session,
    *,
    change_set_id: UUID,
    page: ContextPage,
    version: ContextPageVersion,
) -> None:
    if not version.page_metadata:
        version.page_metadata = page_metadata_snapshot(page)


def record_unarchive(
    session: Session,
    *,
    page: ContextPage,
    actor_kind: str,
    actor_id: str,
) -> None:
    """Log that a revived archived page's published version is live again.

    Reviving re-exposes the published head without publishing anything, so
    without this the publish timeline (what_changed, as_of reads) would end
    at the archive and never learn the page came back.
    """

    if page.current_published_version_id is None:
        return
    change_set = ChangeSet(
        workspace_id=page.workspace_id,
        reason=UNARCHIVE_REASON,
        trigger_type="context_page_unarchive",
        actor_type=actor_kind,
        actor_id=parse_uuid_or_none(actor_id),
        status="applied",
    )
    session.add(change_set)
    session.flush()
    session.add(
        ChangeOperation(
            workspace_id=page.workspace_id,
            change_set_id=change_set.id,
            target_type="context_page",
            target_id=page.id,
            operation="unarchive",
            diff_ref=f"context_page_version:{page.current_published_version_id}",
        )
    )


def is_publishable_draft(session, page, version):
    return version.page_id == page.id and version.version_status in {
        "draft",
        "closed",
        "superseded_snapshot",
    }


@dataclass(frozen=True)
class ContextRejectionPlan:
    """What rejecting one draft of the live chain does to its page."""

    # restore_previous_draft, restore_published or hide_unpublished.
    effect: str
    # The rejected draft and every later draft written on it, newest first,
    # plus any already-rejected legacy ancestors skipped while restoring.
    retired: tuple[ContextPageVersion, ...]
    # restore_previous_draft: the earlier draft that becomes the edit head.
    destination: ContextPageVersion | None
    # A private editing row would be retired with them, or would become the
    # head; the rejection must wait for it.
    blocked: bool
    # Never-published page: the destination's recorded metadata, if any.
    destination_metadata: dict[str, object] | None
    never_published: bool = False

    @property
    def metadata_kept(self) -> bool:
        """A never-published page keeps its current metadata: the restored
        draft predates per-draft metadata records."""
        return (
            self.never_published
            and self.destination is not None
            and self.destination_metadata is None
        )


def plan_context_rejection(
    session: Session, page: ContextPage, version: ContextPageVersion
) -> ContextRejectionPlan | None:
    """Rejecting a draft also rejects the later drafts written on it.

    The rejected draft and every draft above it on the live chain retire, and
    the edit head returns to the draft below it, or to the published version
    (or nothing) when it was the oldest. Earlier drafts and branched
    alternatives are untouched. A version off the chain moves nothing: None.
    """

    if (
        version.page_id != page.id
        or version.version_status != "draft"
        or version.graph_status != "linear"
    ):
        return None
    chain = live_draft_chain(session, page)
    chain_ids = [member.id for member in chain]
    if version.id in chain_ids:
        position = chain_ids.index(version.id)
        rejected_ancestor_ids = _rejected_version_ids(
            session, page, chain[position + 1 :]
        )
        # Older rejections left their drafts linear. Never reopen a review for
        # those exact rejected bytes: retire them and return to what survives
        # below them, while their original rejection decisions stay intact.
        while (
            position + 1 < len(chain)
            and chain[position + 1].version_status == "draft"
            and chain[position + 1].id in rejected_ancestor_ids
        ):
            position += 1
        retired = tuple(chain[: position + 1])
        destination = chain[position + 1] if position + 1 < len(chain) else None
    elif page.latest_draft_version_id == version.id:
        # A latest draft whose chain no longer reaches the published head.
        retired, destination = (version,), None
    else:
        return None
    never_published = page.current_published_version_id is None
    return ContextRejectionPlan(
        effect=(
            "restore_previous_draft"
            if destination is not None
            else "hide_unpublished"
            if never_published
            else "restore_published"
        ),
        retired=retired,
        destination=destination,
        blocked=any(member.version_status == "editing" for member in retired)
        or (destination is not None and destination.version_status != "draft"),
        destination_metadata=(
            effective_metadata_snapshots(session, page, [destination.id]).get(
                destination.id
            )
            if destination is not None and never_published
            else None
        ),
        never_published=never_published,
    )


def apply_context_rejection(
    session: Session, page: ContextPage, plan: ContextRejectionPlan
) -> tuple[ContextPageVersion | None, set[str]]:
    """Retire the rejected range and move the page's heads.

    Returns the version projections now follow (the restored draft, the
    published head, or None for a hidden page) and the metadata fields a
    never-published page restored from that draft.
    """

    retired_ids = {member.id for member in plan.retired}
    for member in plan.retired:
        # Retired, not deleted: the bytes stay readable review evidence; only
        # their place in the editable chain ends.
        member.graph_status = "discarded"
        session.add(member)
    published_id = page.current_published_version_id
    restored_fields: set[str] = set()
    if plan.destination is not None:
        page.latest_draft_version_id = plan.destination.id
        if page.current_version_id is None or page.current_version_id in retired_ids:
            page.current_version_id = plan.destination.id
        if plan.destination_metadata is not None:
            restored_fields = apply_page_metadata_snapshot(
                page, plan.destination_metadata
            )
        resulting = plan.destination
    else:
        page.latest_draft_version_id = None
        page.current_version_id = published_id
        resulting = (
            get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=page.workspace_id,
                resource_id=published_id,
            )
            if published_id is not None
            else None
        )
    if page.lifecycle_status != "archived":
        page.lifecycle_status = "published" if published_id is not None else "draft"
    session.add(page)
    session.flush()
    return resulting, restored_fields


@dataclass(frozen=True)
class RejectedDraftRestart:
    """The head a rejected draft's bytes may restart on (None: no head)."""

    parent_version_id: UUID | None
    # The successful write retires this proven range, including legacy drafts
    # that a historical rejection left linear. Resolving a restart stays pure.
    retired_versions: tuple[ContextPageVersion, ...] = ()


def rejected_draft_restart(
    session: Session,
    page: ContextPage,
    version: ContextPageVersion,
) -> RejectedDraftRestart | None:
    """Where a new draft may continue from a rejected draft's bytes.

    A rejection retires the rejected draft and the later drafts written on
    it and returns the edit head to what lies below them. Any of those
    drafts may restart from its bytes on that head while the head has not
    moved since. A draft rejected before drafts were retired is still linear,
    but likewise restarts on its surviving parent only while that parent is
    still the page's head.
    """

    if (
        version.workspace_id != page.workspace_id
        or version.page_id != page.id
        or version.version_status != "draft"
        or version.graph_status not in {"linear", "discarded"}
    ):
        return None
    legacy_linear = version.graph_status == "linear"
    ancestors: list[ContextPageVersion] = []
    cursor: ContextPageVersion | None = version
    seen: set[UUID] = set()
    while (
        cursor is not None
        and cursor.version_status == "draft"
        and cursor.graph_status in {"linear", "discarded"}
    ):
        if (
            cursor.workspace_id != page.workspace_id
            or cursor.page_id != page.id
            or cursor.id in seen
        ):
            return None
        seen.add(cursor.id)
        ancestors.append(cursor)
        if cursor.parent_version_id is None:
            cursor = None
            break
        cursor = get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=page.workspace_id,
            resource_id=cursor.parent_version_id,
        )
        if cursor is None:
            return None
    rejected_ids = _rejected_version_ids(session, page, ancestors)
    if legacy_linear and version.id not in rejected_ids:
        return None
    retired_ids: set[UUID] = set()
    for member in ancestors:
        if member.graph_status != "discarded" and member.id not in rejected_ids:
            cursor = member
            break
        retired_ids.add(member.id)
    surviving_id = cursor.id if cursor is not None else None
    head_id = page.latest_draft_version_id or page.current_published_version_id
    if surviving_id != head_id or not retired_ids.intersection(rejected_ids):
        return None
    if cursor is not None:
        # Pointer equality alone cannot bless an editing, branched, foreign or
        # broken head. Reuse the publication topology gate before attaching a
        # new submitted draft to it; no body or review state is changed here.
        try:
            assert_context_publish_candidate(
                session,
                page=page,
                version=cursor,
                require_latest_draft=page.latest_draft_version_id is not None,
            )
        except StaleContextDraftError:
            return None
    return RejectedDraftRestart(
        parent_version_id=surviving_id,
        retired_versions=tuple(
            member for member in ancestors if member.id in retired_ids
        ),
    )


def _rejected_version_ids(
    session: Session, page: ContextPage, versions: list[ContextPageVersion]
) -> set[UUID]:
    """Exact page versions whose original review still records a rejection."""

    versions = [
        version
        for version in versions
        if version.workspace_id == page.workspace_id and version.page_id == page.id
    ]
    if not versions:
        return set()
    versions_by_change_set: dict[UUID, set[UUID]] = {}
    refs = {f"context_page_version:{version.id}": version.id for version in versions}
    # Publication accepts both the version's producing ledger and later exact
    # operations (metadata edits, no-op revisions) as its review sources.
    for version in versions:
        if version.change_set_id is not None:
            versions_by_change_set.setdefault(version.change_set_id, set()).add(
                version.id
            )
    for diff_ref, change_set_id in session.execute(
        select_in_workspace(
            ChangeOperation,
            workspace_id=page.workspace_id,
            criteria=[
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page.id,
                ChangeOperation.diff_ref.in_(refs),
            ],
        ).with_only_columns(ChangeOperation.diff_ref, ChangeOperation.change_set_id)
    ).all():
        versions_by_change_set.setdefault(change_set_id, set()).add(refs[diff_ref])
    if not versions_by_change_set:
        return set()
    rejected_change_set_ids = session.scalars(
        select_in_workspace(
            ReviewItem,
            workspace_id=page.workspace_id,
            criteria=[
                ReviewItem.change_set_id.in_(versions_by_change_set),
                ReviewItem.review_type == "context_update",
                ReviewItem.status == "rejected",
            ],
        ).with_only_columns(ReviewItem.change_set_id)
    ).all()
    return {
        version_id
        for change_set_id in rejected_change_set_ids
        for version_id in versions_by_change_set[change_set_id]
    }


def assert_context_publish_candidate(
    session, *, page, version, require_latest_draft, allow_chain_ancestor=False
):
    if (
        version.page_id != page.id
        or version.workspace_id != page.workspace_id
        or version.version_status
        not in {"draft", "closed", "published_snapshot", "superseded_snapshot"}
    ):
        raise StaleContextDraftError()
    from backend.services.context_draft_workflow import parents_for

    for key in parents_for(session, version):
        parent = get_in_workspace(
            session, ContextPageVersion, workspace_id=page.workspace_id, resource_id=key
        )
        if parent is None or parent.page_id != page.id or not parent.commit_oid:
            raise StaleContextDraftError("Context parents must be immutable commits")


def audit_context_history(
    session: Session,
    *,
    lock: bool = False,
) -> ContextHistoryAuditReport:
    """Inspect persisted published-lineage invariants without changing rows.

    Every non-null pointer must resolve to a compatible version on the same
    page. The published pointer is the only durable proof of which legacy
    snapshot is canonical, so contradictory edit-head pointers are reported as
    blocking instead of guessing from version numbers or dates.
    ``lock=True`` is reserved for the apply transaction so the audit and repair
    see one stable set of page/version rows on databases that support row locks.
    """

    page_statement = select(ContextPage).order_by(ContextPage.id)
    version_statement = select(ContextPageVersion).order_by(
        ContextPageVersion.page_id,
        ContextPageVersion.version_no,
        ContextPageVersion.id,
    )
    if lock:
        page_statement = page_statement.with_for_update()
        version_statement = version_statement.with_for_update()
    pages = list(session.scalars(page_statement).all())
    versions = list(session.scalars(version_statement).all())
    versions_by_id = {version.id: version for version in versions}
    versions_by_page: dict[UUID, list[ContextPageVersion]] = {}
    for version in versions:
        versions_by_page.setdefault(version.page_id, []).append(version)

    violations: list[ContextHistoryViolation] = []
    for page in pages:
        page_versions = versions_by_page.get(page.id, [])
        published_rows = [
            version
            for version in page_versions
            if version.version_status == "published_snapshot"
        ]
        stored_published_head = versions_by_id.get(page.current_published_version_id)
        proven_published_head = (
            stored_published_head
            if stored_published_head is not None
            and stored_published_head.page_id == page.id
            else (
                published_rows[0]
                if page.current_published_version_id is None
                and len(published_rows) == 1
                else None
            )
        )
        stored_current = versions_by_id.get(page.current_version_id)
        current_draft_is_valid = bool(
            stored_current is not None
            and stored_current.page_id == page.id
            and stored_current.version_status == "draft"
            and stored_current.graph_status == "linear"
        )
        stored_latest_draft = versions_by_id.get(page.latest_draft_version_id)
        latest_draft_is_valid = bool(
            stored_latest_draft is not None
            and stored_latest_draft.page_id == page.id
            and stored_latest_draft.version_status == "draft"
            and stored_latest_draft.graph_status == "linear"
        )
        proven_draft = _proven_linear_draft(
            page_versions,
            current_draft=stored_current if current_draft_is_valid else None,
        )
        linear_drafts = _linear_draft_rows(page_versions)
        # An unset current pointer has no stored value to contradict, so a valid
        # `latest_draft` pointer is proof for it exactly as a valid current
        # pointer is proof for the draft pointer.
        proven_edit_head = (
            stored_latest_draft if latest_draft_is_valid else proven_draft
        )
        current_pointer_repairable = bool(
            latest_draft_is_valid
            # A uniquely proven draft is as good a replacement for the current
            # pointer as a valid `latest_draft` pointer, so a page whose two
            # edit-head pointers are both corrupt is still recoverable when the
            # page holds exactly one linear draft.
            or proven_draft is not None
            or proven_published_head is not None
        )
        # The published head proves nothing about the draft pointer: repairing
        # that pointer can only name a draft or clear it, and clearing it while
        # a live draft row exists would strand the staged edit behind a clean
        # audit. Only a uniquely proven draft, or the proven absence of any
        # linear draft row, decides this pointer.
        latest_pointer_repairable = bool(proven_draft is not None or not linear_drafts)
        # An unset pointer is a distinct defect from a corrupt one: nothing
        # contradicts, so nothing above fires, and the page audits clean while a
        # staged draft stays unreachable. Both classes are decided by the same
        # proof, so they cannot drift apart.
        # A page whose every version left the editable chain (all rejected,
        # say) has nothing for the pointer to name; that is not a defect.
        if page.current_version_id is None and any(
            version.graph_status == "linear" for version in page_versions
        ):
            violations.append(
                ContextHistoryViolation(
                    code="current_version_unset",
                    page_id=page.id,
                    version_id=None,
                    # A draft outranks the published head, and the head is only
                    # a safe answer when no live draft could have been meant.
                    repairable=bool(
                        proven_edit_head is not None
                        or (not linear_drafts and proven_published_head is not None)
                    ),
                )
            )
        if page.latest_draft_version_id is None and linear_drafts:
            violations.append(
                ContextHistoryViolation(
                    code="latest_draft_unset_with_live_draft",
                    page_id=page.id,
                    version_id=None,
                    repairable=proven_draft is not None,
                )
            )
        current_id = page.current_version_id
        if current_id is not None:
            current = versions_by_id.get(current_id)
            if current is None:
                violations.append(
                    ContextHistoryViolation(
                        code="current_version_missing",
                        page_id=page.id,
                        version_id=current_id,
                        repairable=current_pointer_repairable,
                    )
                )
            elif current.page_id != page.id:
                violations.append(
                    ContextHistoryViolation(
                        code="current_version_cross_page",
                        page_id=page.id,
                        version_id=current_id,
                        repairable=False,
                    )
                )
            elif current_id != page.current_published_version_id and (
                current.version_status != "draft" or current.graph_status != "linear"
            ):
                violations.append(
                    ContextHistoryViolation(
                        code="current_version_status_contradiction",
                        page_id=page.id,
                        version_id=current_id,
                        repairable=current_pointer_repairable,
                    )
                )

        latest_draft_id = page.latest_draft_version_id
        if latest_draft_id is not None:
            latest_draft = versions_by_id.get(latest_draft_id)
            if latest_draft is None:
                violations.append(
                    ContextHistoryViolation(
                        code="latest_draft_missing",
                        page_id=page.id,
                        version_id=latest_draft_id,
                        repairable=latest_pointer_repairable,
                    )
                )
            elif latest_draft.page_id != page.id:
                violations.append(
                    ContextHistoryViolation(
                        code="latest_draft_cross_page",
                        page_id=page.id,
                        version_id=latest_draft_id,
                        repairable=False,
                    )
                )
            elif latest_draft_id != page.current_published_version_id and (
                latest_draft.version_status != "draft"
                or latest_draft.graph_status != "linear"
            ):
                violations.append(
                    ContextHistoryViolation(
                        code="latest_draft_status_contradiction",
                        page_id=page.id,
                        version_id=latest_draft_id,
                        repairable=latest_pointer_repairable,
                    )
                )

        head_id = page.current_published_version_id
        if head_id is None:
            if published_rows:
                violations.append(
                    ContextHistoryViolation(
                        code="published_without_head",
                        page_id=page.id,
                        version_id=None,
                        repairable=len(published_rows) == 1,
                    )
                )
            continue

        head = versions_by_id.get(head_id)
        if head is None:
            violations.append(
                ContextHistoryViolation(
                    code="published_head_missing",
                    page_id=page.id,
                    version_id=head_id,
                    repairable=False,
                )
            )
            continue
        if head.page_id != page.id:
            violations.append(
                ContextHistoryViolation(
                    code="published_head_cross_page",
                    page_id=page.id,
                    version_id=head_id,
                    repairable=False,
                )
            )
            continue

        if head.version_status != "published_snapshot":
            violations.append(
                ContextHistoryViolation(
                    code="published_head_status_mismatch",
                    page_id=page.id,
                    version_id=head.id,
                    repairable=True,
                )
            )
        if head.graph_status != "linear":
            violations.append(
                ContextHistoryViolation(
                    code="published_head_graph_mismatch",
                    page_id=page.id,
                    version_id=head.id,
                    repairable=True,
                )
            )
        if page.latest_draft_version_id == head.id:
            violations.append(
                ContextHistoryViolation(
                    code="published_head_marked_as_draft_head",
                    page_id=page.id,
                    version_id=head.id,
                    # This is a broken draft pointer like any other: clearing
                    # the overlap is only safe when no live linear draft is
                    # being hidden behind it, so it takes the same proof.
                    repairable=latest_pointer_repairable,
                )
            )
        for version in page_versions:
            if version.id != head.id and version.version_status == "published_snapshot":
                violations.append(
                    ContextHistoryViolation(
                        code="extra_published_snapshot",
                        page_id=page.id,
                        version_id=version.id,
                        repairable=True,
                    )
                )
            if (
                version.id != head.id
                and version.version_status == "superseded_snapshot"
                and version.graph_status != "linear"
            ):
                violations.append(
                    ContextHistoryViolation(
                        code="snapshot_graph_mismatch",
                        page_id=page.id,
                        version_id=version.id,
                        repairable=True,
                    )
                )

    violations.sort(
        key=lambda violation: (
            str(violation.page_id),
            violation.code,
            str(violation.version_id or ""),
        )
    )
    return ContextHistoryAuditReport(
        pages_scanned=len(pages),
        versions_scanned=len(versions),
        violations=tuple(violations),
    )


def _linear_draft_rows(
    versions: Iterable[ContextPageVersion],
) -> list[ContextPageVersion]:
    """Every row that is still an editable linear draft on this page."""

    return [
        version
        for version in versions
        if version.version_status == "draft" and version.graph_status == "linear"
    ]


def _proven_linear_draft(
    versions: Iterable[ContextPageVersion],
    *,
    current_draft: ContextPageVersion | None,
) -> ContextPageVersion | None:
    """The one draft a broken ``latest_draft`` pointer may be restored to.

    Two things count as proof: the current pointer already names a valid linear
    draft, or the page holds exactly one linear draft row. An active review may
    corroborate such a candidate, but it never breaks a tie -- with two or more
    linear drafts and no current-pointer proof the pointer stays unrepairable,
    because the contract aborts on ambiguity instead of guessing a head.
    """

    if current_draft is not None:
        return current_draft
    drafts = _linear_draft_rows(versions)
    return drafts[0] if len(drafts) == 1 else None


def repair_context_history(
    session: Session, *, context_version_writer=None
) -> ContextHistoryRepairResult:
    """Repair every unambiguous published-lineage violation, page by page.

    The caller owns the transaction.  The entry gate and the residual check
    are both scoped to one page: a page carrying its own unrepairable
    violation is excluded up front, and a page that still fails after repair
    rolls the whole transaction back (an unexpected regression, not routine
    ambiguity). Neither scope lets one ambiguous page block repair of every
    other page, so a cross-page pointer or a genuinely ambiguous draft on one
    page no longer withholds --apply from the rest of the installation.
    Repeated ``--apply`` invocations stay idempotent for the pages this
    repairs; pages it excludes need operator intervention or the root-cause
    write-path fix (Context publication demoting stale linear drafts) before
    a later ``--apply`` can reach them.
    """

    from backend.services.db_write_intent import acquire_write_intent

    acquire_write_intent(session)
    before = audit_context_history(session, lock=True)
    blocked_page_ids = {
        violation.page_id for violation in before.violations if not violation.repairable
    }
    repair_page_ids = {
        violation.page_id for violation in before.violations if violation.repairable
    } - blocked_page_ids
    # An already-clean database (no violations at all, e.g. a repeated
    # `--apply`) must succeed as a no-op exactly like before this change:
    # only refuse when a blocked page actually exists and left nothing this
    # call can safely repair. `repair_page_ids` alone cannot distinguish
    # "nothing to do" from "everything is blocked".
    if not repair_page_ids and blocked_page_ids:
        raise ContextHistoryRepairBlockedError(before)

    repaired_versions = 0
    for page_id in sorted(repair_page_ids, key=str):
        page = session.get(ContextPage, page_id)
        if page is None:
            # Pages were locked by the audit. Treat an unexpected disappearance
            # as a failed invariant instead of silently reporting success.
            raise ContextHistoryRepairFailedError(audit_context_history(session))
        _repair_context_head_pointers(session, page=page)
        if page.current_published_version_id is not None:
            repaired_versions += repair_context_published_lineage(
                session,
                page=page,
                context_version_writer=context_version_writer,
            )
    session.flush()

    after = audit_context_history(session)
    residual = tuple(
        violation
        for violation in after.violations
        if violation.page_id in repair_page_ids
    )
    if residual:
        # A page this repair touched is still in violation: a genuine failure
        # of the repair contract, not the routine ambiguity the entry gate
        # already excluded. Keep the all-or-nothing rollback for that case.
        raise ContextHistoryRepairFailedError(after)
    return ContextHistoryRepairResult(
        before=before,
        after=after,
        repaired_pages=len(repair_page_ids),
        repaired_versions=repaired_versions,
        unrepaired_pages=len(blocked_page_ids),
    )


def repair_context_published_lineage(
    session: Session,
    *,
    context_version_writer=None,
    page: ContextPage,
) -> int:
    """Repair one page only when its stored published pointer proves the head."""

    head_id = page.current_published_version_id
    head = session.get(ContextPageVersion, head_id) if head_id is not None else None
    if head is None or head.page_id != page.id:
        raise ContextHistoryRepairBlockedError(audit_context_history(session))
    _require_repair_candidate_allowed(session, page=page, version=head)

    repaired_versions = _normalize_context_published_rows(
        session,
        context_version_writer=context_version_writer,
        page=page,
        version=head,
    )
    if page.latest_draft_version_id == head.id:
        # Same rule as the pointer repair: the redundant overlap may only be
        # cleared when it is not the last thing masking a live linear draft.
        page_versions = list(
            session.scalars(
                select(ContextPageVersion).where(ContextPageVersion.page_id == page.id)
            ).all()
        )
        proven_draft = _proven_linear_draft(page_versions, current_draft=None)
        if proven_draft is None and _linear_draft_rows(page_versions):
            raise ContextHistoryRepairBlockedError(audit_context_history(session))
        if proven_draft is not None:
            _require_repair_candidate_allowed(
                session,
                page=page,
                version=proven_draft,
            )
        page.latest_draft_version_id = (
            proven_draft.id if proven_draft is not None else None
        )
        session.add(page)
    return repaired_versions


def _repair_context_head_pointers(
    session: Session,
    *,
    page: ContextPage,
) -> None:
    """Repair only pointers for which another stored pointer proves the head."""

    versions = list(
        session.scalars(
            select(ContextPageVersion)
            .where(ContextPageVersion.page_id == page.id)
            .with_for_update()
        ).all()
    )
    versions_by_id = {version.id: version for version in versions}
    published_rows = [
        version
        for version in versions
        if version.version_status == "published_snapshot"
    ]
    published_head = versions_by_id.get(page.current_published_version_id)
    if page.current_published_version_id is None and len(published_rows) == 1:
        published_head = published_rows[0]
        _require_repair_candidate_allowed(
            session,
            page=page,
            version=published_head,
        )
        page.current_published_version_id = published_head.id
    if published_head is not None and published_head.page_id != page.id:
        published_head = None

    current = versions_by_id.get(page.current_version_id)
    current_draft = (
        current
        if current is not None
        and current.version_status == "draft"
        and current.graph_status == "linear"
        else None
    )
    latest = versions_by_id.get(page.latest_draft_version_id)
    latest_draft = (
        latest
        if latest is not None
        and latest.version_status == "draft"
        and latest.graph_status == "linear"
        else None
    )

    linear_drafts = _linear_draft_rows(versions)
    if page.latest_draft_version_id is not None and latest_draft is None:
        # Restore the draft this pointer must have named, and clear it only when
        # the page proves no linear draft exists. The audit refuses to classify
        # an ambiguous page as repairable, so reaching here without a proven
        # draft means the page genuinely has none -- if it does not, the audit
        # and this writer have drifted apart and clearing the pointer would
        # strand live drafts behind a clean report, so refuse the whole repair.
        proven_draft = _proven_linear_draft(versions, current_draft=current_draft)
        if proven_draft is None and linear_drafts:
            raise ContextHistoryRepairBlockedError(audit_context_history(session))
        if proven_draft is not None:
            _require_repair_candidate_allowed(
                session,
                page=page,
                version=proven_draft,
            )
        page.latest_draft_version_id = (
            proven_draft.id if proven_draft is not None else None
        )
        latest_draft = proven_draft
    elif page.latest_draft_version_id is None and linear_drafts:
        # Unset while live drafts exist: the pointer was never written, so the
        # draft is reachable from nothing. Same proof, same refusal.
        proven_draft = _proven_linear_draft(versions, current_draft=current_draft)
        if proven_draft is None:
            raise ContextHistoryRepairBlockedError(audit_context_history(session))
        _require_repair_candidate_allowed(
            session,
            page=page,
            version=proven_draft,
        )
        page.latest_draft_version_id = proven_draft.id
        latest_draft = proven_draft
    if page.current_version_id is not None and (
        current is None
        or current.page_id != page.id
        or (
            current.id != page.current_published_version_id
            and (current.version_status != "draft" or current.graph_status != "linear")
        )
    ):
        replacement = latest_draft or published_head
        if replacement is not None:
            _require_repair_candidate_allowed(
                session,
                page=page,
                version=replacement,
            )
            page.current_version_id = replacement.id
    elif page.current_version_id is None:
        replacement = latest_draft or published_head
        if replacement is not None:
            _require_repair_candidate_allowed(
                session,
                page=page,
                version=replacement,
            )
            page.current_version_id = replacement.id
    if published_head is not None and page.latest_draft_version_id == published_head.id:
        page.latest_draft_version_id = None
    if (
        page.is_public
        and page.public_exposed_version_id != page.current_published_version_id
    ):
        clear_public_page_exposure(page)
    session.add(page)
    session.flush()


def _normalize_context_published_rows(
    session: Session,
    *,
    context_version_writer=None,
    page: ContextPage,
    version: ContextPageVersion,
) -> int:
    """Demote old snapshots before promoting one proven canonical head."""

    from backend.services.context_version_storage import (
        align_publication,
        prepare_publication,
    )

    prepared = prepare_publication(session, page, version, context_version_writer)
    prior_snapshots = session.scalars(
        select(ContextPageVersion)
        .where(
            ContextPageVersion.page_id == page.id,
            ContextPageVersion.id != version.id,
            ContextPageVersion.version_status.in_(
                ("published_snapshot", "superseded_snapshot")
            ),
        )
        .with_for_update()
    ).all()
    repaired_versions = 0
    demoted_published = False
    for previous in prior_snapshots:
        changed = False
        if previous.version_status == "published_snapshot":
            previous.version_status = "superseded_snapshot"
            demoted_published = True
            changed = True
        if previous.graph_status != "linear":
            previous.graph_status = "linear"
            changed = True
        if changed:
            repaired_versions += 1
            session.add(previous)

    # The partial unique index permits only one published_snapshot per page.
    # Persist demotions before promoting the candidate; relying on ORM UPDATE
    # ordering would make correctness depend on UUID sort order.
    if demoted_published:
        session.flush()

    if (
        version.version_status != "published_snapshot"
        or version.graph_status != "linear"
    ):
        repaired_versions += 1
    version.commit_oid = prepared.commit_oid
    version.content_byte_size = prepared.content_byte_size
    version.version_status = "published_snapshot"
    version.graph_status = "linear"
    session.add(version)

    session.flush()

    align_publication(session, page, prepared.commit_oid, context_version_writer)
    return repaired_versions


def _require_repair_candidate_allowed(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
) -> None:
    """Fail a pointer repair before it makes legacy media reachable again."""

    require_no_visible_legacy_media(
        workspace_id=page.workspace_id,
        body_markdown=version.body_md,
    )


@dataclass(frozen=True)
class ContextDecisionRecord:
    version: ContextPageVersion | None
    previous_published_version_id: UUID | None
    change_set: ChangeSet | None
    audit_event: object | None
    restored: dict[str, set[str]] | None = None


def record_context_decision(
    session: Session,
    *,
    action: str,
    page: ContextPage,
    version: ContextPageVersion | None = None,
    context_version_writer=None,
    actor_kind: str = "system",
    actor_id: str = "internal",
    actor=None,
    reason: str,
    risk_level: str = "low",
    audit_service=None,
    self_origins=None,
    effect: dict | None = None,
    review_decision_id: UUID | None = None,
    existing_change_set: ChangeSet | None = None,
    existing_audit_event=None,
    event_action: str | None = None,
) -> ContextDecisionRecord:
    """Commit a Context transition and its durable operation/audit together.

    Review permissions and ReviewDecision rows remain with ReviewService. This
    entry owns every Context close, published-ref movement and undo; the Git
    preparation/alignment functions below it are storage primitives.
    """
    from backend.services.audit_service import AuditService
    from backend.services.context_version_storage import (
        ContextStorageServiceError,
        align_publication,
        freeze_context_version,
    )

    with session.begin_nested():
        previous_id = page.current_published_version_id
        restored = None
        if action == "publish":
            if version is None:
                raise ValueError("publication requires a version")
            _apply_context_publication(
                session,
                context_version_writer=context_version_writer,
                page=page,
                version=version,
                require_latest_draft=False,
                actor=actor,
                self_origins=self_origins,
            )
            operation, trigger = "publish", "context_page_publish"
            audit_action = "context_page.draft.publish"
        elif action == "close":
            if version is None:
                raise ValueError("closing requires a version")
            freeze_context_version(session, page, version, context_version_writer)
            operation, trigger = "close", "context_draft_close"
            audit_action = "context_page.draft.close"
        elif action == "undo":
            from backend.services.review_decision_effect import restore_effect

            if effect is None:
                raise ValueError("undo requires a recorded decision effect")
            restored = restore_effect(session, page.workspace_id, effect)
            if "current_published_version_id" not in restored.get(
                "context_page", set()
            ):
                return ContextDecisionRecord(None, previous_id, None, None, restored)
            version = (
                get_in_workspace(
                    session,
                    ContextPageVersion,
                    workspace_id=page.workspace_id,
                    resource_id=page.current_published_version_id,
                )
                if page.current_published_version_id
                else None
            )
            if page.current_published_version_id and (
                version is None or version.page_id != page.id or not version.commit_oid
            ):
                raise ContextStorageServiceError("context_snapshot_invalid")
            align_publication(
                session,
                page,
                version.commit_oid if version else None,
                context_version_writer,
            )
            operation, trigger = "publish_revert", "context_page_publish_revert"
            audit_action = "context_page.publication.undo"
        elif action == "replay":
            if version is None or version.id != page.current_published_version_id:
                raise ContextStorageServiceError("context_snapshot_invalid")
            if existing_change_set is None or existing_audit_event is None:
                raise ValueError("publication replay requires its existing ledger")
            align_publication(session, page, version.commit_oid, context_version_writer)
            return ContextDecisionRecord(
                version, previous_id, existing_change_set, existing_audit_event
            )
        else:
            raise ValueError(f"unknown Context decision: {action}")

        change_set = ChangeSet(
            workspace_id=page.workspace_id,
            reason=reason,
            trigger_type=trigger,
            actor_type=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            status="applied",
            risk_level=risk_level,
        )
        session.add(change_set)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=page.workspace_id,
                change_set_id=change_set.id,
                target_type="context_page",
                target_id=page.id,
                operation=operation,
                diff_summary=reason,
                diff_ref=f"context_page_version:{version.id}" if version else None,
            )
        )
        audit = (audit_service or AuditService()).emit(
            session=session,
            workspace_id=page.workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action=event_action or audit_action,
            target_kind="context_page",
            target_id=page.id,
            payload={
                "reason": reason,
                "version_id": str(version.id) if version else None,
                "previous_published_version_id": str(previous_id)
                if previous_id
                else None,
                "change_set_id": str(change_set.id),
                **(
                    {"review_decision_id": str(review_decision_id)}
                    if review_decision_id
                    else {}
                ),
            },
        )
        session.flush()
        return ContextDecisionRecord(version, previous_id, change_set, audit, restored)


def _apply_context_publication(
    session,
    *,
    context_version_writer=None,
    page,
    version,
    require_latest_draft,
    allow_chain_ancestor=False,
    actor=None,
    self_origins=None,
):
    """Publish one prepared merge result; unrelated drafts remain untouched."""
    assert_context_publish_candidate(
        session, page=page, version=version, require_latest_draft=require_latest_draft
    )
    require_no_visible_legacy_media(
        workspace_id=page.workspace_id, body_markdown=version.body_md
    )
    from backend.services.context_draft_workflow import metadata_dict, metadata_for
    from backend.services.document_references import publish_document_references

    previous_id = page.current_published_version_id
    if previous_id != version.id:
        publish_document_references(
            session, version=version, actor=actor, self_origins=self_origins
        )
        previous = (
            get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=page.workspace_id,
                resource_id=previous_id,
            )
            if previous_id
            else None
        )
        if previous is None or (
            normalize_content_recency(parse_page_frontmatter(previous.body_md).body)
            != normalize_content_recency(parse_page_frontmatter(version.body_md).body)
            or previous.rendering_profile != version.rendering_profile
        ):
            page.content_changed_at = utc_now()
    _normalize_context_published_rows(
        session,
        page=page,
        version=version,
        context_version_writer=context_version_writer,
    )
    apply_page_metadata_snapshot(page, metadata_dict(metadata_for(page, version)))
    page.current_published_version_id = version.id
    page.current_version_id = version.id
    page.lifecycle_status = "published"
    reconcile_public_exposure_after_publication(
        page, previous_published_version_id=previous_id, published_version_id=version.id
    )
    session.add_all([page, version])
    return previous_id


def apply_context_publication(
    session,
    *,
    page,
    version,
    context_version_writer=None,
    require_latest_draft=False,
    allow_chain_ancestor=False,
    actor=None,
    self_origins=None,
):
    """Internal publication callers share the common decision recorder."""
    del require_latest_draft, allow_chain_ancestor
    result = record_context_decision(
        session,
        action="publish",
        page=page,
        version=version,
        context_version_writer=context_version_writer,
        actor=actor,
        actor_kind=getattr(actor, "actor_kind", "system"),
        actor_id=getattr(actor, "actor_id", "internal"),
        reason="Context content published",
        self_origins=self_origins,
    )
    return result.previous_published_version_id
