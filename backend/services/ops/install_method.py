"""Detect how this dtxWiki is installed, so update/rollback can drive it.

Resolution order (first match wins unless ``override`` is given):
npm -> docker -> systemd -> pipx -> venv (plain pip-in-venv fallback).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: systemd units installed by ``setup production`` / install.sh.
SYSTEMD_SERVICES: tuple[str, ...] = ("dtxwiki", "dtxwiki-worker")
#: Fixed production venv (matches install.sh INSTALL_DIR and the systemd units).
SYSTEMD_VENV = Path("/opt/dtxwiki/.venv")

VALID_KINDS = frozenset({"npm", "docker", "systemd", "pipx", "venv"})

#: Set to ``npm`` by the ``@dtxsuite/dtxwiki`` launcher
#: (npm/dtxwiki/bin/dtxwiki.js) for the dtxwiki it starts.
LAUNCHER_ENV = "DTXWIKI_LAUNCHER"


@dataclass(frozen=True)
class InstallMethod:
    """How dtxWiki is installed + how to drive its package/service lifecycle."""

    kind: str  # one of VALID_KINDS
    venv_path: Path | None
    python_path: Path | None
    pip_argv: tuple[str, ...]  # prefix to build a pip/pipx command from
    dtxwiki_bin: Path | None  # the `dtxwiki` entry point to re-exec after upgrade
    services: tuple[str, ...]


def _venv_bin(venv: Path, name: str) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / f"{name}.exe"
    return venv / "bin" / name


def _bin_or_none(venv: Path, name: str) -> Path | None:
    """``_venv_bin`` but ``None`` when the console script is not on disk.

    Lets ``detect_install_method`` report ``dtxwiki_bin=None`` so update's
    ``is None`` safe-exit fires (notify-only exit-3) instead of crashing with an
    uncaught ``FileNotFoundError`` mid-migration when the entry point is absent.
    """
    path = _venv_bin(venv, name)
    return path if path.exists() else None


def running_in_docker() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return "docker" in cgroup or "containerd" in cgroup


def systemd_unit_present() -> bool:
    if sys.platform == "win32" or shutil.which("systemctl") is None:
        return False
    try:
        result = subprocess.run(
            ["systemctl", "cat", "dtxwiki.service"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def running_under_npm_launcher() -> bool:
    return os.environ.get(LAUNCHER_ENV) == "npm"


def running_under_pipx() -> bool:
    prefix = Path(sys.prefix).as_posix()
    return "/pipx/venvs/dtxwiki" in prefix or "pipx" in Path(sys.prefix).parts


def _auto_kind() -> str:
    # First, because it says how this very process was started. The launcher
    # runs dtxwiki from a uvx or `pipx run` cache environment that it, not the
    # operator, owns: uvx's has no pip, and `pipx run`'s looks like a pipx
    # install, so either other tier would upgrade the wrong copy.
    if running_under_npm_launcher():
        return "npm"
    if running_in_docker():
        return "docker"
    if systemd_unit_present():
        return "systemd"
    if running_under_pipx():
        return "pipx"
    return "venv"


def detect_install_method(*, override: str | None = None) -> InstallMethod:
    if override is not None and override not in VALID_KINDS and override != "auto":
        raise ValueError(
            f"unknown install method {override!r}; expected one of "
            f"{sorted(VALID_KINDS)} or 'auto'"
        )
    kind = _auto_kind() if (override is None or override == "auto") else override

    if kind == "docker":
        return InstallMethod("docker", None, None, (), None, ())

    if kind == "npm":
        # The launcher's package version picks the dtxwiki release; there is
        # nothing here for pip or pipx to replace.
        return InstallMethod("npm", None, None, (), None, ())

    if kind == "systemd":
        python = _venv_bin(SYSTEMD_VENV, "python")
        return InstallMethod(
            kind="systemd",
            venv_path=SYSTEMD_VENV,
            python_path=python,
            pip_argv=(str(python), "-m", "pip"),
            dtxwiki_bin=_bin_or_none(SYSTEMD_VENV, "dtxwiki"),
            services=SYSTEMD_SERVICES,
        )

    venv = Path(sys.prefix)
    python = Path(sys.executable)
    dtxwiki_bin = _bin_or_none(venv, "dtxwiki")
    if kind == "pipx":
        # pipx drives its own venv; upgrades go through the `pipx` CLI.
        return InstallMethod(
            kind="pipx",
            venv_path=venv,
            python_path=python,
            pip_argv=("pipx",),
            dtxwiki_bin=dtxwiki_bin,
            services=(),
        )

    return InstallMethod(
        kind="venv",
        venv_path=venv,
        python_path=python,
        pip_argv=(str(python), "-m", "pip"),
        dtxwiki_bin=dtxwiki_bin,
        services=(),
    )
