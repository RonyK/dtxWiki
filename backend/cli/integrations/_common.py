"""Shared filesystem primitives for HTTP MCP client configuration."""

import os
import shutil
import stat
import tempfile
from pathlib import Path

# MCP client configuration can carry a bearer token, so files written for a
# client -- and their backups -- are readable by their owner only.
OWNER_ONLY_FILE_MODE = 0o600


def atomic_write_text(path: Path, content: str, *, mode: int | None = None) -> None:
    """Replace ``path`` with ``content`` atomically.

    ``mode`` forces the resulting permissions; without it an existing file
    keeps its mode (operator files such as a Caddyfile another service reads)
    and a new file gets the private temporary file's 0600.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = path.stat().st_mode if path.exists() else None
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = Path(handle.name)
        if mode is not None:
            os.chmod(tmp_path, mode)
        elif existing_mode is not None:
            os.chmod(tmp_path, existing_mode)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def restrict_to_owner(path: Path) -> None:
    """Drop group/other access from ``path`` (no-op on Windows)."""

    if os.name == "nt":
        return
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        os.chmod(path, OWNER_ONLY_FILE_MODE)


def backup_once(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup
