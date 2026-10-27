import json
import logging
import os
import signal
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import typer
from sqlalchemy import select

from backend.app.bootstrap import DatabaseNotCurrentError, verify_database_current
from backend.app.bootstrap import bootstrap_core as bootstrap_app
from backend.app.container import BlobStoreProvider, DatabaseProvider
from backend.app.plugins import load_plugins
from backend.app.runtime_resources import RuntimeResources
from backend.app.settings import Settings
from backend.models.workspace import Workspace
from backend.services.background_job_retention import prune_background_history
from backend.services.context_media_policy import resolve_context_media_policy
from backend.services.job_queue import JobQueueService
from backend.services.mail_delivery import EmailDeliveryService
from backend.services.ops import runtime_lock
from backend.worker.periodic import due_job_types
from backend.worker.runtime import OneShotWorker, WorkerShutdownRequested
from backend.worker.tick import seed_jobs_for_workspace

logger = logging.getLogger(__name__)

app = typer.Typer(no_args_is_help=True, help="Run dtxWiki background worker jobs.")

_DISPATCH_ERROR_BACKOFF_CAP_SECONDS = 30.0
_EMAIL_SWEEP_MIN_INTERVAL_SECONDS = 1.0
_HISTORY_SWEEP_BUDGET_SECONDS = 1.0
_HISTORY_BATCH_SIZE = 200


@dataclass
class _HistorySweepState:
    cursors: dict[UUID, UUID | None] = field(default_factory=dict)
    next_workspace: int = 0


#: The database is not this code's schema, or was written by a newer release.
DATABASE_NOT_CURRENT_EXIT_CODE = 3
#: ``dtxwiki update``/``rollback``/``migrate-data`` holds the runtime lock.
RUNTIME_LOCKED_EXIT_CODE = 4
_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class _ShutdownSignals:
    """SIGTERM/SIGINT: stop claiming work and give the running job back.

    The first signal records the request. Inside an interruptible section --
    an idle or back-off sleep, or the business phase of a claimed job -- it
    also raises :class:`WorkerShutdownRequested`, which the dispatcher turns
    into a lease release rather than a failed attempt. Anywhere else (a claim
    or status transaction, a mail sweep) the work in hand finishes and the
    loop stops at its next boundary.
    """

    def __init__(self) -> None:
        self.signal_name: str | None = None
        self._interruptible_depth = 0

    @property
    def requested(self) -> bool:
        return self.signal_name is not None

    def handle(self, signum: int, _frame) -> None:
        name = signal.Signals(signum).name
        if self.signal_name is None:
            self.signal_name = name
            logger.info("worker received %s; stopping", name)
        if self._interruptible_depth:
            raise WorkerShutdownRequested(name)

    @contextmanager
    def interruptible(self) -> Iterator[None]:
        if self.signal_name is not None:
            raise WorkerShutdownRequested(self.signal_name)
        self._interruptible_depth += 1
        try:
            yield
        finally:
            self._interruptible_depth -= 1

    def sleep(self, seconds: float) -> None:
        with self.interruptible():
            time.sleep(seconds)

    @contextmanager
    def installed(self) -> Iterator["_ShutdownSignals"]:
        previous = {}
        try:
            for shutdown_signal in _SHUTDOWN_SIGNALS:
                previous[shutdown_signal] = signal.signal(shutdown_signal, self.handle)
        except ValueError:
            # Not the main thread (an embedding host): the host owns signals.
            pass
        try:
            yield self
        finally:
            for shutdown_signal, handler in previous.items():
                signal.signal(shutdown_signal, handler)

    def stopped_payload(self) -> str:
        return json.dumps(
            {"signal": self.signal_name, "status": "stopped"}, sort_keys=True
        )


