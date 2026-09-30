"""`dtxwiki setup production` on a remote host: ordering, listen address, files.

Covers the release fixes for the systemd Quick install: the database is
bootstrapped (and handed to the service account) before any unit is enabled or
started, `--bind-host` reaches the installed unit, and the env file / Caddyfile
/ data directory end up with the ownership the services need.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

import backend.cli.commands.prod as prod_commands
import backend.cli.main as cli_main
from backend.app.bootstrap import current_alembic_head
from backend.cli import prod_setup

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_TEMPLATE = REPO_ROOT / "infra" / "prod" / prod_setup.BACKEND_UNIT


def _out(result) -> str:
    text = result.stdout or ""
    try:
        text += result.stderr or ""
    except ValueError:
        pass
    return text


class _Systemctl:
    """Records systemctl calls; any other command runs for real."""

    def __init__(self, events: list[str], on_call=None) -> None:
        self.events = events
        self.on_call = on_call
        self._real_run = subprocess.run

    def __call__(self, argv, *args, **kwargs):
        argv = list(argv)
        if argv and argv[0] == "systemctl":
            self.events.append(" ".join(argv))
            if self.on_call is not None:
                self.on_call(argv)
            return subprocess.CompletedProcess(argv, 0)
        return self._real_run(argv, *args, **kwargs)


@pytest.fixture
def unit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "systemd"
    directory.mkdir()
    monkeypatch.setattr(prod_setup, "SYSTEMD_UNIT_DIR", directory)
    return directory


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "env": tmp_path / "etc" / "dtxwiki.env",
        "jwt": tmp_path / "etc" / "jwt.pem",
        "caddy": tmp_path / "Caddyfile",
        "db": tmp_path / "data" / "dtxwiki.sqlite",
    }


def _setup_args(paths: dict[str, Path], *extra: str) -> list[str]:
    paths["db"].parent.mkdir(parents=True, exist_ok=True)
    return [
        "setup", "production",
        "--domain", "wiki.example.com",
        "--email", "admin@example.com",
        "--env-file", str(paths["env"]),
        "--jwt-key", str(paths["jwt"]),
        "--caddyfile", str(paths["caddy"]),
        "--database-url", f"sqlite+pysqlite:///{paths['db'].as_posix()}",
        "--skip-preflight",
        "--no-smoke",
        *extra,
    ]


def _db_revision(db: Path) -> str | None:
    if not db.exists():
        return None
    with closing(sqlite3.connect(db)) as conn:
        try:
            row = conn.execute("select version_num from alembic_version").fetchone()
        except sqlite3.OperationalError:
            return None
    return row[0] if row else None


def _db_has_user(db: Path, email: str) -> bool:
    with closing(sqlite3.connect(db)) as conn:
        return (
            conn.execute("select count(*) from users where email = ?", (email,))
            .fetchone()[0]
            == 1
        )


# --------------------------------------------------------------------------- #
# C9: bootstrap before the units are enabled or started
# --------------------------------------------------------------------------- #
def test_setup_bootstraps_and_hands_over_data_before_units_start(
    tmp_path: Path, unit_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = _paths(tmp_path)
    events: list[str] = []
    state_when_started: dict[str, object] = {}
    real_bootstrap = prod_commands.bootstrap_app
    real_hand_over = prod_setup.hand_data_to_service_user

    def recording_bootstrap(settings, app=None):
        events.append("bootstrap")
        return real_bootstrap(settings, app)

    def recording_hand_over(data_dir, service_user, **kwargs):
        events.append("hand-over")
        return real_hand_over(data_dir, service_user, **kwargs)

    def on_systemctl(argv: list[str]) -> None:
        if argv[1] in {"enable", "restart"}:
            db = paths["db"]
            # Looked at before this test opens the database itself: setup's own
            # connections are closed, so nothing root created (-wal/-shm) is
            # left behind for the service account to trip over.
            sidecars = sorted(p.name for p in db.parent.glob(db.name + "-*"))
            state_when_started[argv[1]] = {
                "revision": _db_revision(db),
                "admin": _db_has_user(db, "admin@example.com"),
                "sidecars": sidecars,
                "units": sorted(p.name for p in unit_dir.iterdir()),
            }

    monkeypatch.setattr(prod_commands, "bootstrap_app", recording_bootstrap)
    monkeypatch.setattr(prod_setup, "hand_data_to_service_user", recording_hand_over)
    monkeypatch.setattr(prod_setup.subprocess, "run", _Systemctl(events, on_systemctl))

    result = runner.invoke(cli_main.app, _setup_args(paths, "--no-caddy"))

    assert result.exit_code == 0, _out(result)
    assert events == [
        "bootstrap",
        "hand-over",
        "systemctl daemon-reload",
        "systemctl enable dtxwiki dtxwiki-worker",
        "systemctl restart dtxwiki dtxwiki-worker",
    ]
    expected = {
        "revision": current_alembic_head(),
        "admin": True,
        "sidecars": [],
        "units": ["dtxwiki-worker.service", "dtxwiki.service"],
    }
    assert state_when_started == {"enable": expected, "restart": expected}


def test_setup_without_bootstrap_says_the_services_will_refuse(
    tmp_path: Path, unit_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = _paths(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(prod_setup.subprocess, "run", _Systemctl(events))

    result = runner.invoke(
        cli_main.app,
        _setup_args(paths, "--no-caddy", "--no-bootstrap", "--no-admin"),
    )

    assert result.exit_code == 0, _out(result)
    assert "until 'dtxwiki bootstrap' has migrated the database" in _out(result)
    assert _db_revision(paths["db"]) is None


# --------------------------------------------------------------------------- #
# M-18(g): --bind-host is written into the backend unit
# --------------------------------------------------------------------------- #
def _exec_start(unit: Path) -> str:
    lines = [
        line
        for line in unit.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    ]
    assert len(lines) == 1
    return lines[0]


def test_default_unit_listens_on_loopback_and_is_world_readable(
    tmp_path: Path, unit_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = _paths(tmp_path)
    monkeypatch.setattr(prod_setup.subprocess, "run", _Systemctl([]))

    result = runner.invoke(
        cli_main.app,
        _setup_args(paths, "--no-caddy", "--no-bootstrap", "--no-admin"),
    )

    assert result.exit_code == 0, _out(result)
    backend = unit_dir / "dtxwiki.service"
    assert " --host 127.0.0.1 --port 21080" in _exec_start(backend)
    assert stat.S_IMODE(backend.stat().st_mode) == 0o644
    # Everything but the listen address is the shipped unit, verbatim.
    assert backend.read_text(encoding="utf-8") == BACKEND_TEMPLATE.read_text(
        encoding="utf-8"
    )
    worker = unit_dir / "dtxwiki-worker.service"
    assert worker.read_text(encoding="utf-8") == (
        REPO_ROOT / "infra" / "prod" / "dtxwiki-worker.service"
    ).read_text(encoding="utf-8")


def test_bind_host_for_a_remote_load_balancer_reaches_unit_and_env(
    tmp_path: Path, unit_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = _paths(tmp_path)
    monkeypatch.setattr(prod_setup.subprocess, "run", _Systemctl([]))

    result = runner.invoke(
        cli_main.app,
        _setup_args(
            paths,
            "--tls", "external",
            "--bind-host", "0.0.0.0",
            "--forwarded-ip", "10.0.0.2",
            "--no-bootstrap", "--no-admin",
        ),
    )

    assert result.exit_code == 0, _out(result)
    assert " --host 0.0.0.0 --port 21080" in _exec_start(unit_dir / "dtxwiki.service")
    env = prod_setup.read_env_file(paths["env"])
    assert env["DTXWIKI_FORWARDED_ALLOW_IPS"] == "10.0.0.2"
    assert "dtxWiki listens on 0.0.0.0" in _out(result)
    assert not paths["caddy"].exists()


def test_reconfigure_keeps_the_installed_bind_host(
    tmp_path: Path, unit_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = _paths(tmp_path)
    monkeypatch.setattr(prod_setup.subprocess, "run", _Systemctl([]))
    base = _setup_args(
        paths,
        "--tls", "external",
        "--forwarded-ip", "10.0.0.2",
        "--no-bootstrap", "--no-admin",
    )
    first = runner.invoke(cli_main.app, [*base, "--bind-host", "10.0.0.5"])
    assert first.exit_code == 0, _out(first)

    second = runner.invoke(cli_main.app, [*base, "--reconfigure"])

    assert second.exit_code == 0, _out(second)
    assert " --host 10.0.0.5 --port 21080" in _exec_start(unit_dir / "dtxwiki.service")


def test_remote_bind_host_without_a_trusted_proxy_is_refused(
    tmp_path: Path, unit_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = _paths(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(prod_setup.subprocess, "run", _Systemctl(events))

    result = runner.invoke(
        cli_main.app,
        _setup_args(paths, "--tls", "external", "--bind-host", "0.0.0.0"),
    )

    assert result.exit_code == 2
    assert "--forwarded-ip" in _out(result)
    assert not paths["env"].exists()
    assert list(unit_dir.iterdir()) == []
    assert events == []


@pytest.mark.parametrize(
    "value", ["0.0.0.0 --reload", "127.0.0.1\nExecStartPre=/bin/sh", "", "a b"]
)
def test_bind_host_must_be_a_plain_address(
    tmp_path: Path, unit_dir: Path, monkeypatch: pytest.MonkeyPatch, value: str
):
    paths = _paths(tmp_path)
    monkeypatch.setattr(prod_setup.subprocess, "run", _Systemctl([]))

    result = runner.invoke(
        cli_main.app,
        _setup_args(paths, "--tls", "external", "--bind-host", value),
    )

    assert result.exit_code == 2
    assert not paths["env"].exists()
    assert list(unit_dir.iterdir()) == []


def test_render_backend_unit_changes_only_the_listen_address(tmp_path: Path):
    template = BACKEND_TEMPLATE.read_text(encoding="utf-8")

    rendered = prod_setup.render_backend_unit(template, bind_host="::")

    assert rendered == template.replace(
        "--host 127.0.0.1 --port", "--host :: --port"
    )
    installed = tmp_path / "dtxwiki.service"
    installed.write_text(rendered, encoding="utf-8")
    assert prod_setup.installed_bind_host(installed) == "::"
    assert prod_setup.installed_bind_host(tmp_path / "missing.service") is None
    with pytest.raises(ValueError, match="exactly one ExecStart"):
        prod_setup.render_backend_unit(
            "[Service]\nExecStart=/bin/true\n", bind_host="::"
        )


# --------------------------------------------------------------------------- #
# M-18(c): env file root:<service user> 0640, Caddyfile readable by caddy
# --------------------------------------------------------------------------- #
def test_set_env_file_ownership_as_root_is_group_readable(tmp_path: Path):
    env = tmp_path / "dtxwiki.env"
    env.write_text("DTXWIKI_APP_ENV=production\n", encoding="utf-8")
    calls: list[tuple] = []

    applied = prod_setup.set_env_file_ownership(
        env,
        "dtxwiki",
        geteuid=lambda: 0,
        chown=lambda path, user=None, group=None: calls.append(
            ("chown", str(path), user, group)
        ),
        chmod=lambda path, mode: calls.append(("chmod", str(path), mode)),
    )

    assert applied is True
    assert calls == [
        ("chown", str(env), "root", "dtxwiki"),
        ("chmod", str(env), 0o640),
    ]


def test_set_env_file_ownership_is_a_noop_when_not_root(tmp_path: Path):
    env = tmp_path / "dtxwiki.env"
    env.write_text("X=1\n", encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("must not chown/chmod when not root")

    assert (
        prod_setup.set_env_file_ownership(
            env, "dtxwiki", geteuid=lambda: 1000, chown=forbidden, chmod=forbidden
        )
        is False
    )


def test_setup_secures_the_env_file_it_wrote(
    tmp_path: Path, unit_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = _paths(tmp_path)
    seen: list[tuple[Path, str, bool]] = []

    def recording(path, service_group, **_kwargs):
        seen.append((path, service_group, path.exists()))
        return False

    monkeypatch.setattr(prod_setup, "set_env_file_ownership", recording)
    result = runner.invoke(
        cli_main.app,
        _setup_args(
            paths,
            "--no-systemd", "--no-caddy", "--no-bootstrap", "--no-admin",
            "--service-user", "wikisvc",
        ),
    )

    assert result.exit_code == 0, _out(result)
    assert seen == [(paths["env"], "wikisvc", True)]


def test_config_rewrite_keeps_the_env_file_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env = tmp_path / "dtxwiki.env"
    env.write_text("DTXWIKI_APP_NAME=dtxWiki\n", encoding="utf-8")
    before = env.stat()
    restored: list[tuple[Path, tuple[int, int] | None]] = []
    monkeypatch.setattr(
        prod_setup,
        "_restore_owner",
        lambda path, owner: restored.append((path, owner)),
    )

    prod_setup.set_env_values(env, {"DTXWIKI_APP_NAME": "Wiki"})
    prod_setup.write_env_text(env, "DTXWIKI_APP_NAME=Other\n")

    owner = (before.st_uid, before.st_gid)
    assert restored == [(env, owner), (env, owner)]


def test_restore_owner_chowns_back_only_when_the_owner_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "dtxwiki.env"
    path.write_text("X=1\n", encoding="utf-8")
    current = (path.stat().st_uid, path.stat().st_gid)
    calls: list[tuple] = []
    monkeypatch.setattr(prod_setup.os, "chown", lambda *args: calls.append(args))

    prod_setup._restore_owner(path, current)
    assert calls == []

    prod_setup._restore_owner(path, (0, 4242))
    assert calls == [(path, 0, 4242)]


def test_setup_writes_a_caddyfile_caddy_can_read(
    tmp_path: Path, unit_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = _paths(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(prod_setup.subprocess, "run", _Systemctl(events))
    old_umask = os.umask(0o077)
    try:
        result = runner.invoke(
            cli_main.app,
            _setup_args(
                paths,
                "--tls", "internal",
                "--no-systemd", "--no-bootstrap", "--no-admin",
            ),
        )
    finally:
        os.umask(old_umask)

    assert result.exit_code == 0, _out(result)
    caddyfile = paths["caddy"]
    assert stat.S_IMODE(caddyfile.stat().st_mode) == 0o644
    text = caddyfile.read_text(encoding="utf-8")
    assert "wiki.example.com {" in text
    assert "tls internal" in text
    assert "max_size 64MB" in text
    assert events == ["systemctl reload-or-restart caddy"]


# --------------------------------------------------------------------------- #
# The data directory and database belong to the service account
# --------------------------------------------------------------------------- #
def _recorder():
    calls: list[tuple[str, int, int]] = []

    def lchown(path, uid, gid):
        calls.append((str(path), uid, gid))

    return calls, lchown


def test_hand_over_reowns_the_tree_without_following_links(tmp_path: Path):
    data = tmp_path / "var" / "lib" / "dtxwiki"
    (data / "library" / "blobs").mkdir(parents=True)
    (data / "dtxwiki.sqlite").write_bytes(b"db")
    (data / "dtxwiki.sqlite-wal").write_bytes(b"wal")
    (data / "library" / "blobs" / "a.bin").write_bytes(b"blob")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x", encoding="utf-8")
    (data / "escape").symlink_to(outside, target_is_directory=True)
    calls, lchown = _recorder()
    modes: list[tuple[str, int]] = []

    problems = prod_setup.hand_data_to_service_user(
        data,
        "dtxwiki",
        geteuid=lambda: 0,
        lchown=lchown,
        chmod=lambda path, mode: modes.append((str(path), mode)),
        lookup_ids=lambda _user: (64000, 64000),
    )

    assert problems == []
    owned = {path for path, _uid, _gid in calls}
    assert owned == {
        str(data),
        str(data / "library"),
        str(data / "library" / "blobs"),
        str(data / "library" / "blobs" / "a.bin"),
        str(data / "dtxwiki.sqlite"),
        str(data / "dtxwiki.sqlite-wal"),
        str(data / "escape"),
    }
    assert {(uid, gid) for _path, uid, gid in calls} == {(64000, 64000)}
    assert str(outside / "secret") not in owned
    assert modes == [(str(data), 0o750)]


def test_hand_over_refuses_system_directories(tmp_path: Path):
    calls, lchown = _recorder()

    for directory in (Path("/var/lib"), Path("/"), Path("/srv")):
        problems = prod_setup.hand_data_to_service_user(
            directory,
            "dtxwiki",
            geteuid=lambda: 0,
            lchown=lchown,
            chmod=lambda *_args: None,
            lookup_ids=lambda _user: (64000, 64000),
        )
        assert problems and "system directory" in problems[0]

    assert calls == []


def test_hand_over_also_reowns_a_database_outside_the_data_dir(tmp_path: Path):
    data = tmp_path / "srv" / "data"
    data.mkdir(parents=True)
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    database = db_dir / "dtxwiki.sqlite"
    database.write_bytes(b"db")
    (db_dir / "dtxwiki.sqlite-shm").write_bytes(b"shm")
    calls, lchown = _recorder()

    problems = prod_setup.hand_data_to_service_user(
        data,
        "dtxwiki",
        database_file=database,
        geteuid=lambda: 0,
        lchown=lchown,
        chmod=lambda *_args: None,
        lookup_ids=lambda _user: (64000, 64000),
    )

    assert problems == []
    owned = {path for path, _uid, _gid in calls}
    assert {str(database), str(db_dir / "dtxwiki.sqlite-shm")} <= owned
    assert str(db_dir / "dtxwiki.sqlite-wal") not in owned


def test_hand_over_reports_a_missing_service_account(tmp_path: Path):
    def missing(_user):
        raise KeyError(_user)

    problems = prod_setup.hand_data_to_service_user(
        tmp_path / "var" / "lib" / "dtxwiki",
        "dtxwiki",
        geteuid=lambda: 0,
        lookup_ids=missing,
    )

    assert problems == ["service account 'dtxwiki' does not exist"]


def test_hand_over_is_a_noop_when_not_root(tmp_path: Path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("must not touch ownership when not root")

    assert (
        prod_setup.hand_data_to_service_user(
            tmp_path,
            "dtxwiki",
            geteuid=lambda: 1000,
            lchown=forbidden,
            chmod=forbidden,
            lookup_ids=forbidden,
        )
        == []
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "sqlite+pysqlite:////var/lib/dtxwiki/dtxwiki.sqlite",
            "/var/lib/dtxwiki/dtxwiki.sqlite",
        ),
        ("sqlite:///:memory:", None),
        ("sqlite://", None),
        ("postgresql+pg8000://u:p@db/dtx", None),
        ("not a url", None),
    ],
)
def test_sqlite_database_file(url: str, expected: str | None):
    result = prod_setup.sqlite_database_file(url)
    assert (str(result) if result else None) == expected


# --------------------------------------------------------------------------- #
# C21: an empty or placeholder session secret is replaced, a real one kept
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "app_mode", "expected"),
    [
        ("", "team", True),
        ("   ", "team", True),
        ("<replace-with-64-byte-secret>", "team", True),
        ("my-change-me-secret-that-is-quite-long-enough", "team", True),
        ("dtxwiki-development-session-secret", "solo_local", True),
        ("short-but-not-a-placeholder", "team", True),
        ("short-but-not-a-placeholder", "solo_local", False),
        ("s" * 80, "team", False),
    ],
)
def test_session_secret_needs_replacement(value: str, app_mode: str, expected: bool):
    assert (
        prod_setup.session_secret_needs_replacement(value, app_mode=app_mode)
        is expected
    )


@pytest.mark.parametrize("template_value", ["", "<replace-with-64-byte-secret>"])
def test_reconfigure_replaces_a_template_session_secret(
    tmp_path: Path, template_value: str
):
    env = tmp_path / "dtxwiki.env"
    env.write_text(
        "DTXWIKI_PUBLIC_BASE_URL=https://wiki.example.com\n"
        f"DTXWIKI_SESSION_SECRET={template_value}\n"
        "DTXWIKI_APP_ENV=production\n",
        encoding="utf-8",
    )

    db_url = f"sqlite+pysqlite:///{(tmp_path / 'db.sqlite').as_posix()}"
    result = runner.invoke(
        cli_main.app,
        [
            "setup", "production",
            "--env-file", str(env),
            "--jwt-key", str(tmp_path / "jwt.pem"),
            "--database-url", db_url,
            "--skip-preflight",
            "--no-systemd", "--no-caddy", "--no-smoke", "--no-admin", "--no-bootstrap",
            "--reconfigure",
        ],
    )

    assert result.exit_code == 0, _out(result)
    secret = prod_setup.get_env_value(env, "DTXWIKI_SESSION_SECRET")
    assert secret and secret != template_value
    assert not prod_setup.session_secret_needs_replacement(secret, app_mode="team")
    assert secret not in _out(result)
