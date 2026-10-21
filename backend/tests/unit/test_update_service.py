"""Tests for the update/rollback orchestration (notify-only, anti-brick)."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.runtime_resources import RuntimeResources, acquire_paths
from backend.app.settings import Settings
from backend.services import backup_service
from backend.services import update_service as us
from backend.services.ops.maintenance_handoff import verify_finalizer_handoff
from backend.services.update_check_service import UpdateInfo


def _raise(exc):
    def _fn(*a, **k):
        raise exc

    return _fn


def _install(override):
    """A realistic install whose console script EXISTS (Path(sys.executable))
    so the #6 bin-None safe-exit stays inert on hosts without /opt/dtxwiki."""
    kind = "venv" if override in (None, "auto") else override
    services = us.install_method.SYSTEMD_SERVICES if kind == "systemd" else ()
    return us.install_method.InstallMethod(
        kind=kind,
        venv_path=None,
        python_path=Path(sys.executable),
        pip_argv=("py", "-m", "pip"),
        dtxwiki_bin=Path(sys.executable),
        services=services,
    )


def _settings(**overrides):
    base = {
        "app_version": "0.1.0",
        "database_url": "sqlite+pysqlite:///:memory:",
        "app_env": "development",
        "update_index_url": "https://idx/simple",
    }
    base.update(overrides)
    return Settings(**base)


def _patch_common(monkeypatch, run=lambda argv, echo, **_kw: None):
    monkeypatch.setattr(us, "_run_argv", run)
    monkeypatch.setattr(
        us.backup_service, "create_backup", lambda *a, **k: Path("/tmp/bk")
    )
    monkeypatch.setattr(us.backup_service, "prune_backups", lambda **k: [])
    monkeypatch.setattr(us.bootstrap, "current_db_alembic_revision", lambda s: "rev1")
    monkeypatch.setattr(us, "_installed_version", lambda install: "0.2.0")
    monkeypatch.setattr(
        us.install_method,
        "detect_install_method",
        lambda override=None: _install(override),
    )


def test_run_update_venv_happy(monkeypatch):
    calls = []
    _patch_common(monkeypatch, run=lambda argv, echo, **_kw: calls.append(argv))
    result = us.run_update(_settings(), method="venv", yes=True, echo=lambda *a: None)
    assert result.ok and result.exit_code == 0
    assert result.to_version == "0.2.0"
    assert any("install" in argv for argv in calls)  # pip install ran
    assert any("--finalize-bootstrap" in argv for argv in calls)  # migrations ran


def test_update_refuses_db_activity_from_another_data_root(monkeypatch, tmp_path):
    calls = []
    _patch_common(monkeypatch, run=lambda argv, echo, **kw: calls.append(argv))
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'db.sqlite'}")
    resources = RuntimeResources.for_settings(settings)
    with acquire_paths([resources.activity_paths[1]], "shared"):
        result = us.run_update(
            settings,
            method="venv",
            yes=True,
            data_dir=tmp_path / "other",
            echo=lambda *a: None,
        )
    assert result.exit_code == 2
    assert "dtxwiki-activity.lock" in result.message
    assert calls == []


def test_finalize_private_guard_handoff_is_subprocess_only(monkeypatch):
    calls = []
    settings = _settings()
    expected = RuntimeResources.for_settings(settings).activity_paths
    monkeypatch.delenv(us.HANDOFF_ENV, raising=False)

    def run(argv, echo, **kw):
        assert us.HANDOFF_ENV not in os.environ
        calls.append((argv, kw))
        if "--finalize-bootstrap" in argv:
            with monkeypatch.context() as child_environment:
                child_environment.setenv(us.HANDOFF_ENV, kw["env"][us.HANDOFF_ENV])
                assert verify_finalizer_handoff(expected)

    _patch_common(monkeypatch, run=run)
    result = us.run_update(settings, method="venv", yes=True, echo=lambda *a: None)
    assert result.ok
    install = next(kw for argv, kw in calls if "install" in argv)
    finalize = next(kw for argv, kw in calls if "--finalize-bootstrap" in argv)
    assert "env" not in install
    assert us.HANDOFF_ENV in finalize["env"]
    assert us.HANDOFF_ENV not in os.environ


def test_handoff_setup_failure_after_install_keeps_services_stopped(
    monkeypatch, tmp_path
):
    calls = []
    services = []
    _patch_common(monkeypatch, run=lambda argv, echo, **kw: calls.append(argv))
    monkeypatch.setattr(
        us.svc, "run_systemctl", lambda action, names: services.append(action)
    )
    monkeypatch.setattr(
        us,
        "finalizer_handoff",
        _raise(us.HandoffError("handoff listener unavailable")),
    )

    result = us.run_update(
        _settings(),
        method="systemd",
        yes=True,
        data_dir=tmp_path,
        echo=lambda *a: None,
    )

    assert result.exit_code == 3
    assert any("install" in argv for argv in calls)
    assert not any("--finalize-bootstrap" in argv for argv in calls)
    assert services == ["stop"]
    with us.runtime_lock.acquire_path(
        tmp_path / ".runtime.lock", "shared", strict=True
    ):
        pass