@contextmanager
def _worker_runtime(settings: Settings, *, skip_bootstrap: bool) -> Iterator[None]:
    """Hold the shared runtime lock and prove the database fits this code.

    The lock makes ``dtxwiki update``/``rollback``/``migrate-data`` refuse
    while this worker runs. Migrations run only where they are allowed
    (``--bootstrap`` with automatic bootstrap on); the compatibility check
    runs regardless, so a skipped bootstrap can no longer mean a worker
    writing to a schema it was not built for.
    """

    bootstrap_requested = not skip_bootstrap and settings.auto_bootstrap
    load_plugins(settings)
    with ExitStack() as guards:
        try:
            resources = RuntimeResources.for_settings(settings)
            activity = resources.acquire_activity(
                "exclusive" if bootstrap_requested else "shared"
            )
            guards.enter_context(activity)
            # All consuming workers, including a standalone external worker,
            # obey the same DB-side consumer lock. Managed registration owns
            # another SH lease, but never a second consumer lock.
            guards.enter_context(resources.acquire_consumer())
        except (runtime_lock.RuntimeLockHeldError, OSError, ValueError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=RUNTIME_LOCKED_EXIT_CODE) from exc
        environment = resources.export_environment()
        previous_environment = {key: os.environ.get(key) for key in environment}
        os.environ.update(environment)
        try:
            if bootstrap_requested:
                try:
                    bootstrap_app(settings)
                except DatabaseNotCurrentError as exc:
                    _refuse_database(exc)
                # A migration requires quiescent storage. Keep the consumer
                # lock continuously while changing to the lifetime SH lease;
                # a maintenance operation winning this gap makes us refuse
                # before any job can be consumed.
                activity.release()
                try:
                    guards.enter_context(resources.acquire_activity("shared"))
                except (runtime_lock.RuntimeLockHeldError, OSError, ValueError) as exc:
                    typer.echo(str(exc), err=True)
                    raise typer.Exit(code=RUNTIME_LOCKED_EXIT_CODE) from exc
            _verify_database(settings)
            yield
        finally:
            for key, previous in previous_environment.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous


def _verify_database(settings: Settings) -> None:
    try:
        verify_database_current(
            settings,
            subject="dtxwiki-worker refuses to run",
            require_version_marker=False,
        )
    except DatabaseNotCurrentError as exc:
        _refuse_database(exc)


def _refuse_database(exc: DatabaseNotCurrentError) -> None:
    logger.error("%s", exc)
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=DATABASE_NOT_CURRENT_EXIT_CODE) from exc


@app.callback()
def root() -> None:
    pass


@app.command(name="run-once")
def run_once(
    db: Annotated[
        str | None,
        typer.Option(help="Override database URL."),
    ] = None,
    workspace_id: Annotated[
        UUID | None,
        typer.Option(help="Limit claim to a workspace."),
    ] = None,
    job_type: Annotated[
        list[str] | None,
        typer.Option("--job-type", help="Limit claim to one or more job types."),
    ] = None,
    skip_bootstrap: Annotated[
        bool,
        typer.Option(
            "--skip-bootstrap/--bootstrap",
            envvar="SKIP_BOOTSTRAP",
            help=(
                "Skip migrations at startup. Set where another step already "
                "bootstrapped (Docker init service, `dtxwiki setup production`)."
            ),
        ),
    ] = False,
) -> None:
    settings = Settings(database_url=db) if db else Settings()
    shutdown = _ShutdownSignals()
    with (
        _worker_runtime(settings, skip_bootstrap=skip_bootstrap),
        shutdown.installed(),
    ):
        database_provider = DatabaseProvider(settings)
        blob_store_provider = BlobStoreProvider(settings)
        factory = database_provider.control_session_factory()
        worker = OneShotWorker(
            settings=settings,
            database_provider=database_provider,
            blob_store_provider=blob_store_provider,
        )
        worker.business_phase_guard = shutdown.interruptible
        worker.reclaim_stale_jobs_with_session_factory(
            session_factory=factory,
            lease_timeout_seconds=settings.worker_lease_timeout_seconds,
        )
        # One bounded outbox pass (EMAIL_OUTBOX_SWEEP_LIMIT rows) before
        # dispatch. run-once never swept at all, which was survivable only
        # while an email_delivery Job existed to carry delivery through the
        # dispatcher -- with that path deleted (MAIL-02) the sweep is the ONLY
        # delivery route, so a one-shot worker that skipped it could never
        # deliver mail. Unlike _seed_due_jobs (deliberately absent here, see
        # test_worker_run_once_does_not_seed_periodic_jobs) this creates no
        # work; it only finishes work that is already queued and due.
        _sweep_email_outbox(database_provider, settings)
        if shutdown.requested:
            typer.echo(shutdown.stopped_payload())
            return
        # BIZ-H-4: use the two-phase dispatcher so a commit-time failure on the
        # job status update cannot roll back already-applied business writes.
        try:
            result = worker.dispatch_once_with_session_factory(
                session_factory=factory,
                workspace_id=workspace_id,
                job_types=set(job_type) if job_type else None,
            )
        except WorkerShutdownRequested:
            typer.echo(shutdown.stopped_payload())
            return
        if result is None:
            typer.echo(json.dumps({"status": "idle"}))
            return
        typer.echo(json.dumps(result.to_payload(), sort_keys=True))


