"""systemd service control shared by install (setup) and update/rollback.

Command *building* is separated from *execution* so ``setup production`` can run
the argv through its dry-run-aware ``Runner`` while ``update``/``rollback``
execute directly — both share one source of truth for the service names.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence

from backend.services.ops.install_method import SYSTEMD_SERVICES

__all__ = [
    "SYSTEMD_SERVICES",
    "has_systemd",
    "systemctl_argv",
    "run_systemctl",
]


def has_systemd() -> bool:
    return sys.platform != "win32" and shutil.which("systemctl") is not None


def systemctl_argv(
    action: str,
    services: Sequence[str] = SYSTEMD_SERVICES,
    *,
    extra_flags: Sequence[str] = (),
) -> list[str]:
    """Build a ``systemctl <action> [flags] <services...>`` argv."""

    return ["systemctl", action, *extra_flags, *services]


def run_systemctl(
    action: str,
    services: Sequence[str] = SYSTEMD_SERVICES,
    *,
    extra_flags: Sequence[str] = (),
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Execute a systemctl action (update/rollback path)."""

    return subprocess.run(
        systemctl_argv(action, services, extra_flags=extra_flags),
        capture_output=True,
        text=True,
        check=check,
    )
