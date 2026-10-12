"""Tests for dtxWiki version mechanism (Task 4.13 — ADR A13)."""

import json
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from backend.app.settings import Settings
from backend.tests.utils.migrated_sqlite_templates import shared_templates


def _current_alembic_head() -> str:
    from backend.app.bootstrap import current_alembic_head

    return current_alembic_head()


def _alembic_config(settings: Settings):
    from alembic.config import Config

    from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir

    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    return config


def _sqlite_path(settings: Settings) -> Path:
    from sqlalchemy.engine import make_url

    database = make_url(settings.database_url).database
    assert database is not None
    return Path(database)


def _execute(settings: Settings, *statements: str) -> None:
    """Run DDL/DML without the application engine.

    ``make_engine`` issues ``PRAGMA journal_mode=WAL`` on connect, so building a
    fixture with it converts the file before the baseline fingerprint is taken
    and hides exactly the mutation these tests exist to detect.
    """
    from sqlalchemy import create_engine

    engine = create_engine(settings.database_url, future=True)
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def _clone(templates, settings: Settings, name: str) -> Path:
    """Start from a byte copy of a migrated template instead of upgrading."""
    templates.clone(name, _sqlite_path(settings))
    return _sqlite_path(settings)


def _manually_stamp_0001_as_current_0002(templates, settings: Settings) -> Path:
    _clone(templates, settings, "base_0001")
    _execute(settings, "UPDATE alembic_version SET version_num = 'efcbcf724889'")
    return _sqlite_path(settings)


_MIGRATED_DAMAGE_STATEMENTS: dict[str, tuple[str, ...]] = {
    "corrupt_alembic_version": (
        "UPDATE alembic_version SET version_num = 'not a revision'",
    ),
    "retired_revision": (
        "UPDATE alembic_version SET version_num = '0007_retired_revision'",
    ),
    "unstamped_final_schema": ("DELETE FROM alembic_version",),
    "dropped_alembic_version": ("DROP TABLE alembic_version",),
    # ``workspace_job_hint`` belongs to the final 0002 marker group, so dropping
    # it leaves that group "partial" instead of "complete".
    "partial_schema": (
        "DROP TABLE workspace_job_hint",
        "DROP TABLE alembic_version",
    ),
}


def _damage_database(templates, settings: Settings, damage: str) -> Path:
    """Build one unidentifiable database, never WAL-converting the file."""
    if damage == "empty_file":
        _sqlite_path(settings).touch()
        return _sqlite_path(settings)
    if damage == "foreign_user_table":
        _execute(
            settings,
            "CREATE TABLE customer_invoices (id INTEGER PRIMARY KEY, amount TEXT)",
            "INSERT INTO customer_invoices (id, amount) VALUES (1, 'important')",
        )
        return _sqlite_path(settings)
    if damage == "unreadable_file":
        database = _sqlite_path(settings)
        database.write_bytes(b"this is not a sqlite database" * 100)
        return database

    _clone(templates, settings, "head")
    _execute(settings, *_MIGRATED_DAMAGE_STATEMENTS[damage])
    return _sqlite_path(settings)


def _file_fingerprint(database: Path):
    """Main database plus its ``-wal``/``-shm`` sidecars.

    Hashing only the main file cannot see a WAL conversion whose evidence lands
    in the sidecars, so every byte SQLite may have written is covered here.
    """
    import hashlib

    def digest(path: Path) -> str | None:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

    return (
        tuple(
            (name, digest(database.parent / name))
            for name in (
                database.name,
                f"{database.name}-wal",
                f"{database.name}-shm",
            )
        ),
        database.stat().st_mtime_ns,
        sorted(path.name for path in database.parent.iterdir()),
    )


def _journal_mode(database: Path) -> str:
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        return connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()


def _record_db_version(settings: Settings, value: str) -> None:
    """Record ``dtxwiki_version`` without the application (WAL) engine."""
    from sqlalchemy import create_engine

    engine = create_engine(settings.database_url, future=True)
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


def _table_names(database: Path) -> tuple[str, ...]:
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    return tuple(row[0] for row in rows)


@pytest.fixture(scope="session")
def sqlite_templates(tmp_path_factory):
    return shared_templates(tmp_path_factory)


@pytest.fixture
def sqlite_settings(tmp_path):
    db = tmp_path / "test.sqlite"
    return Settings(database_url=f"sqlite+pysqlite:///{db}", auto_bootstrap=False)


