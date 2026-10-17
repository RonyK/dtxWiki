"""CLI-level tests for `dtxwiki update` / `dtxwiki rollback` wrappers."""

import os
import subprocess

import pytest
import typer
from typer.testing import CliRunner

from backend.app.runtime_resources import RuntimeResources, acquire_paths
from backend.app.settings import Settings
from backend.cli.commands import update as cmd
from backend.cli.main import app
from backend.services.ops import runtime_lock
from backend.services.ops.install_method import InstallMethod

runner = CliRunner()


def test_old_updater_finalizer_adds_db_guard_without_retaking_parent_root(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(root))
    monkeypatch.setenv("DTXWIKI_DATABASE_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    monkeypatch.delenv(cmd.update_service.MAINTENANCE_DB_LOCK_PARENT_ENV, raising=False)
    monkeypatch.setattr(cmd, "_load_prod_env", lambda *a: None)
    resources = RuntimeResources.for_settings(Settings())
    db_path = resources.activity_paths[1]
    calls = []

    def bootstrap(settings):
        with pytest.raises(runtime_lock.RuntimeLockHeldError):
            runtime_lock.acquire_path(db_path, "shared", strict=True)
        calls.append(True)

    monkeypatch.setattr(cmd, "bootstrap_app", bootstrap)
    with runtime_lock.acquire_exclusive(root):
        result = runner.invoke(
            app, ["update", "--finalize-bootstrap", "--method", "venv"]
        )
    assert result.exit_code == 0, result.output
    assert calls == [True]


def test_new_updater_finalizer_uses_parent_db_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "root"))
    monkeypatch.setenv("DTXWIKI_DATABASE_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    monkeypatch.setenv(
        cmd.update_service.MAINTENANCE_DB_LOCK_PARENT_ENV, str(os.getppid())
    )
    monkeypatch.setattr(cmd, "_load_prod_env", lambda *a: None)
    calls = []
    monkeypatch.setattr(cmd, "bootstrap_app", lambda settings: calls.append(True))
    resources = RuntimeResources.for_settings(Settings())
    with resources.acquire_activity("exclusive"):
        result = runner.invoke(
            app, ["update", "--finalize-bootstrap", "--method", "venv"]
        )
    assert result.exit_code == 0, result.output
    assert calls == [True]