@app.command(name="run")
def run(
    db: Annotated[
        str | None,
        typer.Option(help="Override database URL."),
    ] = None,
    workspace_id: Annotated[
        UUID | None,
        typer.Option(help="Limit claims to a workspace."),
    ] = None,
    job_type: Annotated[
        list[str] | None,
        typer.Option("--job-type", help="Limit claims to one or more job types."),
    ] = None,
    poll_interval_seconds: Annotated[
        float,
        typer.Option(help="Seconds to sleep when no job is available."),
    ] = 1.0,
    max_jobs: Annotated[
        int | None,
        typer.Option(help="Exit after processing this many jobs."),
    ] = None,
    idle_exit_after: Annotated[
        int | None,
        typer.Option(help="Exit after this many consecutive idle polls."),
    ] = None,
    skip_bootstrap: Annotated[
        bool,
        typer.Option(
            "--skip-bootstrap/--bootstrap",
            envvar="SKIP_BOOTSTRAP",
            help=(
                "Skip migrations at startup. Set where another step already "
                "bootstrapped (Docker init service, `dtxwiki setup production`)."
            ),
        ),
    ] = False,
) -> None:
    if poll_interval_seconds < 0:
        raise typer.BadParameter("poll interval must be non-negative")
    if max_jobs is not None and max_jobs < 1:
        raise typer.BadParameter("max jobs must be at least 1")
    if idle_exit_after is not None and idle_exit_after < 1:
        raise typer.BadParameter("idle exit count must be at least 1")

    settings = Settings(database_url=db) if db else Settings()
    shutdown = _ShutdownSignals()
    with (
        _worker_runtime(settings, skip_bootstrap=skip_bootstrap),
        shutdown.installed(),
    ):
        try:
            _run_loop(
                settings,
                shutdown,
                workspace_id=workspace_id,
                job_type=job_type,
                poll_interval_seconds=poll_interval_seconds,
                max_jobs=max_jobs,
                idle_exit_after=idle_exit_after,
            )
        except WorkerShutdownRequested:
            # Raised from an idle sleep or a job's business phase; the
            # dispatcher has already given an interrupted job back.
            pass
        if shutdown.requested:
            typer.echo(shutdown.stopped_payload())