def test_match_skips_alembic(sqlite_settings, sqlite_templates):
    """DB version and Alembic revision both current -> alembic upgrade not called."""
    from backend.app.bootstrap import ensure_app_version_synced
    from backend.app.db import make_session_factory_from_settings
    from backend.models.app_metadata import AppMetadata

    _clone(sqlite_templates, sqlite_settings, "head")

    # Insert matching version row
    factory = make_session_factory_from_settings(sqlite_settings)
    with factory.begin() as session:
        session.merge(
            AppMetadata(key="dtxwiki_version", value=sqlite_settings.app_version)
        )

    with mock.patch("backend.app.bootstrap.run_alembic_upgrade") as mock_alembic:
        ensure_app_version_synced(sqlite_settings)
    mock_alembic.assert_not_called()


def test_removed_revision_refuses_automatic_upgrade(sqlite_settings, sqlite_templates):
    """A revision outside the consolidated graph requires operator conversion."""
    from backend.app.bootstrap import ensure_app_version_synced
    from backend.app.db import make_engine, make_session_factory_from_settings
    from backend.models.app_metadata import AppMetadata

    _clone(sqlite_templates, sqlite_settings, "head")

    factory = make_session_factory_from_settings(sqlite_settings)
    with factory.begin() as session:
        session.merge(
            AppMetadata(key="dtxwiki_version", value=sqlite_settings.app_version)
        )

    engine = make_engine(sqlite_settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": "0000_pre_squash_revision"},
        )

    with mock.patch("backend.app.bootstrap.run_alembic_upgrade") as mock_alembic:
        with pytest.raises(RuntimeError, match="unknown_revision"):
            ensure_app_version_synced(sqlite_settings)
    mock_alembic.assert_not_called()


def test_old_schema_stamped_current_is_drift_and_bootstrap_fails_nonmutating(
    sqlite_settings,
    sqlite_templates,
):
    from backend.app.bootstrap import ensure_app_version_synced, migration_status

    database = _manually_stamp_0001_as_current_0002(sqlite_templates, sqlite_settings)
    _record_db_version(sqlite_settings, sqlite_settings.app_version)
    before_journal = _journal_mode(database)
    before = _file_fingerprint(database)

    assert migration_status(sqlite_settings).status == "drift"
    with mock.patch("backend.app.bootstrap.run_alembic_upgrade") as mock_alembic:
        with pytest.raises(RuntimeError, match="stamp_marker_conflict"):
            ensure_app_version_synced(sqlite_settings)

    mock_alembic.assert_not_called()
    assert _file_fingerprint(database) == before
    assert _journal_mode(database) == before_journal == "delete"


def test_direct_alembic_upgrade_rejects_old_schema_stamped_current(
    sqlite_settings,
    sqlite_templates,
):
    from alembic import command

    database = _manually_stamp_0001_as_current_0002(sqlite_templates, sqlite_settings)
    before = _file_fingerprint(database)

    with pytest.raises(RuntimeError, match="stamp_marker_conflict"):
        command.upgrade(_alembic_config(sqlite_settings), "head")

    assert _file_fingerprint(database) == before


@pytest.mark.parametrize("target", ("efcbcf724889", "head", "heads"))
def test_direct_alembic_stamp_cannot_launder_0001_as_current(
    sqlite_settings,
    sqlite_templates,
    target,
):
    from alembic import command

    from backend.app.bootstrap import current_db_alembic_revision

    _clone(sqlite_templates, sqlite_settings, "base_0001")

    with pytest.raises(RuntimeError, match="stamp_marker_conflict"):
        command.stamp(_alembic_config(sqlite_settings), target)

    assert current_db_alembic_revision(sqlite_settings) == "bd62193d5cf6"


def test_direct_alembic_stamp_cannot_relabel_final_schema_as_0001(
    sqlite_settings,
    sqlite_templates,
):
    from alembic import command

    from backend.app.bootstrap import current_db_alembic_revision

    database = _clone(sqlite_templates, sqlite_settings, "head")
    before = _file_fingerprint(database)

    with pytest.raises(RuntimeError, match="stamp_marker_conflict"):
        command.stamp(_alembic_config(sqlite_settings), "bd62193d5cf6")

    assert current_db_alembic_revision(sqlite_settings) == "efcbcf724889"
    assert _file_fingerprint(database) == before