def test_old_updater_finalizer_refuses_remaining_db_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path / "root"))
    monkeypatch.setenv("DTXWIKI_DATABASE_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    monkeypatch.delenv(cmd.update_service.MAINTENANCE_DB_LOCK_PARENT_ENV, raising=False)
    monkeypatch.setattr(cmd, "_load_prod_env", lambda *a: None)
    monkeypatch.setattr(
        cmd, "bootstrap_app", lambda settings: pytest.fail("must not write")
    )
    resources = RuntimeResources.for_settings(Settings())
    with acquire_paths([resources.activity_paths[1]], "shared"):
        with runtime_lock.acquire_exclusive(resources.root):
            result = runner.invoke(
                app, ["update", "--finalize-bootstrap", "--method", "venv"]
            )
    assert result.exit_code == 3
    assert "finalize-bootstrap refused" in result.output


def test_update_abort_maps_to_exit_2(monkeypatch):
    # Non-interactive confirm raises typer.Abort (Typer would map it to exit 1);
    # the wrapper must surface the documented exit 2 'aborted by operator'.
    monkeypatch.setattr(cmd, "_load_prod_env", lambda method, env_file: None)

    def boom(*a, **k):
        raise typer.Abort()

    monkeypatch.setattr(cmd.update_service, "run_update", boom)
    result = runner.invoke(app, ["update", "--method", "venv"])
    assert result.exit_code == 2


def test_rollback_abort_maps_to_exit_2(monkeypatch):
    monkeypatch.setattr(cmd, "_load_prod_env", lambda method, env_file: None)

    def boom(*a, **k):
        raise typer.Abort()

    monkeypatch.setattr(cmd.update_service, "run_rollback", boom)
    result = runner.invoke(app, ["rollback", "--method", "venv"])
    assert result.exit_code == 2


def test_load_prod_env_systemd_sources_file(monkeypatch, tmp_path):
    # On the systemd tier the CLI must source the prod env file so Settings()
    # resolves the real DB/data dir instead of root defaults (B2/#31).
    env = tmp_path / "dtxwiki.env"
    env.write_text(
        "DTXWIKI_DATABASE_URL=sqlite+pysqlite:////var/lib/dtxwiki/dtxwiki.sqlite\n"
        "DTXWIKI_DATA_DIR=/var/lib/dtxwiki\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cmd.install_method,
        "detect_install_method",
        lambda override=None: InstallMethod(
            "systemd", None, None, (), None, ("dtxwiki",)
        ),
    )
    # setenv-then-delenv (not delenv(raising=False)) so monkeypatch records an
    # undo entry even when the var is absent — _load_prod_env writes os.environ
    # directly and the prod paths must not leak into later tests.
    for key in ("DTXWIKI_DATABASE_URL", "DTXWIKI_DATA_DIR"):
        monkeypatch.setenv(key, "leak-guard")
        monkeypatch.delenv(key)

    cmd._load_prod_env("systemd", str(env))

    db_url = os.environ["DTXWIKI_DATABASE_URL"]
    assert db_url.endswith("/var/lib/dtxwiki/dtxwiki.sqlite")
    assert os.environ["DTXWIKI_DATA_DIR"] == "/var/lib/dtxwiki"


def test_load_prod_env_strips_quotes_like_systemd(monkeypatch, tmp_path):
    # systemd's EnvironmentFile strips surrounding quotes, so a hand-quoted
    # DATABASE_URL works for the running service; the CLI must see the same
    # unquoted value or make_url() rejects it and the pre-update backup
    # silently skips the DB snapshot.
    from sqlalchemy.engine import make_url

    env = tmp_path / "dtxwiki.env"
    env.write_text(
        'DTXWIKI_DATABASE_URL="sqlite+pysqlite:////var/lib/dtxwiki/dtxwiki.sqlite"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cmd.install_method,
        "detect_install_method",
        lambda override=None: InstallMethod(
            "systemd", None, None, (), None, ("dtxwiki",)
        ),
    )
    monkeypatch.setenv("DTXWIKI_DATABASE_URL", "leak-guard")
    monkeypatch.delenv("DTXWIKI_DATABASE_URL")

    cmd._load_prod_env("systemd", str(env))

    url = make_url(os.environ["DTXWIKI_DATABASE_URL"])
    assert url.get_backend_name() == "sqlite"
    assert os.environ["DTXWIKI_DATABASE_URL"].startswith("sqlite+pysqlite://")


def test_update_no_db_backup_flag_reaches_service(monkeypatch):
    monkeypatch.setattr(cmd, "_load_prod_env", lambda method, env_file: None)
    captured = {}

    def fake_run_update(settings, **kwargs):
        captured.update(kwargs)
        return cmd.update_service.UpdateResult(
            True, 0, "0.1.0", "0.2.0", None, "updated"
        )

    monkeypatch.setattr(cmd.update_service, "run_update", fake_run_update)
    result = runner.invoke(
        app, ["update", "--method", "venv", "--yes", "--no-db-backup"]
    )
    assert result.exit_code == 0
    assert captured["no_db_backup"] is True


def test_finalize_bootstrap_echoes_redacted_db_url(monkeypatch):
    # The parent `dtxwiki update` re-echoes this subprocess output, so embedded
    # credentials (userinfo password, sslpassword) must never be printed raw.
    monkeypatch.setattr(cmd, "_load_prod_env", lambda method, env_file: None)
    monkeypatch.setattr(cmd, "bootstrap_app", lambda settings: None)
    monkeypatch.setenv(
        "DTXWIKI_DATABASE_URL",
        "postgresql+pg8000://dtx:s3cret@db.internal:5432/dtx?sslpassword=keypass",
    )

    result = runner.invoke(app, ["update", "--finalize-bootstrap"])

    assert result.exit_code == 0
    assert "s3cret" not in result.stdout
    assert "keypass" not in result.stdout
    assert "dtx:****@db.internal" in result.stdout
    assert "sslpassword=****" in result.stdout


@pytest.mark.parametrize("custom_env", [False, True])
@pytest.mark.parametrize("explicit_env", [False, True])
def test_systemd_update_finalization_preserves_split_worker(
    monkeypatch, tmp_path, custom_env, explicit_env
):
    default_env = tmp_path / "default.env"
    default_env.write_text("# Default file is untouched by a custom deployment\n")
    monkeypatch.setattr(cmd.prod_setup, "DEFAULT_ENV_FILE", default_env)
    env = tmp_path / "custom deployment.env" if custom_env else default_env
    original = "# Existing deployment\nDTXWIKI_SESSION_SECRET=preserve-this-value\n"
    env.write_text(original, encoding="utf-8")
    monkeypatch.setenv("DTXWIKI_WORKER_MODE", "managed")
    monkeypatch.setenv("DTXWIKI_SESSION_SECRET", "outer-test-session-secret")
    monkeypatch.setattr(
        cmd.install_method,
        "detect_install_method",
        lambda override=None: InstallMethod(
            "systemd", None, None, (), None, ("dtxwiki", "dtxwiki-worker")
        ),
    )
    seen = []

    def bootstrap(settings):
        seen.append(settings.worker_mode)
        assert cmd.prod_setup.get_env_value(env, "DTXWIKI_WORKER_MODE") == "external"

    monkeypatch.setattr(cmd, "bootstrap_app", bootstrap)
    queries = []

    def systemctl_show(argv, **kwargs):
        assert not explicit_env, "an explicit --env-file must skip discovery"
        queries.append(argv)
        assert argv == [
            "systemctl",
            "show",
            "dtxwiki.service",
            "--property=EnvironmentFiles",
            "--value",
        ]
        assert kwargs == {
            "check": True,
            "capture_output": True,
            "text": True,
            "timeout": 5,
        }
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"{env} (ignore_errors=no)\n", stderr=""
        )

    monkeypatch.setattr(cmd.subprocess, "run", systemctl_show)

    # The previous updater omits --env-file, even for custom deployments. The
    # current updater explicitly forwards either the default or custom path.
    args = ["update", "--finalize-bootstrap", "--method", "systemd"]
    if explicit_env:
        args.extend(["--env-file", str(env)])
    result = runner.invoke(
        app,
        args,
    )

    assert result.exit_code == 0, result.output
    assert len(queries) == (0 if explicit_env else 1)
    assert seen == ["external"]
    assert (
        cmd.prod_setup.get_env_value(env, "DTXWIKI_SESSION_SECRET")
        == "preserve-this-value"
    )
    assert env.with_name(env.name + ".bak").read_text(encoding="utf-8") == original
    if custom_env:
        assert default_env.read_text() == (
            "# Default file is untouched by a custom deployment\n"
        )