def _run_loop(
    settings: Settings,
    shutdown: _ShutdownSignals,
    *,
    workspace_id: UUID | None,
    job_type: list[str] | None,
    poll_interval_seconds: float,
    max_jobs: int | None,
    idle_exit_after: int | None,
    on_ready: Callable[[], None] | None = None,
    report_idle: bool = True,
) -> None:
    database_provider = DatabaseProvider(settings)
    blob_store_provider = BlobStoreProvider(settings)
    factory = database_provider.control_session_factory()
    worker = OneShotWorker(
        settings=settings,
        database_provider=database_provider,
        blob_store_provider=blob_store_provider,
    )
    worker.business_phase_guard = shutdown.interruptible
    history_state = _HistorySweepState()
    processed_count = 0
    idle_count = 0
    job_types = set(job_type) if job_type else None
    lease_timeout_seconds = settings.worker_lease_timeout_seconds
    reclaim_interval_seconds = max(lease_timeout_seconds / 2.0, 1.0)
    # Phase 6b (plan §3 D-2 "주기적 전체 순회"): the hint fast path
    # (dispatch_once_with_session_factory's default bypass_hint=False) already
    # falls back to a full round-robin sweep on every call where it finds
    # nothing hinted, which is the common case. This timer is the explicit,
    # hint-INDEPENDENT backstop for the narrow case that fallback doesn't
    # cover: hints happening to keep hitting on some OTHER workspace every
    # single call, which would otherwise let an un-hinted (or hint-cleared)
    # workspace's queued job wait indefinitely for the fast path to ever come
    # up empty. Reusing reclaim's cadence -- both are periodic correctness
    # backstops the dispatch loop must run regardless of anything else that
    # happened this iteration.
    full_sweep_interval_seconds = reclaim_interval_seconds
    recovery = worker.recover_initial_stale_jobs_with_session_factory(
        session_factory=factory,
        lease_timeout_seconds=lease_timeout_seconds,
    )
    if not recovery.roster_available:
        raise RuntimeError(
            "worker initial recovery failed: control database workspace roster "
            "is unavailable"
        )
    if recovery.failed_workspace_ids:
        logger.warning(
            "worker initial recovery partial; deferred workspaces: %s",
            ", ".join(map(str, recovery.failed_workspace_ids)),
        )
    if shutdown.requested:
        return
    if on_ready is not None:
        on_ready()
    # Periodic registration and external mail delivery are not prerequisites
    # for accepting jobs. In particular, SMTP must not delay the ready packet.
    _seed_due_jobs(database_provider, settings, history_state)
    _sweep_email_outbox(database_provider, settings)
    last_reclaim_monotonic = time.monotonic()
    last_full_sweep_monotonic = time.monotonic()
    last_email_sweep_monotonic = time.monotonic()
    consecutive_dispatch_errors = 0

    while True:
        if shutdown.requested:
            return
        if time.monotonic() - last_reclaim_monotonic >= reclaim_interval_seconds:
            # A long-running worker re-proves the database on reclaim's
            # cadence: an upgrade (or a restore) performed while it kept
            # running must stop it instead of letting it write to a schema
            # its code was not built for.
            _verify_database(settings)
            worker.reclaim_stale_jobs_with_session_factory(
                session_factory=factory,
                lease_timeout_seconds=lease_timeout_seconds,
            )
            _seed_due_jobs(database_provider, settings, history_state)
            last_reclaim_monotonic = time.monotonic()
        # The outbox sweep is NOT on reclaim's cadence any more. It used to
        # be, because an email_delivery Job carried the latency and the sweep
        # was only a backstop; with that path deleted (MAIL-02) this IS the
        # delivery route, and reclaim_interval_seconds is
        # worker_lease_timeout_seconds/2 -- 300 s at the default -- which
        # would mean a password-reset mail sitting in the outbox for five
        # minutes. Its own minimum-interval timer runs it about once a
        # second: the due-row scan is one indexed lookup on
        # idx_email_outbox_claim that returns nothing when the outbox is
        # idle, strictly cheaper than the INSERT + workspace-plane claim +
        # hint write the deleted Job cost per email.
        if (
            time.monotonic() - last_email_sweep_monotonic
            >= _EMAIL_SWEEP_MIN_INTERVAL_SECONDS
        ):
            _sweep_email_outbox(database_provider, settings)
            last_email_sweep_monotonic = time.monotonic()
        bypass_hint = False
        if time.monotonic() - last_full_sweep_monotonic >= full_sweep_interval_seconds:
            bypass_hint = True
            last_full_sweep_monotonic = time.monotonic()
        # BIZ-H-4: use the two-phase dispatcher so a commit-time failure on the
        # job status update cannot roll back already-applied business writes.
        # Transient dispatch failures (e.g. a dropped DB connection during the
        # claim transaction) must not kill the daemon: log, back off, retry.
        try:
            result = worker.dispatch_once_with_session_factory(
                session_factory=factory,
                workspace_id=workspace_id,
                job_types=job_types,
                bypass_hint=bypass_hint,
            )
        except Exception as exc:
            consecutive_dispatch_errors += 1
            logger.exception(
                "worker dispatch failed (consecutive errors: %d); retrying",
                consecutive_dispatch_errors,
            )
            typer.echo(
                json.dumps({"error": str(exc), "status": "error"}, sort_keys=True)
            )
            shutdown.sleep(
                min(
                    max(poll_interval_seconds, 1.0) * consecutive_dispatch_errors,
                    _DISPATCH_ERROR_BACKOFF_CAP_SECONDS,
                )
            )
            continue

        consecutive_dispatch_errors = 0
        if result is None:
            idle_count += 1
            if report_idle:
                typer.echo(json.dumps({"status": "idle"}, sort_keys=True))
            if idle_exit_after is not None and idle_count >= idle_exit_after:
                return
            shutdown.sleep(poll_interval_seconds)
            continue

        idle_count = 0
        processed_count += 1
        typer.echo(json.dumps(result.to_payload(), sort_keys=True))
        if max_jobs is not None and processed_count >= max_jobs:
            return


def main() -> None:
    app()


def _seed_due_jobs(
    database_provider: DatabaseProvider,
    settings: Settings,
    history_state: _HistorySweepState | None = None,
) -> None:
    """Fan out periodic job seeding across every workspace's plane (Epic-2
    Phase 6a, plan §4 Phase 6): reads the workspace roster from the control
    plane once, then seeds each workspace's periodic jobs in its own
    transaction on session_for(workspace_id) -- ``seed_jobs_for_workspace``
    itself never touches ``Workspace``, unlike ``seed_due_jobs`` (the
    single-session convenience wrapper tests use), so a real overlay's
    per-workspace DB only ever sees writes for its own workspace.

    A workspace whose seeding raises (its DB unreachable, mid-sweep
    deletion, etc.) is logged and skipped rather than aborting the rest of
    the sweep -- the same "one broken workspace can't take down the others"
    contract as reclaim_stale_jobs_with_session_factory.
    """
    try:
        with database_provider.control_session_factory().begin() as control_session:
            workspace_ids = []
            workspace_policies = []
            for workspace in control_session.scalars(
                select(Workspace).order_by(Workspace.id)
            ):
                workspace_ids.append(workspace.id)
                try:
                    policy = resolve_context_media_policy(
                        workspace, settings.context_media_orphan_retention_hours
                    )
                    workspace_policies.append((workspace.id, policy))
                except Exception:
                    logger.exception(
                        "invalid retention policy for workspace %s", workspace.id
                    )
    except Exception:
        logger.exception("periodic job seeding failed reading workspace roster")
        return

    queue = JobQueueService()
    now = datetime.now(UTC)
    failed_workspaces: set[UUID] = set()
    for workspace_id, policy in workspace_policies:
        try:
            # Close every precheck read before beginning registration writes.
            # Credential reconciliation reads both planes sequentially here.
            due_types = due_job_types(
                workspace_id,
                now=now,
                settings=settings,
                policy=policy,
                database_provider=database_provider,
            )
            if due_types:
                with database_provider.session_for(workspace_id).begin() as ws_session:
                    seed_jobs_for_workspace(
                        ws_session,
                        workspace_id,
                        now=now,
                        job_queue=queue,
                        settings=settings,
                        policy=policy,
                        due_types=due_types,
                    )
        except Exception:
            failed_workspaces.add(workspace_id)
            logger.exception(
                "periodic job seeding failed for workspace %s", workspace_id
            )
    _prune_background_history(
        database_provider,
        [
            workspace_id
            for workspace_id in workspace_ids
            if workspace_id not in failed_workspaces
        ],
        now,
        history_state if history_state is not None else _HistorySweepState(),
    )


