"""Canonical Context version publication and history projections.

The database keeps the original compact version-status enum.  This module is
the one place that turns those durable rows plus page pointers into the richer
history vocabulary used by readers, and the one place that advances a
published head.  Keeping both decisions together prevents browser writes,
review approval, and Context/MCP publication from drifting apart again.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.context import ContextPage, ContextPageVersion
from backend.repositories.workspace_scoped import get_in_workspace
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

CONTEXT_HISTORY_STATUSES = (
    "state_inconsistent",
    "current_draft",
    "published_snapshot",
    "alternative_draft",
    "discarded_draft",
    "superseded_snapshot",
    "superseded_draft",
)

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


def project_context_version(
    page: ContextPage,
    version: ContextPageVersion,
    *,
    visible_version_id: UUID | None,
    expose_draft_state: bool,
) -> ContextVersionProjection:
    """Project a raw row without disclosing a hidden draft to viewers.

    ``history_status`` uses the normative precedence encoded below.  A viewer
    never receives a draft row, and hidden draft pointers are deliberately not
    considered while projecting its published row; otherwise an
    ``state_inconsistent`` or head flag could reveal that private work exists.
    """

    is_published_head = version.id == page.current_published_version_id
    actual_draft_head = version.id == page.latest_draft_version_id
    is_draft_head = expose_draft_state and actual_draft_head
    is_alternative = (
        version.version_status == "draft" and version.graph_status == "branched"
    )
    is_discarded = (
        version.version_status == "draft" and version.graph_status == "discarded"
    )
    # P2.2: an "editing" row reaching this function has already been through
    # the actor-ownership visibility check upstream (the same trust boundary
    # this function already relies on for `is_draft_head`/`is_published_head`
    # -- it does not independently re-derive who may see `version`), so any
    # "editing" row seen here is either the caller's own or something a
    # caller that skipped that check is responsible for, not something this
    # projection can or should re-verify.
    is_editing = version.version_status == "editing"

    # P8.1: every in-place save of an open "editing" row points
    # latest_draft_version_id at it (PageService._set_current_version), so an
    # editing draft head is the normal state of an edit in progress, not a
    # contradiction. Reporting it as one put `state_inconsistent` on every
    # successful update/patch response.
    reasons: list[str] = []
    if version.page_id != page.id:
        reasons.append("version_on_another_page")
    if version.version_status not in {
        "editing",
        "draft",
        "published_snapshot",
        "superseded_snapshot",
    }:
        reasons.append("unknown_version_status")
    if version.graph_status not in _GRAPH_STATUSES:
        reasons.append("unknown_graph_status")
    if (
        version.version_status in _PUBLISHED_STATUSES
        and version.graph_status != "linear"
    ):
        reasons.append("published_row_not_linear")
    if is_editing and version.graph_status != "linear":
        reasons.append("editing_row_not_linear")
    if is_published_head and version.version_status != "published_snapshot":
        reasons.append("published_head_not_a_snapshot")
    if (
        expose_draft_state
        and actual_draft_head
        and (
            version.version_status not in {"draft", "editing"}
            or version.graph_status != "linear"
        )
    ):
        reasons.append("draft_head_not_a_linear_draft")
    if expose_draft_state and is_published_head and actual_draft_head:
        reasons.append("published_head_is_also_draft_head")
    inconsistent = bool(reasons)

    # Exact precedence: state inconsistent; current editing; current draft;
    # published snapshot; alternative; discarded; superseded snapshot;
    # superseded draft.
    if inconsistent:
        history_status = "state_inconsistent"
    elif is_editing:
        history_status = "current_editing"
    elif is_draft_head:
        history_status = "current_draft"
    elif is_published_head:
        history_status = "published_snapshot"
    elif is_alternative:
        history_status = "alternative_draft"
    elif is_discarded:
        history_status = "discarded_draft"
    elif version.version_status in _PUBLISHED_STATUSES:
        history_status = "superseded_snapshot"
    else:
        history_status = "superseded_draft"

    return ContextVersionProjection(
        history_status=history_status,
        graph_status=version.graph_status,
        is_visible_head=version.id == visible_version_id,
        is_published_head=is_published_head,
        is_draft_head=is_draft_head,
        is_alternative=is_alternative,
        is_discarded=is_discarded,
        inconsistency_reasons=tuple(reasons),
    )


def live_draft_chain(
    session: Session, page: ContextPage
) -> list[ContextPageVersion]:
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


def is_publishable_draft(
    session: Session, page: ContextPage, version: ContextPageVersion
) -> bool:
    """Whether approving ``version`` would publish exactly this draft.

    The latest draft always can. An earlier linear draft can when it lies on
    the chain between the latest draft and the published head, but not
    before a first publication: context writes then apply page metadata
    directly instead of staging it per draft, so it follows the newest one.
    """

    if (
        version.page_id != page.id
        or version.version_status != "draft"
        or version.graph_status != "linear"
    ):
        return False
    if page.latest_draft_version_id == version.id:
        return True
    return page.current_published_version_id is not None and any(
        member.id == version.id for member in live_draft_chain(session, page)
    )


def assert_context_publish_candidate(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
    require_latest_draft: bool,
    allow_chain_ancestor: bool = False,
) -> None:
    """Reject a stale, branched, discarded, or non-linear publication.

    A current draft may have one or more earlier linear draft ancestors (MCP
    revisions create that shape).  The chain must end at the current published
    head, or at ``None`` for a never-published page.  A historical snapshot,
    cross-page parent, cycle, or alternative anywhere in the chain is stale.
    ``allow_chain_ancestor`` (review approval only) also accepts one of those
    earlier linear drafts once the page has been published: it publishes the
    changes up to that draft and the newer drafts stay on top of it.
    """

    if version.page_id != page.id or version.graph_status != "linear":
        raise StaleContextDraftError()
    if require_latest_draft:
        if version.version_status != "draft" or (
            page.latest_draft_version_id != version.id
            and not (
                allow_chain_ancestor and is_publishable_draft(session, page, version)
            )
        ):
            raise StaleContextDraftError()
    elif version.version_status not in {"draft", "published_snapshot"}:
        raise StaleContextDraftError()

    # Re-applying the already published head is a topology no-op.  Higher-level
    # callers still require their publish operation and audit evidence before
    # treating such a replay as successful.
    if version.id == page.current_published_version_id:
        return

    if not require_latest_draft:
        expected_parent_ids = {
            value
            for value in (page.latest_draft_version_id, page.current_version_id)
            if value is not None
        }
        if expected_parent_ids:
            if version.parent_version_id not in expected_parent_ids:
                raise StaleContextDraftError()
        elif version.parent_version_id is not None:
            raise StaleContextDraftError()

    published_head_id = page.current_published_version_id
    parent_id = version.parent_version_id
    seen = {version.id}
    while parent_id != published_head_id:
        if parent_id is None or parent_id in seen:
            raise StaleContextDraftError()
        seen.add(parent_id)
        parent = session.get(ContextPageVersion, parent_id)
        if (
            parent is None
            or parent.page_id != page.id
            # P9.1: `update_page` (`require_latest_draft=False`) targets the
            # page's own shared head via `page.current_version_id`, which a
            # never-submitted page-first page legitimately still names its
            # original "editing" row (see `_set_current_version`'s own
            # "editing"/"draft" note) -- that row is exactly as live and
            # non-stale a predecessor as a submitted "draft" one, just not
            # yet locked for review. `submit_context_edit_for_review` never
            # inserts a new row on top of one (it flips the same row's status
            # in place), so an "editing" ancestor can only appear as the
            # immediate parent here, never partway through a longer chain.
            or parent.version_status not in ("draft", "editing")
            or parent.graph_status != "linear"
        ):
            raise StaleContextDraftError()
        parent_id = parent.parent_version_id

    if published_head_id is not None:
        published = session.get(ContextPageVersion, published_head_id)
        if (
            published is None
            or published.page_id != page.id
            or published.version_status not in _PUBLISHED_STATUSES
            or published.graph_status != "linear"
        ):
            raise StaleContextDraftError()


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
        if page.current_version_id is None and page_versions:
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


def repair_context_history(session: Session) -> ContextHistoryRepairResult:
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
    page: ContextPage,
    version: ContextPageVersion,
) -> int:
    """Demote old snapshots before promoting one proven canonical head."""

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
    # P8: `last_saved_at` tracks a real author save, not an administrative
    # pointer/status repair. Reassigning it to its own pre-repair value in
    # the SAME flush as the status change is a no-op to SQLAlchemy's dirty
    # tracking (identical value -> not considered "explicitly set" for this
    # flush), so `onupdate` fires anyway. Instead: let the status-changing
    # flush(es) happen normally below, then correct `last_saved_at` back to
    # its preserved value as a genuinely different follow-up write, which
    # SQLAlchemy does include (and which therefore suppresses `onupdate`).
    preserved_last_saved_at: dict[UUID, datetime] = {}
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
            preserved_last_saved_at[previous.id] = previous.last_saved_at
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
        preserved_last_saved_at[version.id] = version.last_saved_at
    version.version_status = "published_snapshot"
    version.graph_status = "linear"
    session.add(version)

    if preserved_last_saved_at:
        # Force the status/graph_status UPDATE(s) above to actually reach
        # the DB (so `onupdate` runs and stamps `last_saved_at`), then
        # correct it back to the pre-repair value as its own distinct
        # write -- see the comment on `preserved_last_saved_at` above for
        # why this can't be folded into the same flush as the status
        # change.
        session.flush()
        for version_id, original_last_saved_at in preserved_last_saved_at.items():
            touched = (
                version
                if version_id == version.id
                else session.get(ContextPageVersion, version_id)
            )
            if touched is not None:
                touched.last_saved_at = original_last_saved_at
        session.flush()

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


def apply_context_publication(
    session: Session,
    *,
    page: ContextPage,
    version: ContextPageVersion,
    require_latest_draft: bool,
    allow_chain_ancestor: bool = False,
) -> UUID | None:
    """Atomically normalize snapshots and advance every published pointer.

    The caller owns the surrounding transaction/savepoint.  Selecting prior
    rows for update gives PostgreSQL the same serialization boundary as the
    already locked page row; SQLite keeps the operation in its single write
    transaction.  Every former ``published_snapshot`` is normalized, not only
    the row named by the pointer, so legacy multi-published pages self-heal on
    their next successful publication.
    """

    assert_context_publish_candidate(
        session,
        page=page,
        version=version,
        require_latest_draft=require_latest_draft,
        allow_chain_ancestor=allow_chain_ancestor,
    )
    require_no_visible_legacy_media(
        workspace_id=page.workspace_id,
        body_markdown=version.body_md,
    )
    # Publishing an earlier draft of the chain leaves the drafts above it
    # pending; they stay the edit head and build on this version.
    newer_draft_ids: set[UUID] = set()
    if allow_chain_ancestor and page.latest_draft_version_id not in (
        None,
        version.id,
    ):
        chain_ids = [draft.id for draft in live_draft_chain(session, page)]
        newer_draft_ids = set(chain_ids[: chain_ids.index(version.id)])
    previous_published_version_id = page.current_published_version_id
    previous = (
        get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=page.workspace_id,
            resource_id=previous_published_version_id,
        )
        if previous_published_version_id is not None
        else None
    )
    if previous is None or (
        normalize_content_recency(parse_page_frontmatter(previous.body_md).body)
        != normalize_content_recency(parse_page_frontmatter(version.body_md).body)
        or previous.rendering_profile != version.rendering_profile
    ):
        page.content_changed_at = utc_now()
    _normalize_context_published_rows(session, page=page, version=version)
    # A multi-step draft chain (ordinary since staged autosave shipped -- see
    # commit 60593953 -- not an edge case) leaves every ancestor between the
    # published tip and the previous published head `draft`/`linear`. This
    # publish is about to clear `latest_draft_version_id`, so those ancestors
    # would otherwise become audit-visible orphans that repair could
    # resurrect as the edit head. `assert_context_publish_candidate` just
    # walked this exact same path (its own loop above) and proved it reaches
    # `previous_published_version_id` through nothing but `draft`/`linear`
    # rows, so retracing it here and demoting everything found is provably
    # safe: a page has at most one editable chain rooted at its published
    # head, so this walk can never reach an unrelated draft still reachable
    # some other way (e.g. a preserved alternative, which is `branched`, not
    # `linear`, precisely so this walk leaves it alone).
    #
    # Replaying the already-published head (``version.id ==
    # previous_published_version_id``) is the one case where that proof does
    # NOT hold: `assert_context_publish_candidate` returns early for that
    # topology no-op (its own docstring calls it out) and never walks the
    # chain at all, so nothing here guarantees `version.parent_version_id`
    # even belongs to an active draft chain. Skip the walk entirely then --
    # there is no new chain being published, so there is nothing to demote.
    if version.id != previous_published_version_id:
        parent_id = version.parent_version_id
        seen = {version.id}
        while parent_id != previous_published_version_id:
            if parent_id is None or parent_id in seen:
                break
            seen.add(parent_id)
            ancestor = session.get(ContextPageVersion, parent_id)
            if (
                ancestor is None
                or ancestor.version_status not in ("draft", "editing")
                or ancestor.graph_status != "linear"
            ):
                break
            if ancestor.version_status == "editing":
                # P9.1: a page-first page's initial "editing" row can be the
                # immediate ancestor of its first direct publish (see the
                # matching relaxation in `assert_context_publish_candidate`).
                # It is not part of the shared draft chain the way a
                # submitted "draft" ancestor is, so demote its
                # `version_status` too, exactly like an explicit
                # `discard_context_edit` would -- otherwise it keeps
                # matching `_editable_version_id_for_actor`'s "this actor's
                # own open editing row" lookup forever, and every later
                # autosave by that actor resolves back onto this superseded
                # row instead of the actual published head.
                ancestor.version_status = "discarded"
            ancestor.graph_status = "discarded"
            session.add(ancestor)
            parent_id = ancestor.parent_version_id
    page.current_published_version_id = version.id
    if page.current_version_id not in newer_draft_ids:
        page.current_version_id = version.id
    if not newer_draft_ids:
        page.latest_draft_version_id = None
    page.lifecycle_status = "published"
    reconcile_public_exposure_after_publication(
        page,
        previous_published_version_id=previous_published_version_id,
        published_version_id=version.id,
    )
    session.add_all([page, version])
    return previous_published_version_id
