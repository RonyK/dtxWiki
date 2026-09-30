"""Environment isolation helpers for settings-facing tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REAL_DATA_DIR = (Path.home() / ".dtxwiki").resolve()
# The Host Starlette's TestClient sends, allowed suite-wide by
# backend/tests/conftest.py (see backend/policy/allowed_hosts.py).
TEST_CLIENT_ALLOWED_HOSTS_ENV = "DTXWIKI_ALLOWED_HOSTS"
TEST_CLIENT_ALLOWED_HOSTS = "testserver"


def is_inside_real_data_dir(path: Path) -> bool:
    """True when ``path`` is the real installation or anything beneath it.

    Containment, not equality: ``~/.dtxwiki/library`` is just as much the
    user's installation as ``~/.dtxwiki`` itself. Callers pass an already
    ``resolve()``d path so a symlink pointing under it is caught too.

    Defined here, and imported by both the repo-root conftest hook and
    backend/tests/unit/test_data_dir_isolation.py, because a second copy is
    how the two drifted apart the first time -- the hook rejecting only an
    exact match while the test already knew to reject children.
    """

    return path == REAL_DATA_DIR or REAL_DATA_DIR in path.parents


def clear_ambient_dtxwiki_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited dtxWiki configuration for one test.

    Tests that exercise an environment setting must opt in explicitly after this
    helper runs.  Keeping this local to settings-facing modules avoids changing
    collection-time opt-ins used by PostgreSQL and live-provider suites.
    """

    preserved = os.environ.get("DTXWIKI_DATA_DIR")
    allowed_hosts = os.environ.get(TEST_CLIENT_ALLOWED_HOSTS_ENV)
    for name in tuple(os.environ):
        if name.startswith("DTXWIKI_"):
            monkeypatch.delenv(name, raising=False)
    if preserved is not None:
        # DTXWIKI_DATA_DIR is the suite's redirect away from the real
        # ~/.dtxwiki installation (see the repo-root conftest), not a Settings
        # field these tests inspect -- settings.py lists it under
        # OPERATIONAL_ENV_KEYS for exactly that reason. Stripping it would send
        # the test back to the developer's real data directory.
        monkeypatch.setenv("DTXWIKI_DATA_DIR", preserved)
    if allowed_hosts == TEST_CLIENT_ALLOWED_HOSTS:
        # Harness configuration, not inherited operator config: without it
        # every TestClient request (Host: testserver) is refused with 400.
        monkeypatch.setenv(TEST_CLIENT_ALLOWED_HOSTS_ENV, allowed_hosts)


def child_env_without_dtxwiki_config() -> dict[str, str]:
    """A subprocess env with dtxWiki config stripped but the data dir kept."""

    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("DTXWIKI_")
    }
    data_dir = os.environ.get("DTXWIKI_DATA_DIR")
    if data_dir is not None:
        env["DTXWIKI_DATA_DIR"] = data_dir
    return env