def test_alembic_guard_allows_verified_noop_nonbase_stamps(
    sqlite_settings, sqlite_templates, tmp_path
):
    from alembic import command

    from backend.app.bootstrap import current_db_alembic_revision

    _clone(sqlite_templates, sqlite_settings, "base_0001")
    command.stamp(_alembic_config(sqlite_settings), "bd62193d5cf6")
    assert current_db_alembic_revision(sqlite_settings) == "bd62193d5cf6"

    # The no-op head stamp needs a head schema; 0001 -> head runs for real in the
    # initial-release round trip, so this half starts from a head copy.
    head_settings = Settings(
        database_url=sqlite_templates.clone("head", tmp_path / "head.sqlite"),
        auto_bootstrap=False,
    )
    command.stamp(_alembic_config(head_settings), "head")
    assert current_db_alembic_revision(head_settings) == "efcbcf724889"


def test_alembic_guard_allows_downgrade_base(sqlite_settings, sqlite_templates):
    from alembic import command

    from backend.app.bootstrap import current_db_alembic_revision

    _clone(sqlite_templates, sqlite_settings, "base_0001")
    command.downgrade(_alembic_config(sqlite_settings), "base")

    assert current_db_alembic_revision(sqlite_settings) is None


def test_alembic_guard_allows_recovery_unstamp_to_base(
    sqlite_settings, sqlite_templates
):
    from alembic import command

    from backend.app.bootstrap import current_db_alembic_revision

    _clone(sqlite_templates, sqlite_settings, "base_0001")
    command.stamp(_alembic_config(sqlite_settings), "base")

    assert current_db_alembic_revision(sqlite_settings) is None


@pytest.mark.parametrize(
    ("damage", "consistency"),
    (
        ("corrupt_alembic_version", "invalid_alembic_revision"),
        ("retired_revision", "unknown_revision"),
        ("unstamped_final_schema", "stamp_marker_conflict"),
        ("dropped_alembic_version", "stamp_marker_conflict"),
        ("partial_schema", "partial_schema"),
        ("foreign_user_table", "unversioned_populated"),
    ),
)
def test_direct_alembic_upgrade_refuses_unidentifiable_start_state(
    sqlite_settings,
    sqlite_templates,
    damage,
    consistency,
):
    """A mutating upgrade is refused before Alembic resolves any revision.

    Without the start-state preflight none of these reach a refusal: a corrupt
    or retired stamp fails inside Alembic with ``CommandError: Can't locate
    revision identified by ...``, an unstamped final schema replays 0001 onto
    tables that already exist, and a populated but unstamped foreign database
    is migrated in place as if it were a fresh install.
    """
    from alembic import command

    from backend.services.ops.migration_preflight import PREFLIGHT_REFUSAL_TOKEN

    database = _damage_database(sqlite_templates, sqlite_settings, damage)
    before_journal = _journal_mode(database)
    before = _file_fingerprint(database)

    with pytest.raises(RuntimeError, match=PREFLIGHT_REFUSAL_TOKEN) as raised:
        command.upgrade(_alembic_config(sqlite_settings), "head")

    assert consistency in str(raised.value)
    assert _file_fingerprint(database) == before
    assert _journal_mode(database) == before_journal == "delete"


@pytest.mark.parametrize(
    ("damage", "consistency"),
    (
        ("unstamped_final_schema", "stamp_marker_conflict"),
        ("dropped_alembic_version", "stamp_marker_conflict"),
        ("partial_schema", "partial_schema"),
        ("corrupt_alembic_version", "invalid_alembic_revision"),
        ("foreign_user_table", "unversioned_populated"),
        ("unreadable_file", "probe_error"),
    ),
)
def test_bootstrap_refuses_blocked_preflight_without_mutation(
    sqlite_settings,
    sqlite_templates,
    damage,
    consistency,
):
    """An absent or unreadable stamp over data is corruption, not a fresh DB."""
    from backend.app.bootstrap import ensure_app_version_synced
    from backend.services.ops.migration_preflight import PREFLIGHT_REFUSAL_TOKEN

    database = _damage_database(sqlite_templates, sqlite_settings, damage)
    before_journal = None if damage == "unreadable_file" else _journal_mode(database)
    before = _file_fingerprint(database)

    with mock.patch("backend.app.bootstrap.run_alembic_upgrade") as mock_alembic:
        with pytest.raises(RuntimeError, match=consistency) as raised:
            ensure_app_version_synced(sqlite_settings)

    assert PREFLIGHT_REFUSAL_TOKEN in str(raised.value)
    mock_alembic.assert_not_called()
    assert _file_fingerprint(database) == before
    if before_journal is not None:
        assert _journal_mode(database) == before_journal == "delete"