def test_run_update_dry_run_executes_nothing(monkeypatch):
    calls = []
    _patch_common(monkeypatch, run=lambda argv, echo, **_kw: calls.append(argv))
    result = us.run_update(
        _settings(), method="venv", dry_run=True, echo=lambda *a: None
    )
    assert result.ok
    assert calls == []


def test_run_update_systemd_stops_starts_healthchecks(monkeypatch):
    sysctl = []
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        us.svc, "run_systemctl", lambda action, services: sysctl.append(action)
    )
    monkeypatch.setattr(us.healthcheck, "poll_health_ready", lambda base, **k: True)
    result = us.run_update(
        _settings(), method="systemd", yes=True, echo=lambda *a: None
    )
    assert result.ok
    assert sysctl == ["stop", "start"]


def test_run_update_finalize_failure_is_exit3_notify_only(monkeypatch):
    def run(argv, echo, **_kw):
        if "--finalize-bootstrap" in argv:
            raise us.UpdateStepError("alembic boom")

    _patch_common(monkeypatch, run=run)
    result = us.run_update(_settings(), method="venv", yes=True, echo=lambda *a: None)
    assert not result.ok
    assert result.exit_code == 3  # post-migration -> manual intervention
    assert result.backup_dir == Path("/tmp/bk")