def _prune_background_history(
    database_provider: DatabaseProvider,
    workspace_ids: list[UUID],
    now: datetime,
    state: _HistorySweepState,
) -> None:
    """Short round-robin batches; dispatch gets control back within the budget.

    Check the shared deadline before each transaction. One in-flight batch
    may finish beyond it. Carry cursors across ticks so unknown/protected keys
    and large workspaces cannot indefinitely hide other eligible history.
    No cleanup Job is created, and no automatic VACUUM is issued.
    """
    deadline = time.monotonic() + _HISTORY_SWEEP_BUDGET_SECONDS
    if not workspace_ids:
        return
    positions = {
        workspace_id: index for index, workspace_id in enumerate(workspace_ids)
    }
    state.cursors = {
        workspace_id: cursor
        for workspace_id, cursor in state.cursors.items()
        if workspace_id in positions
    }
    start = state.next_workspace % len(workspace_ids)
    pending = deque(workspace_ids[start:] + workspace_ids[:start])
    while pending and time.monotonic() < deadline:
        workspace_id = pending.popleft()
        # Resume the next tick with the workspace after this one, even if
        # this transaction exhausts the shared budget or fails.
        state.next_workspace = (positions[workspace_id] + 1) % len(workspace_ids)
        try:
            batch = prune_background_history(
                database_provider.session_for(workspace_id),
                workspace_id,
                now=now,
                batch_size=_HISTORY_BATCH_SIZE,
                job_cursor=state.cursors.get(workspace_id),
            )
            state.cursors[workspace_id] = batch.next_job_cursor
            if (
                batch.next_job_cursor is not None
                or batch.receipts_deleted == _HISTORY_BATCH_SIZE
            ):
                pending.append(workspace_id)
        except Exception:
            logger.exception(
                "background history pruning failed for workspace %s", workspace_id
            )


def _sweep_email_outbox(
    database_provider: DatabaseProvider, settings: Settings
) -> None:
    """Deliver due password-reset/email-verification mail. Since MAIL-02 this is
    the ONLY delivery route: the `email_delivery` Job that used to carry it
    is gone, because email_outbox is identity-plane data and that Job was
    workspace-sharded, so it landed where no claim path could reach it.

    Runs on the control plane: email_outbox has no workspace_id column (FKs
    to users and password_reset_tokens only) -- it is identity-plane data,
    the same category CONTROL_SESSION_FACTORY_ALLOWLIST already accepts this
    file for via _seed_due_jobs's Workspace roster read and worker/runtime.
    py's terminal-failure notification email.

    The factory is handed over WHOLE, not opened here: sweep_due owns its own
    transaction boundaries so that no provider send ever runs inside one (see
    EmailDeliveryService.deliver). Wrapping this call in a transaction would
    reinstate exactly the defect MAIL-01 removes.

    Same "one broken thing must not abort the rest" shape as
    reclaim_stale_jobs_with_session_factory/_seed_due_jobs above: a failure
    here is logged and skipped, not raised, so it cannot take down the
    reclaim/seed sweep it runs alongside.
    """
    try:
        EmailDeliveryService(settings=settings).sweep_due(
            session_factory=database_provider.control_session_factory()
        )
    except Exception:
        logger.exception("email outbox sweep failed")


if __name__ == "__main__":
    main()