def test_repeated_bootstrap_on_damaged_database_never_mutates_it(
    sqlite_settings, sqlite_templates
):
    """Every restart must refuse identically, not retry the failed migration.

    A dtxWiki schema whose ``alembic_version`` table was dropped (an interrupted
    attempt, or a partial backup restore) used to make Alembic replay 0002 and
    leave a fresh empty ``alembic_version`` table behind on every single run,
    because SQLite DDL is not transactional.
    """
    from backend.app.bootstrap import ensure_app_version_synced

    database = _damage_database(
        sqlite_templates, sqlite_settings, "dropped_alembic_version"
    )
    before_journal = _journal_mode(database)
    before_tables = _table_names(database)
    before = _file_fingerprint(database)
    assert "alembic_version" not in before_tables

    for _ in range(3):
        with pytest.raises(RuntimeError, match="stamp_marker_conflict"):
            ensure_app_version_synced(sqlite_settings)
        assert _table_names(database) == before_tables
        assert _file_fingerprint(database) == before
        assert _journal_mode(database) == before_journal == "delete"


def test_migration_status_reports_drift_for_populated_unversioned_database(
    sqlite_settings,
    sqlite_templates,
):
    """``dtxwiki info`` must not adopt — or open — an unidentified database."""
    from backend.app.bootstrap import migration_status

    database = _damage_database(sqlite_templates, sqlite_settings, "foreign_user_table")
    before_journal = _journal_mode(database)
    before = _file_fingerprint(database)

    status = migration_status(sqlite_settings)

    assert status.status == "drift"
    assert status.alembic_db is None
    assert status.db_version is None
    assert _file_fingerprint(database) == before
    assert _journal_mode(database) == before_journal == "delete"


def test_bootstrap_migrates_an_existing_empty_database_file(
    sqlite_settings, sqlite_templates
):
    """A zero-table file is proven empty and must still bootstrap normally."""
    from backend.app.bootstrap import (
        current_db_alembic_revision,
        ensure_app_version_synced,
    )

    _damage_database(sqlite_templates, sqlite_settings, "empty_file")

    ensure_app_version_synced(sqlite_settings)

    assert current_db_alembic_revision(sqlite_settings) == _current_alembic_head()


def test_source_lock_probe_leaves_a_rollback_journal_database_unchanged(
    sqlite_settings,
    sqlite_templates,
):
    """The migrate-data source lock probe must not convert the source file."""
    from backend.scripts.migrate_data import verify_source_writable_lock

    database = _damage_database(sqlite_templates, sqlite_settings, "foreign_user_table")
    before_journal = _journal_mode(database)
    before = _file_fingerprint(database)

    verify_source_writable_lock(sqlite_settings.database_url)

    assert _file_fingerprint(database) == before
    assert _journal_mode(database) == before_journal == "delete"


@pytest.mark.parametrize(
    ("state", "verdict", "consistency"),
    (
        # An absent file is the only state that is fresh without being read.
        # Emptiness — not the mere absence of a stamp — is what makes a
        # database safe to migrate in place.
        ("absent", "fresh", "database_absent"),
        ("empty_file", "fresh", "proven_empty"),
        ("head", "ready", "consistent"),
        ("laundered", "blocked", "stamp_marker_conflict"),
        ("retired_revision", "blocked", "unknown_revision"),
        ("foreign_user_table", "blocked", "unversioned_populated"),
        ("partial_schema", "blocked", "partial_schema"),
        ("corrupt_alembic_version", "blocked", "invalid_alembic_revision"),
        ("unreadable_file", "blocked", "probe_error"),
    ),
)
def test_migration_preflight_reports_tri_state_verdicts(
    sqlite_settings,
    sqlite_templates,
    state,
    verdict,
    consistency,
):
    from backend.services.ops.migration_preflight import (
        evaluate_migration_preflight,
    )

    if state == "head":
        _clone(sqlite_templates, sqlite_settings, "head")
    elif state == "laundered":
        _manually_stamp_0001_as_current_0002(sqlite_templates, sqlite_settings)
    elif state != "absent":
        _damage_database(sqlite_templates, sqlite_settings, state)

    preflight = evaluate_migration_preflight(sqlite_settings.database_url)

    assert preflight.verdict == verdict
    assert preflight.consistency == consistency
    assert preflight.blocked is (verdict == "blocked")


