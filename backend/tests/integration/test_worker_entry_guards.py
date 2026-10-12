"""Worker startup and shutdown guards (M-9a, C10, d-3).

The worker never migrates when it is told not to, but it always proves the
database is the schema it was built for -- at startup and again on every
reclaim cycle -- holds the shared runtime lock while it runs, and gives a job
it was interrupted in back to the queue on SIGTERM/SIGINT.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text
from typer.testing import CliRunner

from backend.app.bootstrap import (
    ensure_data_dirs,
    resolve_alembic_ini,
    resolve_migrations_dir,
)
from backend.app.db import make_session_factory_from_settings
from backend.app.settings import Settings
from backend.models.job import Job
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.job_queue import JobQueueService
from backend.services.ops import runtime_lock
from backend.tests.utils.bootstrapped_sqlite import copy_migrated_sqlite
from backend.worker import entry as worker_entry
from backend.worker.entry import app as worker_cli
from backend.worker.runtime import OneShotWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX signals and flock semantics"
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "data-dir"
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(path))
    return path


class _SignalReachedTheTest(Exception):
    pass


@pytest.fixture
def signal_backstop():
    """Fail the test, not the pytest process, if the worker ignores a signal.

    The worker under test runs in this process. Without its own handlers a
    SIGTERM would kill pytest and a SIGINT would end the whole session.
    """

    def _backstop(signum, _frame):
        raise _SignalReachedTheTest(signal.Signals(signum).name)

    previous = {
        shutdown_signal: signal.signal(shutdown_signal, _backstop)
        for shutdown_signal in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        yield
    finally:
        for shutdown_signal, handler in previous.items():
            signal.signal(shutdown_signal, handler)


def _database_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'worker-guards.sqlite'}"


def _migrated_database_url(tmp_path: Path) -> str:
    """An empty database at the head schema, as ``bootstrap`` leaves it.

    These tests are about what the worker does with a prepared database, not
    about migrating one, so they start from a copy of a once-built template.
    """

    ensure_data_dirs()
    copy_migrated_sqlite(tmp_path / "worker-guards.sqlite")
    return _database_url(tmp_path)


def _record_db_version(database_url: str, value: str) -> None:
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO app_metadata (key, value) "
                    "VALUES ('dtxwiki_version', :value) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"value": value},
            )
    finally:
        engine.dispose()


def _queue_job(database_url: str, *, job_type: str = "lint_tick") -> UUID:
    factory = make_session_factory_from_settings(Settings(database_url=database_url))
    with factory.begin() as session:
        workspace = seed_default_workspace(session)
        job = JobQueueService().enqueue(
            session=session,
            workspace_id=workspace.id,
            job_type=job_type,
            trigger_type="scheduled",
            idempotency_key=f"worker-guards:{job_type}",
        )
        return job.id


def _job(database_url: str, job_id: UUID) -> Job:
    factory = make_session_factory_from_settings(Settings(database_url=database_url))
    with factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def test_worker_refuses_a_database_written_by_a_newer_release(tmp_path, data_dir):
    """The audit case: a skipped bootstrap used to mean no check at all."""

    database_url = _migrated_database_url(tmp_path)
    job_id = _queue_job(database_url)
    _record_db_version(database_url, "99.0.0")

    result = CliRunner().invoke(
        worker_cli, ["run-once", "--db", database_url, "--skip-bootstrap"]
    )

    assert result.exit_code == worker_entry.DATABASE_NOT_CURRENT_EXIT_CODE
    assert "DB version 99.0.0 > code version" in result.output
    assert _job(database_url, job_id).status == "queued"


def test_worker_refuses_a_schema_that_still_needs_migration(tmp_path, data_dir):
    database_url = _database_url(tmp_path)
    config_ini = str(resolve_alembic_ini())
    from alembic.config import Config

    config = Config(config_ini)
    config.set_main_option("sqlalchemy.url", database_url)
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    command.upgrade(config, "bd62193d5cf6")

    result = CliRunner().invoke(
        worker_cli,
        ["run", "--db", database_url, "--skip-bootstrap", "--idle-exit-after", "1"],
    )

    assert result.exit_code == worker_entry.DATABASE_NOT_CURRENT_EXIT_CODE
    assert "db_revision=bd62193d5cf6" in result.output


def test_worker_accepts_a_head_schema_whose_marker_is_not_recorded_yet(
    tmp_path, data_dir
):
    """A server that has not recorded this release yet is no reason to stop."""

    database_url = _migrated_database_url(tmp_path)
    # Only the upgrade ran, so the release marker was never recorded.
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM app_metadata WHERE key = 'dtxwiki_version'")
            )
        with engine.connect() as connection:
            remaining = connection.execute(
                text("SELECT COUNT(*) FROM app_metadata WHERE key = 'dtxwiki_version'")
            ).scalar_one()
    finally:
        engine.dispose()
    assert remaining == 0

    result = CliRunner().invoke(
        worker_cli, ["run-once", "--db", database_url, "--skip-bootstrap"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout.strip()) == {"status": "idle"}


def test_worker_does_not_start_while_maintenance_holds_the_runtime_lock(
    tmp_path, data_dir
):
    database_url = _migrated_database_url(tmp_path)

    with runtime_lock.acquire_exclusive(data_dir):
        result = CliRunner().invoke(
            worker_cli, ["run-once", "--db", database_url, "--skip-bootstrap"]
        )

    assert result.exit_code == worker_entry.RUNTIME_LOCKED_EXIT_CODE
    assert "update, rollback or migrate-data" in result.output


def test_a_running_worker_holds_the_runtime_lock(tmp_path, data_dir, monkeypatch):
    database_url = _migrated_database_url(tmp_path)
    refusals: list[str] = []

    def _probe_maintenance(_seconds: float) -> None:
        try:
            runtime_lock.acquire_exclusive(data_dir).release()
        except runtime_lock.RuntimeLockHeldError as exc:
            refusals.append(str(exc))

    monkeypatch.setattr(worker_entry.time, "sleep", _probe_maintenance)
    result = CliRunner().invoke(
        worker_cli,
        [
            "run",
            "--db",
            database_url,
            "--skip-bootstrap",
            "--idle-exit-after",
            "2",
            "--poll-interval-seconds",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(refusals) == 1
    assert "stop dtxWiki (server and worker) first" in refusals[0]
    runtime_lock.acquire_exclusive(data_dir).release()


def test_worker_rechecks_the_database_on_every_reclaim_cycle(
    tmp_path, data_dir, monkeypatch
):
    """An upgrade performed under a running worker stops it at the next cycle."""

    monkeypatch.setenv("DTXWIKI_WORKER_LEASE_TIMEOUT_SECONDS", "1")
    database_url = _migrated_database_url(tmp_path)
    upgraded: list[bool] = []
    clock_offset = [0.0]

    def _newer_release_upgrades_the_database(_seconds: float) -> None:
        if not upgraded:
            _record_db_version(database_url, "99.0.0")
            upgraded.append(True)
            # Let reclaim's one-second minimum cadence elapse once, on the
            # worker's clock only, after the new marker is committed.
            clock_offset[0] += 1.1

    monkeypatch.setattr(
        worker_entry,
        "time",
        types.SimpleNamespace(
            monotonic=lambda: time.monotonic() + clock_offset[0],
            sleep=_newer_release_upgrades_the_database,
        ),
    )
    result = CliRunner().invoke(
        worker_cli,
        [
            "run",
            "--db",
            database_url,
            "--skip-bootstrap",
            "--idle-exit-after",
            "50",
            "--poll-interval-seconds",
            "0",
        ],
    )

    assert upgraded
    assert result.exit_code == worker_entry.DATABASE_NOT_CURRENT_EXIT_CODE
    assert "DB version 99.0.0 > code version" in result.output


def test_sigterm_during_a_job_releases_it_without_charging_a_retry(
    tmp_path, data_dir, monkeypatch, signal_backstop
):
    database_url = _migrated_database_url(tmp_path)
    job_id = _queue_job(database_url)
    started: list[UUID] = []

    # Startup seeds the periodic jobs, a second ``lint_tick`` among them, and
    # ``claim_next`` takes the oldest job by ``created_at``. Both rows get that
    # from the host clock, which steps back (NTP, VM resume), so on a bad day
    # the seeded one is the older and the worker is stopped in the wrong job.
    # The real seeding still runs; the two competing rows are then given the
    # order they were meant to have.
    seed_due_jobs = worker_entry._seed_due_jobs
    factory = make_session_factory_from_settings(Settings(database_url=database_url))
    created_at = datetime(2026, 1, 1, tzinfo=UTC)

    def _seed_with_the_queued_job_first(database_provider, settings):
        seed_due_jobs(database_provider, settings)
        with factory.begin() as session:
            for job in session.scalars(select(Job).where(Job.job_type == "lint_tick")):
                job.created_at = (
                    created_at
                    if job.id == job_id
                    else created_at + timedelta(seconds=1)
                )

    monkeypatch.setattr(worker_entry, "_seed_due_jobs", _seed_with_the_queued_job_first)

    def _stopped_mid_job(self, *, session, job):
        started.append(job.id)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(10)
        raise AssertionError("SIGTERM did not interrupt the business phase")

    monkeypatch.setattr(OneShotWorker, "_run_business_logic", _stopped_mid_job)
    result = CliRunner().invoke(
        worker_cli,
        [
            "run",
            "--db",
            database_url,
            "--skip-bootstrap",
            "--job-type",
            "lint_tick",
            "--poll-interval-seconds",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert started == [job_id]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "signal": "SIGTERM",
        "status": "stopped",
    }
    job = _job(database_url, job_id)
    assert job.status == "queued"
    assert job.started_at is None
    assert job.retry_count == 0
    assert job.last_error == "requeued after the worker was stopped"


def test_sigint_while_idle_stops_the_loop(
    tmp_path, data_dir, monkeypatch, signal_backstop
):
    database_url = _migrated_database_url(tmp_path)
    timers: list[threading.Timer] = []

    def _idle_dispatch(self, **_kwargs):
        if not timers:
            timer = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGINT))
            timers.append(timer)
            timer.start()
        return None

    monkeypatch.setattr(
        OneShotWorker, "dispatch_once_with_session_factory", _idle_dispatch
    )
    started = time.monotonic()
    result = CliRunner().invoke(
        worker_cli,
        [
            "run",
            "--db",
            database_url,
            "--skip-bootstrap",
            "--poll-interval-seconds",
            "30",
        ],
    )

    assert result.exit_code == 0, result.output
    assert time.monotonic() - started < 20
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "signal": "SIGINT",
        "status": "stopped",
    }


def test_worker_process_exits_cleanly_on_sigterm(tmp_path, data_dir):
    """The real process boundary: what systemd and `docker stop` send."""

    database_url = _migrated_database_url(tmp_path)
    environment = {
        **os.environ,
        "DTXWIKI_DATA_DIR": str(data_dir),
        "PYTHONPATH": str(REPO_ROOT),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backend.worker.entry",
            "run",
            "--db",
            database_url,
            "--skip-bootstrap",
            "--poll-interval-seconds",
            "0.2",
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            if '"status": "idle"' in line:
                break
        else:
            pytest.fail("worker never reported idle")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == 0, stderr
    assert '"status": "stopped"' in stdout
    runtime_lock.acquire_exclusive(data_dir).release()
