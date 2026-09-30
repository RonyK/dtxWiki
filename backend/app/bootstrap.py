"""First-run bootstrap for local single-user dtxWiki installs."""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from backend.app import versioning
from backend.app.data_dir import db_dir, logs_dir
from backend.app.db import make_engine, make_session_factory
from backend.app.settings import Settings
from backend.services.ops import runtime_lock
from backend.services.ops.migration_preflight import (
    MigrationPreflight,
    evaluate_migration_preflight,
)

logger = logging.getLogger("dtxwiki.bootstrap")


class DatabaseNotCurrentError(RuntimeError):
    """The database is not at this code's schema and may not be migrated here.

    Raised instead of migrating whenever automatic bootstrap is off (and by the
    worker and maintenance commands, which never migrate): the operator backs
    the database up and runs ``dtxwiki bootstrap`` deliberately.
    """


def bootstrap(settings: Settings, app=None) -> None:
    """Prepare the database for ``app`` (a serving process) or for a command.

    A serving process first takes the shared runtime lock, so ``dtxwiki
    update``/``rollback``/``migrate-data`` refuse while it runs, and so a
    serving process never starts in the middle of one of them. The lock is
    released when the application's lifespan ends.

    ``auto_bootstrap=false`` never migrates: the database must already be at
    this code's head revision with a matching version marker, or startup is
    refused with the operator's next step.
    """

    settings.validate_session_secret_invariant()
    lock = _hold_runtime_lock(app) if app is not None else None
    try:
        if not settings.auto_bootstrap:
            logger.info(
                "auto_bootstrap=false; verifying the database without migrating"
            )
            verify_database_current(settings)
            if app is not None:
                app.state.app_version_synced = True
            return
        ensure_data_dirs()
        ensure_app_version_synced(settings, app)
    except BaseException:
        if lock is not None:
            lock.release()
        raise


def _hold_runtime_lock(app) -> runtime_lock.RuntimeLock | None:
    state = getattr(app, "state", None)
    existing = getattr(state, "runtime_lock", None)
    if isinstance(existing, runtime_lock.RuntimeLock) and existing.held:
        return None
    lock = runtime_lock.acquire_shared()
    if lock is None:
        return None
    if state is not None:
        state.runtime_lock = lock
    _release_on_shutdown(app, lock)
    return lock


def _release_on_shutdown(app, lock: runtime_lock.RuntimeLock) -> None:
    """Release ``lock`` when the ASGI lifespan of ``app`` ends.

    A process exit releases it as well (the descriptor closes), and so does
    garbage collection of the lock; this makes an in-process shutdown -- a
    test client, an embedding host -- give the lock back promptly too.
    """

    router = getattr(app, "router", None)
    original = getattr(router, "lifespan_context", None)
    if original is None or not callable(original):
        return

    @asynccontextmanager
    async def lifespan_releasing_runtime_lock(lifespan_app):
        try:
            async with original(lifespan_app) as state:
                yield state
        finally:
            lock.release()

    router.lifespan_context = lifespan_releasing_runtime_lock


def ensure_app_version_synced(settings: Settings, app=None) -> None:
    """ADR A13: compare app_version and the concrete Alembic revision.

    - matching version, head, and schema fingerprint: skip upgrade
    - fresh database or supported ``0001_initial_release``: upgrade to head and
      verify the result
    - every other stamp/schema pair: refuse automatic migration
    - DB strictly newer than code: refuse startup (downversion fail-safe)
    """
    from backend.models.app_metadata import AppMetadata

    code_version = settings.app_version
    alembic_head = current_alembic_head()

    # Validate the stamp/schema pair before opening the application's normal
    # SQLite engine, whose WAL configuration can mutate the file header. A
    # rejected same-ID/unknown stamp must fail without changing the database.
    preflight = _database_preflight(settings)
    db_revision = preflight.revision
    if preflight.blocked:
        raise DatabaseNotCurrentError(
            _blocked_refusal(preflight, code_version, "dtxWiki refuses startup")
        )

    db_version = _read_db_version(settings)
    _refuse_unprovable_or_newer_db_version(
        db_version=db_version,
        code_version=code_version,
        db_revision=db_revision,
        subject="dtxWiki refuses startup",
    )

    if db_version == code_version and db_revision == alembic_head:
        logger.info(
            "app_version synced (%s) and alembic revision synced (%s); "
            "alembic skip",
            code_version,
            alembic_head,
        )
    else:
        logger.info(
            "version drift (db_version=%s, code_version=%s, db_revision=%s, "
            "head_revision=%s); running alembic upgrade head",
            db_version or "<absent>",
            code_version,
            db_revision or "<absent>",
            alembic_head,
        )
        run_alembic_upgrade(settings.database_url)
        migrated = _database_preflight(settings)
        if migrated.revision != alembic_head or migrated.verdict != "ready":
            raise RuntimeError(
                "dtxWiki refuses startup: Alembic upgrade did not produce the "
                "current stamped schema; application version metadata was not "
                "updated. Manual operator intervention required (see "
                "DEPLOYMENT.md Troubleshooting)."
            )
        engine = make_engine(settings.database_url)
        try:
            factory = make_session_factory(engine)
            with factory.begin() as session:
                session.merge(
                    AppMetadata(key="dtxwiki_version", value=code_version)
                )
        finally:
            engine.dispose()

    if app is not None:
        app.state.app_version_synced = True