def test_run_update_health_failure_is_exit1(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(us.svc, "run_systemctl", lambda action, services: None)
    monkeypatch.setattr(us.healthcheck, "poll_health_ready", lambda base, **k: False)
    result = us.run_update(
        _settings(), method="systemd", yes=True, echo=lambda *a: None
    )
    assert not result.ok
    assert result.exit_code == 1


def test_run_update_docker_returns_host_recipe(monkeypatch):
    result = us.run_update(_settings(), method="docker", yes=True, echo=lambda *a: None)
    assert not result.ok
    assert result.exit_code == 2
    assert "docker" in result.message


def _npm_run(monkeypatch, operation, **kwargs):
    """Run update/rollback under the npm launcher; fail on any side effect."""

    def forbidden(*a, **k):
        raise AssertionError("the npm tier must only print instructions")

    _patch_common(monkeypatch, run=forbidden)
    monkeypatch.setattr(us.backup_service, "create_backup", forbidden)
    monkeypatch.setattr(us.backup_service, "restore_backup", forbidden)
    monkeypatch.setattr(us, "_take_runtime_lock", forbidden)
    monkeypatch.setenv("DTXWIKI_DATA_DIR", "/srv/dtxwiki-data")
    lines = []
    result = operation(_settings(), method="npm", yes=True, echo=lines.append, **kwargs)
    return result, "\n".join(lines)


@pytest.mark.parametrize(("version", "target"), [("0.2.0", "0.2.0"), (None, "latest")])
def test_run_update_npm_points_at_the_launcher(monkeypatch, version, target):
    result, text = _npm_run(monkeypatch, us.run_update, version=version)

    assert not result.ok
    assert result.exit_code == 2
    assert result.message.startswith("npm:")
    assert f"npx @dtxsuite/dtxwiki@{target}" in text
    assert f"npm install --global @dtxsuite/dtxwiki@{target}" in text
    assert "/srv/dtxwiki-data" in text
    assert text.index("back up") < text.index("npx ")


def test_run_rollback_npm_points_at_the_older_launcher(monkeypatch):
    result, text = _npm_run(monkeypatch, us.run_rollback)

    assert not result.ok
    assert result.exit_code == 2
    assert result.message.startswith("npm:")
    assert "restore the data directory" in text
    assert "/srv/dtxwiki-data" in text
    assert "npx @dtxsuite/dtxwiki@<version>" in text


def test_run_check_delegates(monkeypatch):
    monkeypatch.setattr(
        us.update_check_service,
        "check_for_update",
        lambda s, **k: UpdateInfo("0.1.0", "0.2.0", True, "stable", None),
    )
    info = us.run_check(_settings())
    assert info is not None and info.update_available


# --- rollback ------------------------------------------------------------


def _rollback_settings(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db = base / "dtxwiki.sqlite"
    sqlite3.connect(str(db)).close()
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("key", encoding="utf-8")
    settings = Settings(
        app_version="0.2.0",
        database_url=f"sqlite+pysqlite:///{db.as_posix()}",
        oauth_jwt_private_key_path=str(jwt),
        app_env="development",
    )
    return settings, base


def test_run_rollback_reinstalls_old_then_restores(monkeypatch, tmp_path):
    settings, base = _rollback_settings(tmp_path)
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    calls = []
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: calls.append(argv))

    lines = []
    result = us.run_rollback(
        settings,
        method="venv",
        yes=True,
        data_dir=base,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert result.ok
    assert result.to_version == "0.1.0"
    assert result.backup_dir == backup_dir
    # the OLD version is pinned + force-reinstalled (anti-brick: code down first)
    assert any("dtxwiki==0.1.0" in argv for argv in calls)
    assert any("--force-reinstall" in argv for argv in calls)
    # SQLite tier: the DB snapshot was restored and the message says so
    assert any(
        f"restored DB + secrets from {backup_dir.name}" in line for line in lines
    )


def test_run_rollback_no_backup_is_exit2(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    settings = Settings(
        app_version="0.1.0",
        database_url="sqlite+pysqlite:///:memory:",
        app_env="development",
    )
    result = us.run_rollback(
        settings, method="venv", yes=True, data_dir=base, echo=lambda *a: None
    )
    assert not result.ok
    assert result.exit_code == 2


def test_run_rollback_dry_run_executes_nothing(monkeypatch, tmp_path):
    settings, base = _rollback_settings(tmp_path)
    backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    calls = []
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: calls.append(argv))
    result = us.run_rollback(
        settings, method="venv", dry_run=True, data_dir=base, echo=lambda *a: None
    )
    assert result.ok
    assert calls == []


# --- hardening regressions (#30/#32/#36/#42/#38) -------------------------


def test_run_update_prerelease_channel_adds_pre(monkeypatch):
    lines = []
    _patch_common(monkeypatch)
    us.run_update(
        _settings(),
        method="venv",
        channel="prerelease",
        dry_run=True,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert any("--pre" in line for line in lines)


def test_run_update_missing_binary_is_exit3(monkeypatch):
    from backend.services.ops.install_method import InstallMethod

    _patch_common(monkeypatch)
    monkeypatch.setattr(
        us.install_method,
        "detect_install_method",
        lambda override=None: InstallMethod(
            "venv", None, None, ("py", "-m", "pip"), None, ()
        ),
    )
    result = us.run_update(_settings(), method="venv", yes=True, echo=lambda *a: None)
    assert not result.ok and result.exit_code == 3  # no success on an unmigrated DB


def test_run_update_systemd_finalize_failure_leaves_services_stopped(monkeypatch):
    sysctl = []

    def run(argv, echo, **_kw):
        if "--finalize-bootstrap" in argv:
            raise us.UpdateStepError("alembic boom")

    _patch_common(monkeypatch, run=run)
    monkeypatch.setattr(
        us.svc, "run_systemctl", lambda action, services: sysctl.append(action)
    )
    monkeypatch.setattr(us.healthcheck, "poll_health_ready", lambda base, **k: True)
    lines = []
    result = us.run_update(
        _settings(),
        method="systemd",
        yes=True,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert result.exit_code == 3
    assert sysctl == ["stop"]  # never restarted after a migration failure
    assert any("STOPPED" in line for line in lines)


def test_run_rollback_corrupt_manifest_is_exit2(monkeypatch, tmp_path):
    settings, base = _rollback_settings(tmp_path)
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    (backup_dir / "manifest.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: None)
    result = us.run_rollback(
        settings,
        method="venv",
        yes=True,
        data_dir=base,
        to_backup=str(backup_dir),
        echo=lambda *a: None,
    )
    assert not result.ok and result.exit_code == 2


def test_run_rollback_integrity_failure_aborts_before_reinstall(monkeypatch, tmp_path):
    settings, base = _rollback_settings(tmp_path)
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    (backup_dir / "dtxwiki.sqlite").write_bytes(b"corrupted")  # sha256 mismatch
    calls = []
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: calls.append(argv))
    result = us.run_rollback(
        settings,
        method="venv",
        yes=True,
        data_dir=base,
        to_backup=str(backup_dir),
        echo=lambda *a: None,
    )
    assert not result.ok and result.exit_code == 2
    assert calls == []  # aborted before touching code or DB


# --- Group A: failure-handling hardening (#4/#5/#7/#16) -------------------


def test_run_argv_timeout_becomes_update_step_error(monkeypatch):
    monkeypatch.setattr(
        us.subprocess,
        "run",
        _raise(subprocess.TimeoutExpired(cmd="pip", timeout=1)),
    )
    with pytest.raises(us.UpdateStepError, match="timed out"):
        us._run_argv(["pip", "install"], echo=lambda *a: None)


def test_run_argv_missing_executable_becomes_update_step_error(monkeypatch):
    monkeypatch.setattr(us.subprocess, "run", _raise(FileNotFoundError("no pipx")))
    with pytest.raises(us.UpdateStepError, match="could not run command"):
        us._run_argv(["pipx", "install"], echo=lambda *a: None)


def test_run_update_backup_failure_is_exit2_restarts_services(monkeypatch):
    sysctl = []
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        us.backup_service, "create_backup", _raise(OSError("disk full"))
    )
    monkeypatch.setattr(
        us.svc, "run_systemctl", lambda action, services: sysctl.append(action)
    )
    result = us.run_update(
        _settings(), method="systemd", yes=True, echo=lambda *a: None
    )
    assert not result.ok and result.exit_code == 2
    assert "backup failed" in result.message
    assert sysctl == ["stop", "start"]  # old code brought back up (no outage)


def test_run_rollback_restore_failure_is_exit3_leaves_services_stopped(
    monkeypatch, tmp_path
):
    # A restore I/O failure can leave a half-copied SQLite file (or the still-NEW
    # schema); starting the old code against it would crash-loop on the
    # downversion fail-safe — services must stay STOPPED, with an explicit note.
    settings, base = _rollback_settings(tmp_path)
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    sysctl = []
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: None)
    monkeypatch.setattr(
        us.svc, "run_systemctl", lambda action, services: sysctl.append(action)
    )
    monkeypatch.setattr(us.healthcheck, "poll_health_ready", lambda base, **k: True)
    monkeypatch.setattr(
        us.backup_service, "restore_backup", _raise(OSError("disk full"))
    )
    lines = []
    result = us.run_rollback(
        settings,
        method="systemd",
        yes=True,
        data_dir=base,
        to_backup=str(backup_dir),
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert not result.ok and result.exit_code == 3
    assert "restore" in result.message and "partially restored" in result.message
    assert sysctl == ["stop"]  # never restarted onto a half-restored DB
    assert any("STOPPED" in line for line in lines)
    # the exact re-rollback recovery command is still printed
    assert any(f"rollback --to-backup {backup_dir}" in line for line in lines)


def test_run_rollback_restore_value_error_is_handled(monkeypatch, tmp_path):
    # #9: a corrupt-manifest re-read inside restore raises ValueError (not
    # OSError); it must still route through _fail (services stopped), not
    # escape raw.
    settings, base = _rollback_settings(tmp_path)
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    sysctl = []
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: None)
    monkeypatch.setattr(
        us.svc, "run_systemctl", lambda action, services: sysctl.append(action)
    )
    monkeypatch.setattr(us.healthcheck, "poll_health_ready", lambda base, **k: True)
    monkeypatch.setattr(
        us.backup_service, "restore_backup", _raise(ValueError("corrupt manifest"))
    )
    result = us.run_rollback(
        settings,
        method="systemd",
        yes=True,
        data_dir=base,
        to_backup=str(backup_dir),
        echo=lambda *a: None,
    )
    assert not result.ok and result.exit_code == 3
    assert sysctl == ["stop"]


def test_run_update_finalize_step_runs_without_timeout(monkeypatch):
    # #7: the migration (finalize-bootstrap) step must NOT inherit the bounded
    # install timeout, so a long real migration is not killed mid-flight.
    calls = []
    _patch_common(monkeypatch, run=lambda argv, echo, **kw: calls.append((argv, kw)))
    us.run_update(_settings(), method="venv", yes=True, echo=lambda *a: None)
    finalize = next(c for c in calls if "--finalize-bootstrap" in c[0])
    assert finalize[1].get("timeout", "MISSING") is None
    install = next(c for c in calls if any("install" in a for a in c[0]))
    assert "timeout" not in install[1]  # network/install step keeps the cap


# --- Group C: env-file threading (#3) ------------------------------------


def test_effective_env_file_prefers_explicit_on_any_tier():
    assert us._effective_env_file(_install("systemd"), None) == us.PROD_ENV_FILE
    assert us._effective_env_file(_install("venv"), None) is None
    assert us._effective_env_file(_install("venv"), "/x/custom.env") == Path(
        "/x/custom.env"
    )


@pytest.mark.parametrize(
    ("method", "env_file", "expected"),
    [
        ("systemd", "/etc/dtxwiki/custom.env", Path("/etc/dtxwiki/custom.env")),
        ("venv", "/srv/custom.env", Path("/srv/custom.env")),
        ("systemd", None, us.PROD_ENV_FILE),
        ("venv", None, None),
    ],
)
def test_run_update_uses_the_same_env_file_for_backup_and_finalization(
    monkeypatch, method, env_file, expected
):
    captured = {}
    calls = []
    _patch_common(monkeypatch, run=lambda argv, echo, **kw: calls.append(argv))

    def fake_create_backup(*a, **k):
        captured.update(k)
        return Path("/tmp/bk")

    monkeypatch.setattr(us.backup_service, "create_backup", fake_create_backup)
    monkeypatch.setattr(us.svc, "run_systemctl", lambda action, services: None)
    monkeypatch.setattr(us.healthcheck, "poll_health_ready", lambda base, **k: True)
    result = us.run_update(
        _settings(),
        method=method,
        env_file=env_file,
        yes=True,
        echo=lambda *a: None,
    )
    assert result.ok
    assert captured["env_file"] == expected
    finalize = next(argv for argv in calls if "--finalize-bootstrap" in argv)
    if expected is None:
        assert "--env-file" not in finalize
    else:
        assert finalize[finalize.index("--env-file") + 1] == str(expected)


# --- Group G: --channel normalization (#15) ------------------------------


def test_run_update_uppercase_prerelease_channel_adds_pre(monkeypatch):
    lines = []
    _patch_common(monkeypatch)
    us.run_update(
        _settings(),
        method="venv",
        channel="PRERELEASE",  # case-insensitive: must still add --pre
        dry_run=True,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert any("--pre" in line for line in lines)


def test_settings_with_overrides_normalizes_channel():
    out = us._settings_with_overrides(_settings(), None, "PreRelease")
    assert out.update_channel == "prerelease"  # not filtered out by --check


# --- M-26: --version downgrade guard --------------------------------------


def test_run_update_refuses_downgrade_before_any_step(monkeypatch):
    # A pinned older version would downgrade the code and trip the bootstrap
    # downversion fail-safe with services stopped; refuse up front instead.
    calls = []
    sysctl = []
    _patch_common(monkeypatch, run=lambda argv, echo, **_kw: calls.append(argv))
    monkeypatch.setattr(
        us.svc, "run_systemctl", lambda action, services: sysctl.append(action)
    )
    lines = []
    result = us.run_update(
        _settings(app_version="0.2.0"),
        method="systemd",
        version="0.1.0",
        yes=True,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert not result.ok and result.exit_code == 2
    assert "rollback" in result.message
    assert any("cannot downgrade" in line for line in lines)
    assert any("dtxwiki rollback" in line for line in lines)
    assert calls == [] and sysctl == []  # refused before stopping anything


def test_run_update_refuses_downgrade_even_in_dry_run(monkeypatch):
    # The dry-run plan must not show an install the real run would refuse.
    calls = []
    _patch_common(monkeypatch, run=lambda argv, echo, **_kw: calls.append(argv))
    result = us.run_update(
        _settings(app_version="0.2.0"),
        method="venv",
        version="0.1.0",
        dry_run=True,
        echo=lambda *a: None,
    )
    assert not result.ok and result.exit_code == 2
    assert calls == []


def test_run_update_same_version_pin_is_allowed(monkeypatch):
    # Reinstalling the current version is not a downgrade (repair use case).
    _patch_common(monkeypatch)
    result = us.run_update(
        _settings(app_version="0.2.0"),
        method="venv",
        version="0.2.0",
        yes=True,
        echo=lambda *a: None,
    )
    assert result.ok


def test_run_update_unparseable_version_skips_downgrade_guard(monkeypatch):
    # "Can't tell" stays permissive: pip raises its own resolution error.
    _patch_common(monkeypatch)
    result = us.run_update(
        _settings(app_version="0.2.0"),
        method="venv",
        version="not-a-version",
        yes=True,
        echo=lambda *a: None,
    )
    assert result.ok


# --- H-09: non-SQLite DB has no snapshot — never claim one ----------------


_PG_URL = "postgresql+pg8000://user:pass@localhost/db"


def test_non_sqlite_database_detection():
    assert us._non_sqlite_database(_PG_URL) is True
    assert us._non_sqlite_database("sqlite+pysqlite:///x.sqlite") is False
    assert us._non_sqlite_database("sqlite+pysqlite:///:memory:") is False
    assert us._non_sqlite_database("not a url") is False


def test_run_update_non_sqlite_refuses_without_ack(monkeypatch):
    calls = []
    _patch_common(monkeypatch, run=lambda argv, echo, **_kw: calls.append(argv))
    lines = []
    result = us.run_update(
        _settings(database_url=_PG_URL),
        method="venv",
        yes=True,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert not result.ok and result.exit_code == 2
    assert "--no-db-backup" in result.message
    assert any("pg_dump" in line for line in lines)
    assert calls == []  # refused before any install/migration step


def test_run_update_non_sqlite_proceeds_with_ack_secrets_only(monkeypatch):
    backups = []
    _patch_common(monkeypatch)

    def fake_create_backup(*a, **k):
        backups.append(k)
        return Path("/tmp/bk")

    monkeypatch.setattr(us.backup_service, "create_backup", fake_create_backup)
    lines = []
    prompts = []

    def confirm(msg):
        prompts.append(msg)
        return True

    result = us.run_update(
        _settings(database_url=_PG_URL),
        method="venv",
        no_db_backup=True,
        confirm=confirm,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert result.ok
    assert backups  # the secrets-only backup is still taken
    assert any("WARNING" in line and "pg_dump" in line for line in lines)
    # the confirm prompt must not claim a full backup
    assert any("secrets-only" in p for p in prompts)
    assert not any("A backup will be taken first." in p for p in prompts)


def test_run_update_non_sqlite_dry_run_notes_secrets_only_backup(monkeypatch):
    lines = []
    _patch_common(monkeypatch)
    result = us.run_update(
        _settings(database_url=_PG_URL),
        method="venv",
        dry_run=True,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert result.ok  # dry-run changes nothing, so it is not gated on the ack
    assert any("DB NOT included" in line for line in lines)


def test_run_update_sqlite_confirm_message_unchanged(monkeypatch):
    _patch_common(monkeypatch)
    prompts = []

    def confirm(msg):
        prompts.append(msg)
        return True

    result = us.run_update(
        _settings(), method="venv", confirm=confirm, echo=lambda *a: None
    )
    assert result.ok
    assert any("A backup will be taken first." in p for p in prompts)


def _pg_rollback_settings(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    jwt = tmp_path / "jwt.pem"
    jwt.write_text("key", encoding="utf-8")
    settings = Settings(
        app_version="0.2.0",
        database_url=_PG_URL,
        oauth_jwt_private_key_path=str(jwt),
        app_env="development",
    )
    return settings, base


def test_run_rollback_non_sqlite_says_db_not_included(monkeypatch, tmp_path):
    settings, base = _pg_rollback_settings(tmp_path)
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: None)
    lines = []
    result = us.run_rollback(
        settings,
        method="venv",
        yes=True,
        data_dir=base,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert result.ok
    assert any(
        f"restored secrets (DB not included in backup) from {backup_dir.name}" in line
        for line in lines
    )
    assert not any("restored DB + secrets" in line for line in lines)
    assert any("pg_restore" in line for line in lines)  # up-front warning


def test_run_rollback_non_sqlite_dry_run_plan_omits_db_restore(monkeypatch, tmp_path):
    settings, base = _pg_rollback_settings(tmp_path)
    backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: None)
    lines = []
    result = us.run_rollback(
        settings,
        method="venv",
        dry_run=True,
        data_dir=base,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert result.ok
    assert any("no DB snapshot to restore" in line for line in lines)
    assert not any("restore DB + secrets" in line for line in lines)


def test_run_rollback_legacy_manifest_falls_back_to_snapshot_file(
    monkeypatch, tmp_path
):
    # Backups created before includes_db existed: presence of the snapshot file
    # decides the message, so a legacy SQLite backup still reports a DB restore.
    settings, base = _rollback_settings(tmp_path)
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    manifest = backup_service.read_manifest(backup_dir)
    del manifest["includes_db"]
    backup_service.write_manifest(backup_dir, manifest)
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: None)
    lines = []
    result = us.run_rollback(
        settings,
        method="venv",
        yes=True,
        data_dir=base,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert result.ok
    assert any(
        f"restored DB + secrets from {backup_dir.name}" in line for line in lines
    )


def test_run_rollback_failures_say_rollback_not_update(monkeypatch, tmp_path):
    # _fail must name the operation that actually ran: an operator who sees
    # 'ERROR: update failed' after `dtxwiki rollback` thinks an update happened.
    settings, base = _rollback_settings(tmp_path)
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: None)
    monkeypatch.setattr(
        us.backup_service, "restore_backup", _raise(OSError("disk full"))
    )
    lines = []
    result = us.run_rollback(
        settings,
        method="venv",
        yes=True,
        data_dir=base,
        to_backup=str(backup_dir),
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert not result.ok and result.exit_code == 3
    assert any("ERROR: rollback failed" in line for line in lines)
    assert not any("update failed" in line for line in lines)
    # the follow-up text must not claim a partially-UPDATED state either
    assert any("partially-rolled-back" in line for line in lines)
    assert not any("partially-updated" in line for line in lines)
    # the retry recovery command is still printed
    assert any(f"rollback --to-backup {backup_dir}" in line for line in lines)


def test_run_rollback_early_failure_says_rollback(monkeypatch, tmp_path):
    settings, base = _rollback_settings(tmp_path)
    backup_dir = backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    (backup_dir / "manifest.json").write_text("{not json", encoding="utf-8")
    lines = []
    result = us.run_rollback(
        settings,
        method="venv",
        yes=True,
        data_dir=base,
        to_backup=str(backup_dir),
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert not result.ok and result.exit_code == 2
    assert any("ERROR: rollback failed" in line for line in lines)
    assert not any("update failed" in line for line in lines)


def test_run_update_failures_still_say_update(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        us.backup_service, "create_backup", _raise(OSError("disk full"))
    )
    lines = []
    result = us.run_update(
        _settings(),
        method="venv",
        yes=True,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert not result.ok
    assert any("ERROR: update failed" in line for line in lines)


# --- credential redaction in echoes/plans/manifest seams -------------------


_CRED_INDEX = "https://user:t0ken@pypi.internal/simple"


def test_run_argv_echo_redacts_index_credentials(monkeypatch):
    monkeypatch.setattr(
        us.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    )
    lines = []
    us._run_argv(
        ["pip", "install", "--index-url", _CRED_INDEX],
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert any("https://user:****@pypi.internal/simple" in line for line in lines)
    assert not any("t0ken" in line for line in lines)


def test_run_argv_failure_message_redacts_index_credentials(monkeypatch):
    monkeypatch.setattr(
        us.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, stdout="", stderr=""),
    )
    with pytest.raises(us.UpdateStepError) as err:
        us._run_argv(
            ["pip", "install", "--index-url", _CRED_INDEX], echo=lambda *a: None
        )
    assert "t0ken" not in str(err.value)
    assert "user:****@pypi.internal" in str(err.value)


def test_run_update_dry_run_plan_redacts_index_url(monkeypatch):
    lines = []
    _patch_common(monkeypatch)
    result = us.run_update(
        _settings(update_index_url=_CRED_INDEX),
        method="venv",
        dry_run=True,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert result.ok
    assert any("user:****@pypi.internal" in line for line in lines)
    assert not any("t0ken" in line for line in lines)


def test_run_rollback_dry_run_plan_redacts_index_url(monkeypatch, tmp_path):
    settings, base = _rollback_settings(tmp_path)
    backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: None)
    lines = []
    result = us.run_rollback(
        settings,
        method="venv",
        index_url=_CRED_INDEX,
        dry_run=True,
        data_dir=base,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert result.ok
    assert any("user:****@pypi.internal" in line for line in lines)
    assert not any("t0ken" in line for line in lines)


def test_docker_instructions_redact_index_url():
    lines = []
    result = us.run_update(
        _settings(),
        method="docker",
        index_url=_CRED_INDEX,
        yes=True,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert not result.ok and result.exit_code == 2
    assert any(
        "PIP_INDEX_URL=https://user:****@pypi.internal/simple" in line for line in lines
    )
    assert not any("t0ken" in line for line in lines)


def test_run_rollback_sqlite_snapshot_onto_non_sqlite_url_not_claimed(
    monkeypatch, tmp_path
):
    # A SQLite-era backup rolled back under a PG database_url: restore_backup
    # skips the DB, so the message must not claim it was restored.
    settings, base = _rollback_settings(tmp_path)
    backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    pg_settings = settings.model_copy(update={"database_url": _PG_URL})
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: None)
    lines = []
    result = us.run_rollback(
        pg_settings,
        method="venv",
        yes=True,
        data_dir=base,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert result.ok
    assert any("DB snapshot NOT restored" in line for line in lines)
    assert not any("restored DB + secrets" in line for line in lines)


# --- runtime lock (C10) and the Docker recipe (M-18b) ----------------------


def _lock_is_free(data_dir: Path) -> bool:
    try:
        held = us.runtime_lock.acquire_shared(data_dir)
    except us.runtime_lock.RuntimeLockHeldError:
        return False
    assert held is not None
    held.release()
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="flock semantics")
def test_run_update_refuses_while_a_runtime_holds_the_lock(monkeypatch, tmp_path):
    """The pipx/venv tier: a `dtxwiki serve` left running blocks the update."""

    calls = []
    backups = []
    _patch_common(monkeypatch, run=lambda argv, echo, **_kw: calls.append(argv))
    monkeypatch.setattr(
        us.backup_service, "create_backup", lambda *a, **k: backups.append(k)
    )
    server = us.runtime_lock.acquire_shared(tmp_path)
    lines = []
    try:
        result = us.run_update(
            _settings(),
            method="venv",
            yes=True,
            data_dir=tmp_path,
            echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
        )
    finally:
        server.release()

    assert not result.ok and result.exit_code == 2
    assert "stop dtxWiki (server and worker) first" in result.message
    assert calls == [] and backups == []
    assert any("no backup was taken" in line for line in lines)


@pytest.mark.skipif(sys.platform == "win32", reason="flock semantics")
def test_run_update_holds_the_lock_until_the_services_restart(monkeypatch, tmp_path):
    observed: dict[str, bool] = {}

    def run(argv, echo, **_kw):
        if "--finalize-bootstrap" in argv:
            observed["finalize_lock_free"] = _lock_is_free(tmp_path)

    def systemctl(action, services):
        observed[f"{action}_lock_free"] = _lock_is_free(tmp_path)

    _patch_common(monkeypatch, run=run)
    monkeypatch.setattr(us.svc, "run_systemctl", systemctl)
    monkeypatch.setattr(us.healthcheck, "poll_health_ready", lambda base, **k: True)

    result = us.run_update(
        _settings(), method="systemd", yes=True, data_dir=tmp_path, echo=print
    )

    assert result.ok
    assert observed == {
        "stop_lock_free": True,
        "finalize_lock_free": False,
        "start_lock_free": True,
    }


@pytest.mark.skipif(sys.platform == "win32", reason="flock semantics")
def test_run_update_gives_the_lock_back_before_restarting_old_code(
    monkeypatch, tmp_path
):
    observed = []

    def run(argv, echo, **_kw):
        raise us.UpdateStepError("index unreachable")

    _patch_common(monkeypatch, run=run)
    monkeypatch.setattr(
        us.svc,
        "run_systemctl",
        lambda action, services: observed.append((action, _lock_is_free(tmp_path))),
    )

    result = us.run_update(
        _settings(), method="systemd", yes=True, data_dir=tmp_path, echo=print
    )

    assert result.exit_code == 2
    assert observed == [("stop", True), ("start", True)]


@pytest.mark.skipif(sys.platform == "win32", reason="flock semantics")
def test_run_rollback_refuses_while_a_runtime_holds_the_lock(monkeypatch, tmp_path):
    settings, base = _rollback_settings(tmp_path)
    backup_service.create_backup(settings, version="0.1.0", data_dir=base)
    calls = []
    monkeypatch.setattr(us, "_run_argv", lambda argv, echo, **_kw: calls.append(argv))
    restores = []
    monkeypatch.setattr(
        us.backup_service, "restore_backup", lambda *a, **k: restores.append(a)
    )
    worker = us.runtime_lock.acquire_shared(base)
    try:
        result = us.run_rollback(
            settings, method="venv", yes=True, data_dir=base, echo=print
        )
    finally:
        worker.release()

    assert not result.ok and result.exit_code == 2
    assert "stop dtxWiki (server and worker) first" in result.message
    assert calls == [] and restores == []


def _docker_lines(monkeypatch, *, image=None, version="0.2.0", index_url=None):
    if image is None:
        monkeypatch.delenv("DTXWIKI_IMAGE", raising=False)
    else:
        monkeypatch.setenv("DTXWIKI_IMAGE", image)
    lines = []
    result = us.run_update(
        _settings(update_index_url=None),
        method="docker",
        version=version,
        index_url=index_url,
        yes=True,
        echo=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    assert not result.ok and result.exit_code == 2
    return "\n".join(lines)


@pytest.mark.parametrize("image", [None, "dtxwiki:local", "dtxwiki:0.1.1"])
def test_docker_update_prints_the_local_build_flow(monkeypatch, image):
    """M-18b: the default stack builds dtxwiki:local, which no registry has."""

    text = _docker_lines(monkeypatch, image=image)

    assert "git fetch --tags && git checkout v0.2.0" in text
    assert "docker compose build" in text
    assert "docker compose up -d" in text
    assert "pull" not in text
    assert text.index("git checkout") < text.index("back up")
    assert text.index("back up") < text.index("docker compose build")


def test_docker_update_without_a_target_version_names_the_tag_to_choose(
    monkeypatch,
):
    text = _docker_lines(monkeypatch, version=None)

    assert "git fetch --tags && git checkout vX.Y.Z" in text


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        (
            "registry.example.com/dtxwiki:0.1.1",
            "DTXWIKI_IMAGE=registry.example.com/dtxwiki:0.2.0",
        ),
        ("localhost:5000/dtxwiki", "DTXWIKI_IMAGE=localhost:5000/dtxwiki:0.2.0"),
        (
            "ghcr.io/org/dtxwiki@sha256:" + "a" * 64,
            "DTXWIKI_IMAGE=ghcr.io/org/dtxwiki:0.2.0",
        ),
    ],
)
def test_docker_update_prints_the_pull_flow_only_for_a_registry_image(
    monkeypatch, image, expected
):
    text = _docker_lines(monkeypatch, image=image)

    assert expected in text
    assert "docker compose pull" in text
    assert "docker compose up -d" in text
    assert "git checkout" not in text
    assert "docker compose build" not in text