@pytest.mark.parametrize(
    "invalid_output",
    [
        "",
        "{env} (ignore_errors=no) {env} (ignore_errors=yes)",
        "{env} (ignore_errors=no)\n{env} (ignore_errors=yes)",
        "relative.env (ignore_errors=no)",
        "{directory}/missing.env (ignore_errors=no)",
        "{directory} (ignore_errors=no)",
        "{env}",
        "{env} (ignore_errors=invalid)",
        "unexpected\n{env} (ignore_errors=no)",
    ],
)
def test_legacy_finalization_refuses_ambiguous_or_invalid_systemd_env(
    monkeypatch, tmp_path, invalid_output
):
    env = tmp_path / "default.env"
    original = "# Must remain untouched\n"
    env.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cmd.prod_setup, "DEFAULT_ENV_FILE", env)
    monkeypatch.setattr(
        cmd.install_method,
        "detect_install_method",
        lambda override=None: InstallMethod(
            "systemd", None, None, (), None, ("dtxwiki", "dtxwiki-worker")
        ),
    )
    monkeypatch.setattr(
        cmd.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout=invalid_output.format(env=env, directory=tmp_path),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        cmd,
        "_load_prod_env",
        lambda *args: pytest.fail("must discover the env file before sourcing it"),
    )
    monkeypatch.setattr(
        cmd,
        "bootstrap_app",
        lambda settings: pytest.fail("must not migrate"),
    )

    result = runner.invoke(
        app, ["update", "--finalize-bootstrap", "--method", "systemd"]
    )

    assert result.exit_code == 3, result.output
    assert "Keep services stopped" in result.output
    assert "--env-file /path/to/dtxwiki.env" in result.output
    assert env.read_text(encoding="utf-8") == original
    assert not env.with_name(env.name + ".bak").exists()


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("systemctl missing"),
        subprocess.TimeoutExpired(["systemctl"], 5),
        subprocess.CalledProcessError(1, ["systemctl"]),
    ],
)
def test_legacy_finalization_refuses_failed_systemd_lookup(monkeypatch, failure):
    def failed_lookup(*args, **kwargs):
        raise failure

    monkeypatch.setattr(cmd.subprocess, "run", failed_lookup)

    with pytest.raises(typer.Exit) as exc:
        cmd._legacy_systemd_env_file()

    assert exc.value.exit_code == 3


def test_venv_finalization_keeps_managed_worker_default(monkeypatch, tmp_path):
    env = tmp_path / "dtxwiki.env"
    original = "# Custom environment\n"
    env.write_text(original, encoding="utf-8")
    monkeypatch.setenv("DTXWIKI_WORKER_MODE", "managed")
    seen = []
    monkeypatch.setattr(
        cmd, "bootstrap_app", lambda settings: seen.append(settings.worker_mode)
    )

    result = runner.invoke(
        app,
        ["update", "--finalize-bootstrap", "--method", "venv", "--env-file", str(env)],
    )

    assert result.exit_code == 0, result.output
    assert seen == ["managed"]
    assert env.read_text(encoding="utf-8") == original


def test_update_invalid_method_is_usage_error():
    # An unknown --method must be a clean exit-2 usage error, not the raw
    # ValueError detect_install_method raises (unhandled-exception box, exit 1).
    result = runner.invoke(app, ["update", "--method", "banana"])
    assert result.exit_code == 2
    assert "must be one of" in result.output
    assert "Traceback" not in result.output


def test_rollback_invalid_method_is_usage_error():
    result = runner.invoke(app, ["rollback", "--method", "banana"])
    assert result.exit_code == 2
    assert "must be one of" in result.output
    assert "Traceback" not in result.output


def test_validate_method_accepts_all_kinds_and_auto():
    from backend.services.ops.install_method import VALID_KINDS

    for value in sorted(VALID_KINDS | {"auto"}):
        assert cmd._validate_method(value) == value


def test_load_prod_env_noop_on_venv(monkeypatch):
    # Non-systemd tier without --env-file must not touch the environment.
    monkeypatch.setattr(
        cmd.install_method,
        "detect_install_method",
        lambda override=None: InstallMethod(
            "venv", None, None, ("py", "-m", "pip"), None, ()
        ),
    )
    assert cmd._load_prod_env("venv", None) is None