def test_migration_preflight_never_creates_the_database_it_inspects(
    sqlite_settings,
):
    """An absent database is classified without opening — and so creating — it."""
    from backend.services.ops.migration_preflight import (
        evaluate_migration_preflight,
    )

    database = _sqlite_path(sqlite_settings)

    preflight = evaluate_migration_preflight(sqlite_settings.database_url)

    assert preflight.verdict == "fresh"
    assert not database.exists()


def test_migration_preflight_leaves_a_rollback_journal_database_unchanged(
    sqlite_settings,
):
    """Preflight must not apply the application engine's WAL configuration."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from backend.services.ops.migration_preflight import (
        evaluate_migration_preflight,
    )

    engine = create_engine(sqlite_settings.database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE probe_marker (id INTEGER)"))
    finally:
        engine.dispose()
    database_name = make_url(sqlite_settings.database_url).database
    assert database_name is not None
    database = Path(database_name)
    before = _file_fingerprint(database)

    evaluate_migration_preflight(sqlite_settings.database_url)

    assert _file_fingerprint(database) == before


def test_mismatch_runs_alembic_and_updates_row(sqlite_settings, sqlite_templates):
    """DB version absent -> alembic upgrade called + row inserted."""
    from backend.app.bootstrap import ensure_app_version_synced
    from backend.app.db import make_session_factory_from_settings
    from backend.models.app_metadata import AppMetadata

    _clone(sqlite_templates, sqlite_settings, "head")

    with mock.patch("backend.app.bootstrap.run_alembic_upgrade") as mock_alembic:
        ensure_app_version_synced(sqlite_settings)
    mock_alembic.assert_called_once()

    # Row should be inserted
    factory = make_session_factory_from_settings(sqlite_settings)
    with factory.begin() as session:
        row = session.get(AppMetadata, "dtxwiki_version")
    assert row is not None
    assert row.value == sqlite_settings.app_version


@pytest.fixture
def served_app(tmp_path_factory, monkeypatch):
    """A stand-in served app whose runtime lock lands in its own data dir.

    Not under ``tmp_path``: the fingerprints below cover the database
    directory's listing, and the lock file must not show up in it.
    """

    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path_factory.mktemp("data-dir")))
    return SimpleNamespace(state=SimpleNamespace())


def test_bootstrap_auto_false_refuses_a_fresh_database_without_creating_it(
    sqlite_settings, served_app
):
    """C9: with auto_bootstrap=false, serve never creates the schema itself."""
    from backend.app.bootstrap import DatabaseNotCurrentError, bootstrap

    with mock.patch("backend.app.bootstrap.run_alembic_upgrade") as mock_alembic:
        with pytest.raises(DatabaseNotCurrentError, match="'dtxwiki bootstrap'"):
            bootstrap(sqlite_settings, served_app)

    mock_alembic.assert_not_called()
    assert not _sqlite_path(sqlite_settings).exists()
    assert not hasattr(served_app.state, "app_version_synced")


def test_bootstrap_auto_false_refuses_a_database_that_needs_migration(
    sqlite_settings, sqlite_templates, served_app
):
    """C9: an older schema is refused byte-for-byte untouched, never migrated."""
    from backend.app.bootstrap import DatabaseNotCurrentError, bootstrap

    _clone(sqlite_templates, sqlite_settings, "base_0001")
    _record_db_version(sqlite_settings, "0.0.1")
    database = _sqlite_path(sqlite_settings)
    before = _file_fingerprint(database)

    with mock.patch("backend.app.bootstrap.run_alembic_upgrade") as mock_alembic:
        with pytest.raises(DatabaseNotCurrentError) as raised:
            bootstrap(sqlite_settings, served_app)

    mock_alembic.assert_not_called()
    message = str(raised.value)
    assert "db_revision=bd62193d5cf6" in message
    assert "Back up the database, then run 'dtxwiki bootstrap'" in message
    assert _file_fingerprint(database) == before
    assert not hasattr(served_app.state, "app_version_synced")


def test_bootstrap_auto_false_refuses_an_unsynced_version_marker(
    sqlite_settings, sqlite_templates, served_app
):
    """C9: a head schema whose marker names another release is not synced."""
    from backend.app.bootstrap import DatabaseNotCurrentError, bootstrap

    _clone(sqlite_templates, sqlite_settings, "head")
    _record_db_version(sqlite_settings, "0.0.1")

    with pytest.raises(DatabaseNotCurrentError, match="db_version=0.0.1"):
        bootstrap(sqlite_settings, served_app)


def test_bootstrap_auto_false_serves_a_database_bootstrapped_by_the_operator(
    sqlite_settings, served_app
):
    """The refusal's own remedy works: `dtxwiki bootstrap`, then serve."""
    from backend.app.bootstrap import DatabaseNotCurrentError, bootstrap

    with pytest.raises(DatabaseNotCurrentError):
        bootstrap(sqlite_settings, served_app)

    # What `dtxwiki bootstrap` does: the same settings with auto_bootstrap on.
    bootstrap(sqlite_settings.model_copy(update={"auto_bootstrap": True}))
    database = _sqlite_path(sqlite_settings)
    before = _file_fingerprint(database)

    with mock.patch("backend.app.bootstrap.run_alembic_upgrade") as mock_alembic:
        bootstrap(sqlite_settings, served_app)

    mock_alembic.assert_not_called()
    assert served_app.state.app_version_synced is True
    assert _file_fingerprint(database) == before


