"""Read-only inventory of clients that support HTTP MCP configuration."""

import shutil
from pathlib import Path

from backend.cli.mcp_http_register import (
    config_path_for_client,
    local_http_client_ids,
)

_CLIENT_EXECUTABLES = {
    "antigravity": "agy",
    "claude-code": "claude",
    "codex": "codex",
    "gemini-cli": "gemini",
}


def detect_clients() -> list[dict[str, str | bool]]:
    """Return every supported HTTP client and whether it is installed locally."""

    clients: list[dict[str, str | bool]] = []
    for name in local_http_client_ids():
        config_path = config_path_for_client(name)
        clients.append(
            {
                "name": name,
                "detected": _is_detected(
                    config_path,
                    executable=_CLIENT_EXECUTABLES.get(name),
                ),
                "config_path": str(config_path),
            }
        )
    return clients


def _is_detected(path: Path, *, executable: str | None) -> bool:
    return path.is_file() or (
        executable is not None and shutil.which(executable) is not None
    )
