"""P10.3 T4.5 -- control-plane -> workspace-plane credential-grant fan-out.

.claude/plan/260909-p10-3-multi-workspace-credential.md section 2.6 is the
authoritative protocol this module implements:

    쓰기(생성/확장/축소/scope 변경) -- control 트랜잭션에서 index 행 upsert:
    desired_scopes, revision += 1, sync_state = pending (축소는 removed_at =
    now). 커밋.
    fan-out 트리거 -- control 세션 after_commit 리스너에서
    record_workspace_job_hint 패턴으로 workspace별 fan-out을 예약. 요청
    스레드에서 즉시 1회 시도하고 실패는 job으로 남긴다.
    workspace 적용 -- workspace별 별도 트랜잭션: workspace_credential_grant
    upsert(synced_revision < revision일 때만 적용, 멱등), 축소면
    revoked_at = now. 성공 시 control 행 sync_state = synced(축소면 행 하드
    삭제), 실패 시 failed + last_sync_error + 감사
    credential_replica.sync_failed{credential_kind, credential_id,
    workspace_id, revision}.

One shared apply function serves BOTH control-index shapes this module
knows about:

* PAT -- ``backend.models.api_token.ApiTokenWorkspace``, PK
  ``(token_id, workspace_id)``. ``credential_id`` IS the PK's ``token_id``,
  so the control row is always addressable from
  ``(credential_kind, credential_id, workspace_id)`` alone.
* OAuth -- ``backend.models.oauth.OAuthConsent``, PK
  ``(user_id, client_id, workspace_id)``. There is no ``grant_id`` column on
  ``OAuthConsent`` (T1 deliberately did not add one -- consent rows exist
  per (user, client, workspace), independent of any one refresh-token
  family). The workspace-plane replica's ``credential_id`` is the (user,
  client) family's stable ``OAuthRefreshToken.grant_id`` (see
  ``resolve_oauth_replica_credential_id`` below), which is NOT a component
  of ``OAuthConsent``'s own primary key -- so the control row is instead
  addressed by ``(user_id, client_id, workspace_id)``, carried on
  ``CredentialReplicaFanOutRequest.client_id`` for the oauth case only.

That is the one real asymmetry between the two credential kinds this module
has to know about (a second locator tuple), and it is confined entirely to
the internal ``_control_row_for`` helper -- ``_write_replica`` (the
workspace-plane half) never needs it: ``workspace_credential_grant`` is
addressed by ``(workspace_id, credential_kind, credential_id)`` for both
kinds identically, which is exactly what its own unique constraint says.

T4.5 boundary (historical, now superseded by the T4.6 half below): the
fan-out functions in the first half of this module only READ a control-index
row's already-committed ``desired_scopes``/``revision``/``removed_at``
(extracted by the caller into a ``CredentialReplicaFanOutRequest`` before
that row's own transaction commits) and perform ONE best-effort apply
attempt, immediately, on the request thread. They do NOT retry a failed
attempt, do NOT diff a workspace's replica set against its control index for
orphans, and do NOT run on any schedule.

T4.6 adds exactly that missing half, in the second section of this file:
``reconcile_workspace_credential_replicas`` -- the body of the worker job
type ``credential_replica_reconcile`` (seeded per workspace, per minute, by
``backend.worker.tick.seed_jobs_for_workspace``; dispatched by
``backend.worker.runtime.OneShotWorker._business_credential_replica_
reconcile``). It re-reads the control index FRESH and calls
``apply_credential_replica_fan_out`` again for every row that needs it --
exactly as T4.5 anticipated -- and adds the two things a single forward
apply attempt structurally cannot do: tombstoning a replica whose control
index row is gone (a restored backup or re-attached shard), and repairing a
control row whose replica has vanished underneath it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import and_, event, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.db_errors import is_database_schema_mismatch
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.base import utc_now
from backend.models.oauth import OAuthConsent, OAuthRefreshToken
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.rbac import WorkspacePlaneUnavailableError
from backend.services.audit_service import AuditService
from backend.services.job_hints import record_workspace_job_hint

logger = logging.getLogger(__name__)

CredentialKind = Literal["pat", "oauth"]

# audit_event.payload/last_sync_error are free text -- cap length so a
# pathological driver error message cannot bloat either row indefinitely.
# No secret ever flows through this path (scopes, ids, exception class
# names only), so truncation is the only sanitization needed.
_DIAGNOSTIC_MAX_LEN = 500


class CredentialReplicaDatabaseProvider(Protocol):
    """The subset of ``backend.app.container.DatabaseProvider`` this module
    needs. ``session.info["database_provider"]`` (stamped on every session a
    real ``DatabaseProvider.session_factory`` creates -- see that class)
    already satisfies this for every production caller; tests can pass any
    object exposing the same two methods, matching the fake shapes already
    established by ``backend.tests.unit.test_mcp_router.
    _FailingWorkspacePlaneProvider`` and ``backend.tests.integration.
    test_workspace_rbac._FailingProvider``."""

    def control_session_factory(self): ...

    def session_for(self, workspace_id: UUID): ...


@dataclass(frozen=True)
class CredentialReplicaFanOutRequest:
    """One control-index row's worth of fan-out work (section 2.6).

    Built by a PAT/OAuth service call site right after it upserts or
    soft-removes an ``ApiTokenWorkspace``/``OAuthConsent`` row -- the values
    below are a snapshot of that row at registration time, not a live
    reference to it (the ORM object itself is only valid inside the control
    session that is about to commit and close).

    ``client_id`` is populated for ``credential_kind == "oauth"`` only: it
    is what lets ``_control_row_for`` find the matching ``OAuthConsent`` row
    back (PK ``(user_id, client_id, workspace_id)``), since ``credential_id``
    for OAuth is a ``grant_id`` that is not part of that PK -- see the
    module docstring.

    ``credential_id`` is ``UUID | None`` rather than always-``UUID``
    specifically for the OAuth case: a (user, client) family that has never
    exchanged an authorization code for a token yet has no
    ``OAuthRefreshToken`` row, hence no ``grant_id`` -- see
    ``resolve_oauth_replica_credential_id``. A request built with
    ``credential_id=None`` is a well-formed "not ready to fan out yet"
    request, not a caller bug: ``apply_credential_replica_fan_out`` treats
    it as an ordinary apply failure (recorded, then left for the
    reconciler), never a crash.
    """

    credential_kind: CredentialKind
    credential_id: UUID | None
    workspace_id: UUID
    user_id: UUID
    desired_scopes: tuple[str, ...]
    revision: int
    removed_at: datetime | None
    client_id: str | None = None


def resolve_oauth_replica_credential_id(
    session: Session, *, user_id: UUID, client_id: str
) -> UUID | None:
    """The workspace-plane ``credential_id`` for an OAuth (user, client)
    family: its refresh-token family key, ``grant_id`` (falling back to a
    legacy row's own ``id``, the same ``COALESCE(grant_id, id)`` convention
    ``_grant_family_predicate`` in ``backend.services.oauth_service`` uses).

    Deliberately reads ANY row for the family, live or not (revoked/expired
    rows still carry the correct, stable ``grant_id`` -- there is no reason
    to prefer a live one here, and doing so would make this return ``None``
    for a family whose only token has since expired, which is wrong: the
    family's *identity* does not expire, only its tokens do). Ordered by
    ``created_at`` descending only so a family with pre-``grant_id`` legacy
    rows prefers whichever row a real family-key computation would today
    (the newest); every row minted after grant_id existed already agrees
    within one family.

    Returns ``None`` when this (user, client) pair has never exchanged an
    authorization code for a token -- e.g. consent was just granted or
    edited via Settings, but the interactive OAuth flow never completed a
    token exchange. There is deliberately no fallback identity minted here
    (a value derived from ``(user_id, client_id)`` would be stable but WRONG
    -- it would not match the real ``grant_id`` that a future token
    exchange mints via ``uuid4()``, so a replica written under it could
    never be found by ``verify_access_token``'s real lookup). Callers must
    treat ``None`` as "not ready" (see ``CredentialReplicaFanOutRequest``'s
    docstring), not synthesize a value.
    """

    row = session.scalar(
        select(OAuthRefreshToken)
        .where(
            OAuthRefreshToken.user_id == user_id,
            OAuthRefreshToken.client_id == client_id,
        )
        .order_by(OAuthRefreshToken.created_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    return row.grant_id or row.id


def register_fan_out(session: Session, request: CredentialReplicaFanOutRequest) -> None:
    """Queue ``request`` for a best-effort apply right after ``session``
    commits (section 2.6's fan-out trigger row).

    Mirrors ``JobQueueService._register_job_hint``'s nested-transaction-safe
    ``after_commit`` registration (``backend/services/job_queue.py``)
    exactly, generalized from a bare workspace-id hint to a full fan-out
    request: a rolled-back session must fire no apply attempt at all (a
    request queued from a nested SAVEPOINT that itself rolls back must not
    survive an outer commit), and the real apply must run strictly after
    the control row's own commit is durable, never inside it or able to
    affect its outcome.

    No-ops when ``session`` was not created by a ``DatabaseProvider`` (most
    unit tests use a bare ``sessionmaker`` fixture with no such stamp) --
    same latency-only fallback ``record_workspace_job_hint`` documents.
    """

    database_provider = session.info.get("database_provider")
    if database_provider is None:
        return
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:  # pragma: no cover - callers always hold one open
        raise RuntimeError(
            "credential replica fan-out registration requires an active "
            "transaction"
        )
    pending_by_transaction: dict[object, list[CredentialReplicaFanOutRequest]] = (
        session.info.setdefault(
            "_pending_credential_replica_fanout_by_transaction", {}
        )
    )
    pending_by_transaction.setdefault(transaction, []).append(request)

    if session.info.get("_credential_replica_fanout_listeners_registered"):
        return
    session.info["_credential_replica_fanout_listeners_registered"] = True

    def _on_commit(_session: Session) -> None:
        pending_scopes: dict[object, list[CredentialReplicaFanOutRequest]] = (
            _session.info.setdefault(
                "_pending_credential_replica_fanout_by_transaction", {}
            )
        )
        nested = _session.get_nested_transaction()
        if nested is not None:
            nested_requests = pending_scopes.pop(nested, [])
            parent = nested.parent
            if parent is not None and nested_requests:
                pending_scopes.setdefault(parent, []).extend(nested_requests)
            return

        requests = [
            pending_request
            for scoped in pending_scopes.values()
            for pending_request in scoped
        ]
        pending_scopes.clear()
        provider = _session.info.get("database_provider")
        if provider is None:
            return
        # apply_credential_replica_fan_out and record_workspace_job_hint are
        # both individually guaranteed never to raise -- no extra try/except
        # needed here, matching _register_job_hint's own _on_commit.
        for pending_request in requests:
            apply_credential_replica_fan_out(provider, pending_request)
            record_workspace_job_hint(provider, pending_request.workspace_id)

    def _on_rollback(_session: Session) -> None:
        pending_scopes: dict[object, list[CredentialReplicaFanOutRequest]] = (
            _session.info.setdefault(
                "_pending_credential_replica_fanout_by_transaction", {}
            )
        )
        nested = _session.get_nested_transaction()
        if nested is not None:
            pending_scopes.pop(nested, None)
        else:
            pending_scopes.clear()

    event.listen(session, "after_commit", _on_commit)
    event.listen(session, "after_rollback", _on_rollback)


def apply_credential_replica_fan_out(
    database_provider: CredentialReplicaDatabaseProvider,
    request: CredentialReplicaFanOutRequest,
) -> None:
    """Apply ONE control-index row's fan-out to its workspace-plane replica.

    MUST NEVER RAISE. Unlike ``backend.policy.rbac.require_workspace_role``
    (T0), which deliberately lets a workspace-plane schema mismatch escape
    to the app's top-level ``unhandled_exception_handler`` because it is
    running synchronously on the request's own authorization path, this
    function runs from a control session's ``after_commit`` listener
    (``register_fan_out``) -- still on the request thread, still INSIDE the
    ``with self.session_factory.begin() as session:`` block of whatever
    control-plane write (``create_token``, a consent PATCH, ...) just
    committed. Anything this function raised would propagate out of that
    `with` block and fail the ALREADY-SUCCEEDED control write's HTTP
    response, which is exactly the outcome section 2.6 and this module's
    caller-facing contract both rule out ("a failure in your fan-out
    attempt must be recorded ... and then LEFT ALONE"). So every exception,
    including a schema mismatch, is caught here -- the callers below still
    distinguish it for a more useful ``last_sync_error``, but nothing is
    ever allowed to reach this function's own caller.
    """

    try:
        _apply_credential_replica_fan_out(database_provider, request)
    except Exception:
        logger.exception(
            "credential replica fan-out crashed applying kind=%s "
            "credential_id=%s workspace_id=%s revision=%s -- best-effort "
            "only, the control row may be left at sync_state=pending "
            "(T4.6's reconciler will retry once it exists)",
            request.credential_kind,
            request.credential_id,
            request.workspace_id,
            request.revision,
        )


def _apply_credential_replica_fan_out(
    database_provider: CredentialReplicaDatabaseProvider,
    request: CredentialReplicaFanOutRequest,
) -> None:
    if request.credential_id is None:
        _mark_failed(
            database_provider,
            request,
            "no stable credential identity yet -- this OAuth (user, "
            "client) family has not exchanged an authorization code for a "
            "token, so it has no grant_id to fan out under",
        )
        return
    try:
        _apply_to_workspace_plane(database_provider, request)
    except Exception as exc:
        _mark_failed(database_provider, request, _diagnostic(exc))
        return
    _mark_synced(database_provider, request)


def _apply_to_workspace_plane(
    database_provider: CredentialReplicaDatabaseProvider,
    request: CredentialReplicaFanOutRequest,
) -> None:
    """Open ``request.workspace_id``'s OWN session and idempotently apply
    the fan-out, in one workspace-plane transaction (section 2.6's
    "workspace 적용" row).

    A workspace-plane session-open/query failure is reclassified as
    ``WorkspacePlaneUnavailableError`` the same way T0 already does for the
    request-time authorization path (``backend.policy.rbac.
    require_workspace_role``) -- schema mismatch stays its own diagnostic
    (``is_database_schema_mismatch``), not folded into "unavailable, retry
    later". Both are still caught by this function's only caller,
    ``_apply_credential_replica_fan_out`` -- see
    ``apply_credential_replica_fan_out``'s docstring for why NEITHER may
    ever propagate past that point.
    """

    try:
        workspace_session_factory = database_provider.session_for(
            request.workspace_id
        )
        with workspace_session_factory.begin() as workspace_session:
            _write_replica(workspace_session, request)
    except SQLAlchemyError as exc:
        if is_database_schema_mismatch(exc):
            raise
        raise WorkspacePlaneUnavailableError(
            workspace_id=request.workspace_id
        ) from exc


def _write_replica(session: Session, request: CredentialReplicaFanOutRequest) -> None:
    """Idempotent upsert/tombstone of ONE ``WorkspaceCredentialGrant`` row,
    guarded by ``synced_revision`` so a retried or duplicated apply attempt
    (this immediate best-effort call racing a later reconciler retry, or
    two immediate attempts from two requests that both touched the same
    control row) converges instead of double-applying or regressing a
    newer state that already landed."""

    existing = session.scalar(
        select(WorkspaceCredentialGrant).where(
            WorkspaceCredentialGrant.workspace_id == request.workspace_id,
            WorkspaceCredentialGrant.credential_kind == request.credential_kind,
            WorkspaceCredentialGrant.credential_id == request.credential_id,
        )
    )
    now = utc_now()

    if request.removed_at is not None:
        # Shrink: tombstone the replica. A retry after a prior apply that
        # tombstoned the replica but failed to mark the control row synced
        # (a control-plane hiccup between the two) must not raise or
        # overwrite synced_at with stale data every time it is retried.
        if existing is not None and existing.revoked_at is None:
            existing.revoked_at = now
            existing.synced_revision = request.revision
            existing.synced_at = now
        return

    if existing is None:
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=request.workspace_id,
                credential_kind=request.credential_kind,
                credential_id=request.credential_id,
                user_id=request.user_id,
                granted_scopes=list(request.desired_scopes),
                synced_revision=request.revision,
                synced_at=now,
            )
        )
        return

    if existing.synced_revision >= request.revision and existing.revoked_at is None:
        # Already applied (idempotent replay), or a NEWER revision already
        # landed via a race with another apply attempt -- either way there
        # is nothing this (older or equal) request should do.
        return

    # Falling through with `synced_revision >= revision` is reachable in
    # exactly one shape, and it is a real one rather than a defensive
    # "shouldn't happen": a workspace REMOVED from the allowed set and then
    # added back. The shrink tombstones this replica at revision N and hard
    # deletes the control row (_mark_synced); the re-add then creates a
    # BRAND-NEW control row whose revision restarts at 1 (that function's own
    # docstring says so). Without the `revoked_at is None` guard above, the
    # re-add's apply would see 1 <= N, return early, and leave the replica
    # tombstoned forever while the control row reads "synced" -- i.e. the
    # user re-grants a workspace in Settings and every request to it is
    # denied with grant_replica_missing, permanently, with nothing in the
    # system reporting a problem. Lowering synced_revision back to
    # request.revision here is correct for the same reason: the counter it
    # has to agree with is the CURRENT control row's, and that row's
    # numbering genuinely did restart. (Found while building T4.6's drift
    # repair, which needs this same revival path for a replica tombstoned by
    # an orphan sweep that a later control write resurrects.)
    existing.granted_scopes = list(request.desired_scopes)
    existing.synced_revision = request.revision
    existing.synced_at = now
    # A workspace re-added to the allowed set after an earlier removal
    # reuses this same row (the unique constraint forbids a second one for
    # the same (workspace, kind, credential) triple) -- un-tombstone it.
    existing.revoked_at = None


def _control_row_for(
    session: Session, request: CredentialReplicaFanOutRequest
) -> ApiTokenWorkspace | OAuthConsent | None:
    if request.credential_kind == "pat":
        return session.get(
            ApiTokenWorkspace, (request.credential_id, request.workspace_id)
        )
    if request.client_id is None:  # pragma: no cover - defensive, see docstring
        return None
    return session.get(
        OAuthConsent, (request.user_id, request.client_id, request.workspace_id)
    )


def _mark_synced(
    database_provider: CredentialReplicaDatabaseProvider,
    request: CredentialReplicaFanOutRequest,
) -> None:
    """Section 2.6: "성공 시 control 행 sync_state = synced(축소면 행 하드
    삭제)". A shrink's control row is deleted outright rather than flipped
    to ``synced`` -- once its replica is tombstoned there is nothing left
    for the row to index (an expansion re-adding the same workspace later
    creates a fresh row, ``revision`` restarting at 1, exactly like a
    brand-new grant).

    Guarded by ``row.revision == request.revision``: if a newer write
    landed on the control row between this apply attempt starting and
    finishing, that newer write already reset ``sync_state`` to
    ``"pending"`` (or ``removed_at`` again) -- this stale attempt must not
    stomp that back to ``"synced"``/delete a row a newer intent still owns.
    """

    with database_provider.control_session_factory().begin() as session:
        row = _control_row_for(session, request)
        if row is None or row.revision != request.revision:
            return
        if request.removed_at is not None:
            session.delete(row)
            return
        row.sync_state = "synced"
        row.last_sync_error = None


def _mark_failed(
    database_provider: CredentialReplicaDatabaseProvider,
    request: CredentialReplicaFanOutRequest,
    diagnostic: str,
) -> None:
    """Section 2.6's failure branch: control row -> ``sync_state="failed"``
    + ``last_sync_error``, plus the audit event
    ``credential_replica.sync_failed``. Always emits the audit event, even
    when the control row itself can no longer be found (already
    hard-deleted by a concurrent successful shrink apply) or has moved to a
    newer revision -- the failed ATTEMPT happened and is worth a record
    regardless of whether there is still a row to annotate. This function
    itself may raise (a control-plane failure while trying to record a
    workspace-plane failure) -- its only caller, ``_apply_credential_
    replica_fan_out``, does not catch that, so it is
    ``apply_credential_replica_fan_out``'s outer guard that has the final
    word (see that function's docstring)."""

    diagnostic = diagnostic[:_DIAGNOSTIC_MAX_LEN]
    with database_provider.control_session_factory().begin() as session:
        row = _control_row_for(session, request)
        if row is not None and row.revision == request.revision:
            row.sync_state = "failed"
            row.last_sync_error = diagnostic
        target_kind = "api_token" if request.credential_kind == "pat" else (
            "oauth_consent"
        )
        target_id = request.credential_id or request.workspace_id
        AuditService().emit(
            session=session,
            workspace_id=request.workspace_id,
            actor_kind="system",
            actor_id="credential_replica_fanout",
            action="credential_replica.sync_failed",
            target_kind=target_kind,
            target_id=target_id,
            payload={
                "credential_kind": request.credential_kind,
                "credential_id": (
                    str(request.credential_id) if request.credential_id else None
                ),
                "workspace_id": str(request.workspace_id),
                "revision": request.revision,
                "reason": diagnostic,
            },
            event_summary="Credential replica fan-out failed",
            retention_class="sensitive",
        )


def _diagnostic(exc: Exception) -> str:
    """A short, secret-free description of an apply failure. No secret
    (bearer token, refresh token, hashed credential) ever reaches this
    path -- the request only ever carries ids, scope strings, and a
    revision number -- so a bare exception repr is safe to store and
    audit."""

    if isinstance(exc, WorkspacePlaneUnavailableError):
        return "workspace_plane_unavailable"
    return f"{type(exc).__name__}: {exc}"[:_DIAGNOSTIC_MAX_LEN]


# ---------------------------------------------------------------------------
# T4.6 -- reconciler (section 2.6's "reconciler" row)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialReplicaReconcileResult:
    """One workspace's reconcile pass, returned as the worker job's detail.

    ``reapplied_count``/``orphan_tombstoned_count`` are the two numbers that
    are 0 on a healthy workspace -- a nonzero value means this tick actually
    repaired drift, which is what makes the job's detail worth reading at
    all.
    """

    index_row_count: int
    replica_count: int
    reapplied_count: int
    not_ready_count: int
    orphan_tombstoned_count: int


@dataclass(frozen=True)
class _ControlIndexSnapshot:
    """One control-index row read fresh at the start of a reconcile pass.

    ``sync_state`` is carried alongside the request because
    ``CredentialReplicaFanOutRequest`` deliberately does not have it: the
    forward fan-out path always applies unconditionally (it was just written
    by the request that triggered it), while the reconciler is precisely the
    caller that has to DECIDE whether an apply is needed at all.
    """

    request: CredentialReplicaFanOutRequest
    sync_state: str


@dataclass(frozen=True)
class _ReplicaSnapshot:
    """One ``workspace_credential_grant`` row, detached from its session.

    Read (and closed) before any control-plane session is opened, so the two
    planes are never held open at the same time -- the same discipline
    ``_apply_to_workspace_plane``/``_mark_synced`` already follow, and what
    ``tests/static/test_workspace_plane_boundary.py``'s allowlist entry for
    this file asserts about it.
    """

    id: UUID
    credential_kind: str
    credential_id: UUID
    synced_revision: int
    revoked_at: datetime | None


def reconcile_workspace_credential_replicas(
    database_provider: CredentialReplicaDatabaseProvider,
    *,
    workspace_id: UUID,
) -> CredentialReplicaReconcileResult:
    """Reconcile ONE workspace's credential-grant replicas against the
    control index (section 2.6's "reconciler" row, plan regression R12).

    Per workspace, not per roster. Section 2.6's prose mentions walking
    ``backend.worker.runtime.OneShotWorker._workspace_roster``, but the job
    it also specifies -- minute-bucketed, "``bundle_expiration_sweep``과 같은
    자리" -- is seeded once PER WORKSPACE by
    ``backend.worker.tick.seed_jobs_for_workspace``, and the roster walk that
    reaches every workspace already happens one level up, in
    ``backend.worker.entry._seed_due_jobs`` (Epic-2 Phase 6a). So the roster
    IS walked, exactly once per tick, by the seeder; each job instance then
    owns exactly one workspace_id. Doing it the other way -- one global job
    that walks the roster itself -- would have to file its own ``Job`` row
    under some arbitrary workspace (``Job`` is workspace-plane data), and one
    unreachable shard would fail the reconcile for every OTHER workspace too,
    which is the opposite of what a repair job should do.

    Order of operations is load-bearing:

    1. Read the workspace plane FIRST. If that read fails, this raises
       immediately -- before any per-row apply attempt. That matters: with
       the workspace DB down, every apply would fail identically and write
       one ``credential_replica.sync_failed`` audit row per credential per
       minute, forever. Raising here instead surfaces the outage ONCE, as a
       failed job (see the job_type's ``NOISY_UNTIL_FAILED_JOB_TYPES``
       registration in ``backend.services.job_queue``), and leaves the
       control rows' existing diagnostics alone.
    2. Read the control index (and decide the orphan verdicts, which need
       control lookups) in ONE control session, then close it.
    3. Re-apply, per row that needs it, via ``apply_credential_replica_fan_
       out`` -- unchanged from T4.5, opening its own sessions per attempt.
    4. Tombstone the orphans found in (2), in one workspace transaction.

    Steps 3 and 4 cannot collide: (4) only ever touches replicas that had NO
    control row at read time, and (3) only ever touches replicas that had
    one.

    Idempotency (R12's fourth clause) comes from ``_write_replica``'s
    ``synced_revision`` guard plus the "does this even need an apply?" test
    in ``_needs_apply``: a second identical pass over a healthy workspace
    issues no writes at all, and re-applying the same ``revision`` twice
    converges on the same row.
    """

    replicas = _read_workspace_replicas(database_provider, workspace_id)
    replica_by_target: dict[tuple[str, UUID], _ReplicaSnapshot] = {
        (replica.credential_kind, replica.credential_id): replica
        for replica in replicas
    }

    with database_provider.control_session_factory().begin() as control_session:
        snapshots = _control_index_snapshots(control_session, workspace_id)
        orphan_ids = _orphan_replica_ids(control_session, workspace_id, replicas)

    reapplied_count = 0
    not_ready_count = 0
    for snapshot in snapshots:
        if snapshot.request.credential_id is None:
            # An OAuth (user, client) family that has never exchanged a
            # token has no grant_id to fan out under -- a legitimate "not
            # ready", not a failure to retry. Record it exactly once (the
            # apply marks the row failed with that diagnostic) and then stop
            # touching it, or every tick would re-emit the same audit event
            # for a consent that is simply waiting on its first token
            # exchange. See resolve_oauth_replica_credential_id.
            not_ready_count += 1
            if snapshot.sync_state != "failed":
                apply_credential_replica_fan_out(database_provider, snapshot.request)
            continue
        replica = replica_by_target.get(
            (snapshot.request.credential_kind, snapshot.request.credential_id)
        )
        if not _needs_apply(snapshot, replica):
            continue
        apply_credential_replica_fan_out(database_provider, snapshot.request)
        reapplied_count += 1

    orphan_tombstoned_count = _tombstone_orphan_replicas(
        database_provider, workspace_id, orphan_ids
    )

    return CredentialReplicaReconcileResult(
        index_row_count=len(snapshots),
        replica_count=len(replicas),
        reapplied_count=reapplied_count,
        not_ready_count=not_ready_count,
        orphan_tombstoned_count=orphan_tombstoned_count,
    )


def _needs_apply(
    snapshot: _ControlIndexSnapshot, replica: _ReplicaSnapshot | None
) -> bool:
    """Whether this control row still has unapplied intent.

    Section 2.6's reconciler row states two things that only look like one:
    "control에서 sync_state != synced 행을 재시도" AND "복제본 없는 index 행은
    재생성". Read literally the first sentence would confine the whole pass
    to non-synced rows, and then the second could never fire for the case it
    exists for -- a row that reached ``synced`` and whose replica LATER
    disappeared (a workspace DB restored from a backup taken before the
    grant, a shard rebuilt, a manual delete). Nothing else in the system
    repairs that: D-01v3-b makes a missing replica a hard denial
    (``grant_replica_missing``), so such a row would deny every request to
    that workspace forever while Settings shows a green "synced" chip.

    So the drift comparison is deliberately NOT limited to
    ``sync_state != "synced"``. It is the union of:

    * unfinished intent on the control side (``sync_state != "synced"``, or
      a shrink whose ``removed_at`` is still set -- a completed shrink hard
      deletes its control row, so its mere existence means unfinished), and
    * disagreement with the workspace side (no replica at all, or a
      tombstoned replica under a live control row).

    A ``synced`` row whose replica is present and live returns False and the
    pass writes nothing -- which is what keeps a per-minute job on a healthy
    workspace free.
    """

    if snapshot.request.removed_at is not None:
        return True
    if snapshot.sync_state != "synced":
        return True
    if replica is None:
        return True
    return replica.revoked_at is not None


def _read_workspace_replicas(
    database_provider: CredentialReplicaDatabaseProvider,
    workspace_id: UUID,
) -> list[_ReplicaSnapshot]:
    """Every replica row for ``workspace_id``, detached from its session.

    Reclassifies a workspace-plane failure the same way
    ``_apply_to_workspace_plane`` does, but -- unlike the fan-out path --
    deliberately lets it PROPAGATE: this runs on a worker job, not on a
    user's request thread, and a workspace whose database cannot be opened
    is precisely the thing the job should fail on so it becomes visible.
    """

    try:
        workspace_session_factory = database_provider.session_for(workspace_id)
        with workspace_session_factory.begin() as workspace_session:
            rows = workspace_session.scalars(
                select(WorkspaceCredentialGrant).where(
                    WorkspaceCredentialGrant.workspace_id == workspace_id
                )
            ).all()
            return [
                _ReplicaSnapshot(
                    id=row.id,
                    credential_kind=row.credential_kind,
                    credential_id=row.credential_id,
                    synced_revision=row.synced_revision,
                    revoked_at=row.revoked_at,
                )
                for row in rows
            ]
    except SQLAlchemyError as exc:
        if is_database_schema_mismatch(exc):
            raise
        raise WorkspacePlaneUnavailableError(workspace_id=workspace_id) from exc


def _control_index_snapshots(
    session: Session, workspace_id: UUID
) -> list[_ControlIndexSnapshot]:
    """Both control-index shapes for ``workspace_id``, as fan-out requests.

    The PAT side joins ``ApiToken`` for ``user_id``: unlike T4.5's call
    sites, which are already inside the service that knows whose token it is,
    ``ApiTokenWorkspace`` itself carries no ``user_id`` -- and the replica
    row needs one (it is what ``ix_workspace_credential_grant_workspace_user``
    indexes). Rows are included regardless of the token's revoked/expired
    state: authentication fails on the control plane first (section 2.6's
    "revoke·만료" row), so a replica for a dead token is inert, whereas
    guessing at liveness here would let the two planes disagree for no gain.
    """

    snapshots: list[_ControlIndexSnapshot] = []
    pat_rows = session.execute(
        select(ApiTokenWorkspace, ApiToken.user_id)
        .join(ApiToken, ApiToken.id == ApiTokenWorkspace.token_id)
        .where(ApiTokenWorkspace.workspace_id == workspace_id)
    ).all()
    for row, user_id in pat_rows:
        snapshots.append(
            _ControlIndexSnapshot(
                request=CredentialReplicaFanOutRequest(
                    credential_kind="pat",
                    credential_id=row.token_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    desired_scopes=tuple(row.desired_scopes or ()),
                    revision=row.revision,
                    removed_at=row.removed_at,
                ),
                sync_state=row.sync_state,
            )
        )

    consent_rows = session.scalars(
        select(OAuthConsent).where(OAuthConsent.workspace_id == workspace_id)
    ).all()
    for consent in consent_rows:
        snapshots.append(
            _ControlIndexSnapshot(
                request=CredentialReplicaFanOutRequest(
                    credential_kind="oauth",
                    credential_id=resolve_oauth_replica_credential_id(
                        session, user_id=consent.user_id, client_id=consent.client_id
                    ),
                    workspace_id=workspace_id,
                    user_id=consent.user_id,
                    desired_scopes=tuple(consent.desired_scopes or ()),
                    revision=consent.revision,
                    removed_at=consent.removed_at,
                    client_id=consent.client_id,
                ),
                sync_state=consent.sync_state,
            )
        )
    return snapshots


def _oauth_family_key_predicate(family_key: UUID):
    """Match every refresh row belonging to one (user, client) grant family.

    A local copy of ``backend.services.oauth_service._grant_family_predicate``
    -- same ``grant_id``-with-legacy-``id``-fallback semantics, same
    index-friendly OR form rather than an unindexable ``COALESCE`` -- rather
    than an import, because ``oauth_service`` imports THIS module for its own
    fan-out call sites (T4.5) and importing back would be a cycle.
    """

    return or_(
        OAuthRefreshToken.grant_id == family_key,
        and_(
            OAuthRefreshToken.grant_id.is_(None),
            OAuthRefreshToken.id == family_key,
        ),
    )


def _orphan_replica_ids(
    session: Session,
    workspace_id: UUID,
    replicas: list[_ReplicaSnapshot],
) -> list[UUID]:
    """Live replicas in ``workspace_id`` with no control-index row at all.

    Section 2.6: "index에 없는 복제본(백업 복원·재부착 고아)은 tombstone".

    The direction of the OAuth lookup is the dangerous part and is
    deliberately the CONSERVATIVE one -- from the replica back to its
    control row, not the other way around. A replica's ``credential_id`` for
    OAuth is an ``OAuthRefreshToken.grant_id``, which is not a column on
    ``OAuthConsent`` at all (that table's PK is ``(user_id, client_id,
    workspace_id)``), so "does a consent row exist for this replica?" has to
    be answered by first resolving the grant_id back to its (user, client)
    family through ``oauth_refresh_tokens``, then looking up THAT consent.

    The tempting alternative -- build the expected id set forward, by
    running ``resolve_oauth_replica_credential_id`` over each consent row and
    tombstoning any replica outside it -- is wrong, and wrong in the
    direction that destroys live access: that resolver returns the family key
    of the NEWEST refresh row for a (user, client) pair, but a re-
    authorization mints a fresh ``grant_id``, so a still-valid older family's
    replica would fall outside the forward set and be tombstoned. Resolving
    backwards asks the only question that actually matters ("is there a live
    consent covering the family this replica belongs to?") and answers it
    correctly for every family, newest or not.

    Only rows that are not already tombstoned are considered, and a control
    row with ``removed_at`` set is treated as PRESENT (not an orphan): that
    is a shrink whose fan-out has not finished, which ``_needs_apply``
    already retries through the proper protocol (tombstone the replica AND
    hard delete the control row). Tombstoning it here as well would race that
    with a second, weaker mechanism for no benefit.
    """

    orphan_ids: list[UUID] = []
    for replica in replicas:
        if replica.revoked_at is not None:
            continue
        if replica.credential_kind == "pat":
            live = (
                session.get(ApiTokenWorkspace, (replica.credential_id, workspace_id))
                is not None
            )
        elif replica.credential_kind == "oauth":
            live = _oauth_family_has_consent(
                session, family_key=replica.credential_id, workspace_id=workspace_id
            )
        else:
            # Not reachable from this codebase (the column is written only
            # with "pat"/"oauth"), and deliberately NOT treated as an orphan:
            # a kind this build does not understand is also a kind whose
            # control table it cannot check, so "I could not find a control
            # row" would not mean "there is none". A grant of an unknown kind
            # is inert to authorization anyway, which looks rows up BY kind.
            logger.warning(
                "credential replica reconcile skipped unknown credential_kind "
                "%r on workspace %s (replica %s)",
                replica.credential_kind,
                workspace_id,
                replica.id,
            )
            continue
        if not live:
            orphan_ids.append(replica.id)
    return orphan_ids


def _oauth_family_has_consent(
    session: Session, *, family_key: UUID, workspace_id: UUID
) -> bool:
    family = session.execute(
        select(OAuthRefreshToken.user_id, OAuthRefreshToken.client_id)
        .where(_oauth_family_key_predicate(family_key))
        .limit(1)
    ).first()
    if family is None:
        return False
    user_id, client_id = family
    return (
        session.get(OAuthConsent, (user_id, client_id, workspace_id)) is not None
    )


def _tombstone_orphan_replicas(
    database_provider: CredentialReplicaDatabaseProvider,
    workspace_id: UUID,
    orphan_ids: list[UUID],
) -> int:
    """Set ``revoked_at`` on the orphans, in ONE workspace transaction.

    The audit event is emitted on the same workspace session as the write it
    records, which is a deliberate divergence from ``_mark_failed`` (which
    emits on the CONTROL session). ``_mark_failed`` has to be on the control
    session because the row it annotates -- ``sync_state``/
    ``last_sync_error`` on the control index row -- lives there and must move
    atomically with the event. An orphan tombstone has the mirror-image
    property: the row it mutates is workspace-plane, and by definition there
    is no control row left to correlate it with. ``audit_event`` is a
    MIXED_PLANE table (``backend.tenancy_manifest``) precisely so an event
    can live with the fact it records.

    No audit-action registry is consulted: ``backend.audit.emitter``'s
    ``OAUTH_AUDIT_ACTIONS`` gate is OAuth-specific and
    ``credential_replica.*`` deliberately goes through plain
    ``AuditService.emit``, the precedent T4.5 set for
    ``credential_replica.sync_failed``.
    """

    if not orphan_ids:
        return 0
    now = utc_now()
    tombstoned = 0
    try:
        workspace_session_factory = database_provider.session_for(workspace_id)
        with workspace_session_factory.begin() as workspace_session:
            rows = workspace_session.scalars(
                select(WorkspaceCredentialGrant).where(
                    WorkspaceCredentialGrant.workspace_id == workspace_id,
                    WorkspaceCredentialGrant.id.in_(orphan_ids),
                    WorkspaceCredentialGrant.revoked_at.is_(None),
                )
            ).all()
            for row in rows:
                row.revoked_at = now
                tombstoned += 1
                AuditService().emit(
                    session=workspace_session,
                    workspace_id=workspace_id,
                    actor_kind="system",
                    actor_id="credential_replica_reconciler",
                    action="credential_replica.orphan_tombstoned",
                    target_kind="workspace_credential_grant",
                    target_id=row.id,
                    payload={
                        "credential_kind": row.credential_kind,
                        "credential_id": str(row.credential_id),
                        "workspace_id": str(workspace_id),
                        "synced_revision": row.synced_revision,
                        "reason": "no control-plane index row for this replica",
                    },
                    event_summary="Orphaned credential replica tombstoned",
                    retention_class="sensitive",
                )
    except SQLAlchemyError as exc:
        if is_database_schema_mismatch(exc):
            raise
        raise WorkspacePlaneUnavailableError(workspace_id=workspace_id) from exc
    return tombstoned