def _stamp_future_release(templates, settings: Settings, *, version: str) -> Path:
    """A database a newer dtxWiki migrated: unknown head, newer marker."""
    _clone(templates, settings, "head")
    _execute(settings, "UPDATE alembic_version SET version_num = '0003_future'")
    _record_db_version(settings, version)
    return _sqlite_path(settings)


def test_unknown_revision_from_a_newer_release_gets_the_downgrade_message(
    sqlite_settings,
    sqlite_templates,
):
    """d-1: a newer release's database is a downgrade, not damage to rebuild."""
    from backend.app.bootstrap import ensure_app_version_synced

    database = _stamp_future_release(
        sqlite_templates, sqlite_settings, version="99.0.0"
    )
    before = _file_fingerprint(database)

    with mock.patch("backend.app.bootstrap.run_alembic_upgrade") as mock_alembic:
        with pytest.raises(RuntimeError) as raised:
            ensure_app_version_synced(sqlite_settings)

    mock_alembic.assert_not_called()
    message = str(raised.value)
    assert f"DB version 99.0.0 > code version {sqlite_settings.app_version}" in (
        message
    )
    assert "Roll the dtxWiki binary forward" in message
    assert "rebuild" not in message
    assert _file_fingerprint(database) == before


def test_unknown_revision_without_a_newer_marker_keeps_the_rebuild_refusal(
    sqlite_settings,
    sqlite_templates,
):
    from backend.app.bootstrap import ensure_app_version_synced
    from backend.services.ops.migration_preflight import PREFLIGHT_REFUSAL_TOKEN

    _stamp_future_release(
        sqlite_templates, sqlite_settings, version=sqlite_settings.app_version
    )

    with pytest.raises(RuntimeError) as raised:
        ensure_app_version_synced(sqlite_settings)

    assert PREFLIGHT_REFUSAL_TOKEN in str(raised.value)
    assert "unknown_revision" in str(raised.value)


