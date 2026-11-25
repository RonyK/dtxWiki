"""Unit tests for the shared operator-operations core (backend/services/ops)."""

import sqlite3

import httpx
import pytest

from backend.services.ops import (
    db_lock,
    healthcheck,
    index,
    install_method,
    service_control,
)

# --- index ---------------------------------------------------------------


def test_resolve_index_url_precedence():
    assert index.resolve_index_url(flag="a", settings_index_url="b") == "a"
    assert index.resolve_index_url(flag=None, settings_index_url="b") == "b"
    assert index.resolve_index_url() is None


def test_package_spec():
    assert index.package_spec() == "dtxwiki"
    assert index.package_spec("0.2.0") == "dtxwiki==0.2.0"


def test_build_pip_install_argv_basic():
    assert index.build_pip_install_argv(["py", "-m", "pip"], "dtxwiki") == [
        "py", "-m", "pip", "install", "--upgrade", "dtxwiki",
    ]


def test_build_pip_install_argv_with_index_and_force():
    assert index.build_pip_install_argv(
        ["pip"], "dtxwiki==0.1.0",
        index_url="https://idx/simple", upgrade=False, force_reinstall=True,
    ) == [
        "pip", "install", "--force-reinstall",
        "--index-url", "https://idx/simple",
        "dtxwiki==0.1.0",
    ]


def test_build_pipx_install_argv_with_index():
    argv = index.build_pipx_install_argv("dtxwiki==0.2.0", index_url="https://idx/simple")
    assert argv[:3] == ["pipx", "install", "--force"]
    assert "dtxwiki==0.2.0" in argv
    assert argv[-2:] == ["--pip-args", "--index-url https://idx/simple"]
    assert "--extra-index-url" not in " ".join(argv)


def test_build_pip_install_argv_pre():
    assert "--pre" in index.build_pip_install_argv(["pip"], "dtxwiki", pre=True)


def test_build_pipx_install_argv_pre():
    argv = index.build_pipx_install_argv("dtxwiki", pre=True)
    assert "--pre" in " ".join(argv)


# --- service_control -----------------------------------------------------


def test_systemctl_argv():
    assert service_control.systemctl_argv("stop") == [
        "systemctl", "stop", "dtxwiki", "dtxwiki-worker",
    ]
    assert service_control.systemctl_argv("enable", extra_flags=("--now",)) == [
        "systemctl", "enable", "--now", "dtxwiki", "dtxwiki-worker",
    ]


# --- install_method ------------------------------------------------------


def test_detect_install_method_venv_override():
    im = install_method.detect_install_method(override="venv")
    assert im.kind == "venv"
    assert im.services == ()
    assert im.pip_argv[-1] == "pip"


def test_detect_install_method_systemd_override():
    im = install_method.detect_install_method(override="systemd")
    assert im.kind == "systemd"
    assert im.services == ("dtxwiki", "dtxwiki-worker")
    assert im.venv_path == install_method.SYSTEMD_VENV


def test_detect_install_method_docker_override():
    im = install_method.detect_install_method(override="docker")
    assert im.kind == "docker"
    assert im.services == ()
    assert im.dtxwiki_bin is None


def test_detect_install_method_pipx_override():
    im = install_method.detect_install_method(override="pipx")
    assert im.kind == "pipx"
    assert im.pip_argv == ("pipx",)


def test_detect_install_method_npm_override():
    im = install_method.detect_install_method(override="npm")
    assert im.kind == "npm"
    assert im.pip_argv == ()
    assert im.dtxwiki_bin is None
    assert im.services == ()


def test_npm_launcher_wins_auto_detection(monkeypatch):
    # The launcher's env var describes this process; a docker or systemd host
    # around it must not send `dtxwiki update` to that other tier.
    monkeypatch.setattr(install_method, "running_in_docker", lambda: True)
    monkeypatch.setattr(install_method, "systemd_unit_present", lambda: True)
    monkeypatch.setenv(install_method.LAUNCHER_ENV, "npm")
    assert install_method.detect_install_method().kind == "npm"

    monkeypatch.setenv(install_method.LAUNCHER_ENV, "something-else")
    assert install_method.detect_install_method().kind == "docker"


def test_detect_install_method_bad_override():
    with pytest.raises(ValueError):
        install_method.detect_install_method(override="nope")


def test_bin_or_none_reflects_disk_presence(tmp_path):
    # #6: an absent console script must resolve to None so update's is-None
    # safe-exit fires instead of crashing mid-migration.
    venv = tmp_path / "venv"
    assert install_method._bin_or_none(venv, "dtxwiki") is None
    target = install_method._venv_bin(venv, "dtxwiki")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    assert install_method._bin_or_none(venv, "dtxwiki") == target


# --- db_lock -------------------------------------------------------------


def test_is_sqlite_url():
    assert db_lock.is_sqlite_url("sqlite+pysqlite:////var/lib/x.sqlite") is True
    assert db_lock.is_sqlite_url("postgresql+pg8000://h/db") is False
    assert db_lock.is_sqlite_url("sqlite+pysqlite:///:memory:") is False


def test_is_sqlite_url_edge_cases():
    # Windows drive path is on-disk SQLite.
    assert db_lock.is_sqlite_url("sqlite:///C:/path/to/db.sqlite") is True
    # mode=memory query is in-memory, not a real file lock to contend for.
    assert db_lock.is_sqlite_url("sqlite+pysqlite:///foo.db?mode=memory") is False


def test_probe_noop_for_non_sqlite():
    db_lock.probe_sqlite_writable("postgresql+pg8000://h/db")  # must not raise/connect


def test_probe_ok_for_unlocked_file(tmp_path):
    path = tmp_path / "t.sqlite"
    sqlite3.connect(str(path)).close()
    db_lock.probe_sqlite_writable(f"sqlite+pysqlite:///{path.as_posix()}")


# --- healthcheck ---------------------------------------------------------


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_poll_health_ready_success(monkeypatch):
    monkeypatch.setattr(
        healthcheck.httpx, "get",
        lambda url, timeout: _Resp(200, {"status": "ready"}),
    )
    assert healthcheck.poll_health_ready("http://x/", sleep=lambda _s: None) is True


def test_poll_health_ready_times_out(monkeypatch):
    monkeypatch.setattr(
        healthcheck.httpx, "get",
        lambda url, timeout: _Resp(503, {"status": "unhealthy"}),
    )
    ticks = {"v": 0.0}

    def fake_now():
        ticks["v"] += 10.0
        return ticks["v"]

    assert healthcheck.poll_health_ready(
        "http://x", timeout_sec=5, sleep=lambda _s: None, now=fake_now,
    ) is False


def test_poll_health_ready_swallows_http_error(monkeypatch):
    def boom(url, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(healthcheck.httpx, "get", boom)
    ticks = {"v": 0.0}

    def fake_now():
        ticks["v"] += 10.0
        return ticks["v"]

    assert healthcheck.poll_health_ready(
        "http://x", timeout_sec=5, sleep=lambda _s: None, now=fake_now,
    ) is False