def verify_database_current(
    settings: Settings,
    *,
    subject: str = "dtxWiki refuses startup",
    require_version_marker: bool = True,
) -> None:
    """Refuse unless the database is exactly what this code serves.

    Runs the same stamp/schema preflight as bootstrap and the same version
    checks, but never migrates and never opens the WAL-configuring
    application engine. ``dtxwiki serve`` with ``auto_bootstrap=false``, the
    worker (at startup and on every reclaim cycle) and maintenance commands
    that write the database call this so none of them runs against a schema
    or a version marker it was not built for.

    ``require_version_marker=False`` is for processes that never record the
    marker themselves (the worker, maintenance commands): the schema must be
    this code's head and the database must not be newer, but an absent or
    older marker on a head schema -- a server that has not recorded this
    release yet -- is not a reason to stop.
    """

    code_version = settings.app_version
    alembic_head = current_alembic_head()
    preflight = _database_preflight(settings)
    if preflight.blocked:
        raise DatabaseNotCurrentError(
            _blocked_refusal(preflight, code_version, subject)
        )
    db_version = preflight.recorded_app_version
    _refuse_unprovable_or_newer_db_version(
        db_version=db_version,
        code_version=code_version,
        db_revision=preflight.revision,
        subject=subject,
    )
    if preflight.verdict == "fresh":
        raise DatabaseNotCurrentError(
            f"{subject}: the database has not been bootstrapped yet "
            f"({_redact(settings.database_url)}) and DTXWIKI_AUTO_BOOTSTRAP is "
            "false, so dtxWiki will not create its schema implicitly. Run "
            "'dtxwiki bootstrap' first (see DEPLOYMENT.md)."
        )
    if preflight.revision != alembic_head or (
        require_version_marker and db_version != code_version
    ):
        raise DatabaseNotCurrentError(
            f"{subject}: the database is not at this release's schema "
            f"(db_version={db_version or '<absent>'}, code_version={code_version}, "
            f"db_revision={preflight.revision or '<absent>'}, "
            f"head_revision={alembic_head}) and dtxWiki does not migrate "
            "implicitly when DTXWIKI_AUTO_BOOTSTRAP is false. Back up the "
            "database, then run 'dtxwiki bootstrap' (see DEPLOYMENT.md)."
        )


def _blocked_refusal(
    preflight: MigrationPreflight,
    code_version: str,
    subject: str,
) -> str:
    """Explain a refused stamp/schema pair, downgrade first.

    A database written by a newer dtxWiki carries a revision this code does
    not ship, which the preflight reports as an unknown revision. Its version
    marker is the better evidence: such a database needs the newer binary or
    the backup taken before the upgrade, not a rebuild.
    """

    recorded = preflight.recorded_app_version
    if versioning.is_newer(recorded, code_version):
        return _downversion_refusal(
            db_version=recorded,
            code_version=code_version,
            db_revision=preflight.revision,
            subject=subject,
        )
    return preflight.refusal(subject)


def _refuse_unprovable_or_newer_db_version(
    *,
    db_version: str | None,
    code_version: str,
    db_revision: str | None,
    subject: str,
) -> None:
    # `is_newer` is conservative (unparseable -> not newer) for the
    # update-available check, but that would let an unparseable/empty code
    # version slip past the downversion fail-safe when a real DB version is
    # recorded. Refuse explicitly when we cannot prove the code is not older.
    if (
        db_version is not None
        and versioning.parse_version(db_version) is not None
        and versioning.parse_version(code_version) is None
    ):
        raise DatabaseNotCurrentError(
            f"{subject}: DB version {db_version} is recorded but "
            f"code version {code_version!r} is unparseable; cannot prove the DB "
            "is not newer. Manual operator intervention required (see "
            "DEPLOYMENT.md Troubleshooting). Set a valid DTXWIKI_APP_VERSION or "
            "restore the DB from an earlier backup."
        )
    if versioning.is_newer(db_version, code_version):
        raise DatabaseNotCurrentError(
            _downversion_refusal(
                db_version=db_version,
                code_version=code_version,
                db_revision=db_revision,
                subject=subject,
            )
        )


