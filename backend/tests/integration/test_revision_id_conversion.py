"""ID conversion preserves existing data and refuses unprovable schemas."""

import sqlite3

import pytest
from typer.testing import CliRunner

from backend.cli.main import app
from backend.services.ops import revision_rename
from backend.services.ops.revision_rename import (
    LEGACY_REVISION_ALIASES,
    RevisionRenameError,
    rename_database_revisions,
)
from backend.tests.utils.migrated_sqlite_templates import shared_templates


def _dump(path):
    with sqlite3.connect(path) as connection:
        return [
            line
            for line in connection.iterdump()
            if not line.startswith('INSERT INTO "alembic_version"')
        ]


def _stamp(path, *revisions):
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM alembic_version")
        connection.executemany(
            "INSERT INTO alembic_version VALUES (?)", [(value,) for value in revisions]
        )


@pytest.mark.parametrize(
    "template,legacy",
    [("base_0001", "0001_initial_release"), ("head", "0002_epic1")],
)
def test_conversion_preserves_schema_data_and_is_explicit(
    tmp_path, tmp_path_factory, monkeypatch, template, legacy
):
    path = tmp_path / "legacy.sqlite"
    url = shared_templates(tmp_path_factory).clone(template, path)
    _stamp(path, legacy)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO app_metadata (key, value) VALUES ('rename_probe', 'keep me')"
        )
    before = _dump(path)
    before_bytes = path.read_bytes()
    monkeypatch.setenv("DTXWIKI_DATABASE_URL", url)
    runner = CliRunner()
    preview = runner.invoke(app, ["db", "rename-revisions"])
    assert preview.exit_code == 0, preview.output
    canonical = LEGACY_REVISION_ALIASES[legacy]
    assert f"preview: {legacy} -> {canonical}" in preview.output
    assert path.read_bytes() == before_bytes

    applied = runner.invoke(app, ["db", "rename-revisions", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert f"applied: {legacy} -> {canonical}" in applied.output
    assert _dump(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall() == [(canonical,)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not rename_database_revisions(url, apply=True).changed


@pytest.mark.parametrize(
    "revisions",
    [
        ("0002_epic1",),
        ("0003_oauth_consent_lifetimes",),
        ("0001_initial_release", "0002_epic1"),
        ("0001_initial_release",),
    ],
)
def test_conversion_refuses_mismatched_retired_or_multiple_stamps(
    tmp_path, tmp_path_factory, revisions
):
    path = tmp_path / "blocked.sqlite"
    url = shared_templates(tmp_path_factory).clone("base_0001", path)
    _stamp(path, *revisions)
    if revisions == ("0001_initial_release",):
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE workspace DROP COLUMN goal")
    before = path.read_bytes()
    with pytest.raises(RevisionRenameError, match="revision_rename_refused"):
        rename_database_revisions(url, apply=True)
    assert path.read_bytes() == before


def test_conversion_rolls_back_if_result_verification_fails(
    tmp_path, tmp_path_factory, monkeypatch
):
    path = tmp_path / "rollback.sqlite"
    url = shared_templates(tmp_path_factory).clone("base_0001", path)
    _stamp(path, "0001_initial_release")
    inspect = revision_rename.inspect_revision_snapshot
    calls = 0

    def fail_after_update(connection):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("verification interrupted")
        return inspect(connection)

    monkeypatch.setattr(revision_rename, "inspect_revision_snapshot", fail_after_update)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="verification interrupted"):
        rename_database_revisions(url, apply=True)
    assert path.read_bytes() == before