def test_info_shows_the_newer_version_of_an_unknown_revision_database(
    sqlite_settings, sqlite_templates, monkeypatch
):
    """d-1: `dtxwiki info` reports version_db even for a refused database."""
    from typer.testing import CliRunner

    from backend.cli.main import app

    database = _stamp_future_release(
        sqlite_templates, sqlite_settings, version="99.0.0"
    )
    before = _file_fingerprint(database)

    monkeypatch.setenv("DTXWIKI_DATABASE_URL", sqlite_settings.database_url)
    result = CliRunner().invoke(app, ["info", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["version_db"] == "99.0.0"
    assert payload["alembic_db"] == "0003_future"
    assert payload["migration_status"] == "downversion"
    assert _file_fingerprint(database) == before


def test_downversion_refuses_startup(sqlite_settings, sqlite_templates):
    """DB version > code version -> RuntimeError raised."""
    from backend.app.bootstrap import ensure_app_version_synced
    from backend.app.db import make_session_factory_from_settings
    from backend.models.app_metadata import AppMetadata

    _clone(sqlite_templates, sqlite_settings, "head")

    factory = make_session_factory_from_settings(sqlite_settings)
    with factory.begin() as session:
        session.merge(AppMetadata(key="dtxwiki_version", value="99.0.0"))

    with pytest.raises(RuntimeError, match="DB version.*>.*code version"):
        ensure_app_version_synced(sqlite_settings)


def test_unparseable_code_version_with_db_version_refuses_startup(
    sqlite_settings, sqlite_templates
):
    """#1: recorded DB version + non-PEP440 code version must refuse startup.

    is_newer() treats an unparseable version as "not newer", which would let an
    empty/garbage DTXWIKI_APP_VERSION slip past the downversion guard and migrate
    a DB recorded as newer. The explicit can't-prove-not-older branch fires."""
    from backend.app.bootstrap import ensure_app_version_synced
    from backend.app.db import make_session_factory_from_settings
    from backend.models.app_metadata import AppMetadata

    _clone(sqlite_templates, sqlite_settings, "head")
    factory = make_session_factory_from_settings(sqlite_settings)
    with factory.begin() as session:
        session.merge(AppMetadata(key="dtxwiki_version", value="1.5.0"))

    nightly = sqlite_settings.model_copy(update={"app_version": "nightly"})
    with pytest.raises(RuntimeError, match="unparseable"):
        ensure_app_version_synced(nightly)


def test_info_outputs_three_version_lines(sqlite_settings, monkeypatch, tmp_path):
    """dtxwiki info outputs version_code, version_db, migration_status keys."""
    from typer.testing import CliRunner

    from backend.cli.main import app

    monkeypatch.setenv("DTXWIKI_DATABASE_URL", sqlite_settings.database_url)
    runner = CliRunner()
    result = runner.invoke(app, ["info"])
    assert "version_code" in result.output
    assert "version_db" in result.output
    assert "migration_status" in result.output


def test_info_reports_upgrade_pending_for_lower_db_version(
    sqlite_settings,
    sqlite_templates,
    monkeypatch,
):
    from typer.testing import CliRunner

    from backend.app.db import make_session_factory_from_settings
    from backend.cli.main import app
    from backend.models.app_metadata import AppMetadata

    _clone(sqlite_templates, sqlite_settings, "head")
    factory = make_session_factory_from_settings(sqlite_settings)
    with factory.begin() as session:
        session.merge(AppMetadata(key="dtxwiki_version", value="0.0.9"))

    monkeypatch.setenv("DTXWIKI_DATABASE_URL", sqlite_settings.database_url)
    result = CliRunner().invoke(app, ["info", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["migration_status"] == "upgrade_pending"


def test_info_reports_drift_for_removed_alembic_revision(
    sqlite_settings,
    sqlite_templates,
    monkeypatch,
):
    from typer.testing import CliRunner

    from backend.app.db import make_engine, make_session_factory_from_settings
    from backend.cli.main import app
    from backend.models.app_metadata import AppMetadata

    _clone(sqlite_templates, sqlite_settings, "head")
    factory = make_session_factory_from_settings(sqlite_settings)
    with factory.begin() as session:
        session.merge(
            AppMetadata(key="dtxwiki_version", value=sqlite_settings.app_version)
        )

    engine = make_engine(sqlite_settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": "0000_pre_squash_revision"},
        )

    monkeypatch.setenv("DTXWIKI_DATABASE_URL", sqlite_settings.database_url)
    result = CliRunner().invoke(app, ["info", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["alembic_db"] == "0000_pre_squash_revision"
    assert payload["alembic_head"] == _current_alembic_head()
    assert payload["migration_status"] == "drift"


def test_info_reports_downversion_for_higher_db_version(
    sqlite_settings, sqlite_templates, monkeypatch
):
    from typer.testing import CliRunner

    from backend.app.db import make_session_factory_from_settings
    from backend.cli.main import app
    from backend.models.app_metadata import AppMetadata

    _clone(sqlite_templates, sqlite_settings, "head")
    factory = make_session_factory_from_settings(sqlite_settings)
    with factory.begin() as session:
        session.merge(AppMetadata(key="dtxwiki_version", value="99.0.0"))

    monkeypatch.setenv("DTXWIKI_DATABASE_URL", sqlite_settings.database_url)
    result = CliRunner().invoke(app, ["info", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["migration_status"] == "downversion"