def _downversion_refusal(
    *,
    db_version: str | None,
    code_version: str,
    db_revision: str | None,
    subject: str,
) -> str:
    return (
        f"{subject}: DB version {db_version} > code version {code_version} "
        f"(db_revision={db_revision or '<unknown>'}). This database was last "
        "used by a newer dtxWiki and cannot be downgraded in place. Roll the "
        f"dtxWiki binary forward to {db_version} or later, or restore the "
        f"database backup that matches {code_version} ('dtxwiki rollback' "
        "restores both together). See DEPLOYMENT.md Troubleshooting."
    )


@dataclass(frozen=True)
class MigrationStatus:
    """Resolved DB-vs-code version state (single source for info/update/banner)."""

    code_version: str
    db_version: str | None
    alembic_head: str | None
    alembic_db: str | None
    status: str


def migration_status(settings: Settings) -> MigrationStatus:
    """Resolve and classify the DB-vs-code migration state.

    Consolidates the logic previously duplicated between
    ``ensure_app_version_synced`` and the ``dtxwiki info`` command. ``status`` is
    one of :data:`backend.app.versioning.MIGRATION_STATUSES`.
    """

    code_version = settings.app_version
    alembic_head = current_alembic_head()
    preflight = _database_preflight(settings)
    alembic_db = preflight.revision
    # A blocked database must not be opened with the application engine: that
    # is what WAL-converts a file we have just refused to identify. Its version
    # marker comes from the preflight's own read-only connection instead.
    db_version = (
        preflight.recorded_app_version
        if preflight.blocked
        else _read_db_version(settings)
    )
    status = versioning.classify_migration_status(
        code_version=code_version,
        db_version=db_version,
        alembic_head=alembic_head,
        alembic_db=alembic_db,
    )
    if preflight.blocked:
        # Not every blocked database has a single stamp row to disagree with
        # the head revision (an unstamped or unversioned-but-populated one has
        # none at all), so the classification cannot see the drift by itself.
        # A newer recorded version is the exception: it names the cause.
        status = (
            "downversion"
            if versioning.is_newer(db_version, code_version)
            else "drift"
        )
    return MigrationStatus(
        code_version=code_version,
        db_version=db_version,
        alembic_head=alembic_head,
        alembic_db=alembic_db,
        status=status,
    )


def _read_db_version(settings: Settings) -> str | None:
    """Read the recorded ``dtxwiki_version`` from AppMetadata, or ``None``."""

    from backend.models.app_metadata import AppMetadata

    try:
        engine = make_engine(settings.database_url)
    except Exception:
        return None
    try:
        factory = make_session_factory(engine)
        with factory.begin() as session:
            row = session.get(AppMetadata, "dtxwiki_version")
            return row.value if row else None
    except Exception:
        # Table may not exist yet (first bootstrap run)
        return None
    finally:
        engine.dispose()


def current_alembic_head() -> str:
    cfg = Config(str(resolve_alembic_ini()))
    cfg.set_main_option("script_location", str(resolve_migrations_dir()))
    return ScriptDirectory.from_config(cfg).get_current_head()


def current_db_alembic_revision(settings: Settings) -> str | None:
    return _database_preflight(settings).revision


def _database_preflight(settings: Settings) -> MigrationPreflight:
    """Read the configured database's stamp/schema verdict exactly once."""

    return evaluate_migration_preflight(settings.database_url)


def ensure_data_dirs() -> None:
    db_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)


def run_alembic_upgrade(database_url: str) -> None:
    cfg = Config(str(resolve_alembic_ini()))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.set_main_option("script_location", str(resolve_migrations_dir()))
    logger.info("alembic upgrade head (db=%s)", _redact(database_url))
    command.upgrade(cfg, "head")


def resolve_alembic_ini() -> Path:
    package_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    if package_ini.is_file():
        return package_ini
    repo_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    if repo_ini.is_file():
        return repo_ini
    raise FileNotFoundError("alembic.ini not found")


def resolve_migrations_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "migrations"


def _redact(url: str) -> str:
    # Codex #47: also mask ?password=/&token= query creds, not just userinfo.
    from backend.utils.redact import redact_url

    return redact_url(url)
