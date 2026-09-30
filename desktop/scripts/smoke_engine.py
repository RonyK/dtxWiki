"""Smoke-test the frozen desktop engine without touching the user's wiki."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _default_engine() -> Path:
    filename = "dtxwiki-engine.exe" if sys.platform == "win32" else "dtxwiki-engine"
    return ROOT / "desktop" / "build" / "engine" / filename


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until_ready(origin: str, process: subprocess.Popen, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"engine exited before readiness with code {process.returncode}"
            )
        try:
            with urllib.request.urlopen(
                f"{origin}/health/ready", timeout=2
            ) as response:
                payload = json.load(response)
                if response.status == 200 and payload.get("status") == "ready":
                    return
                last_error = f"HTTP {response.status}: {payload}"
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = str(error)
        time.sleep(0.25)
    raise TimeoutError(f"engine readiness timed out: {last_error}")


def _check_notices(engine: Path) -> None:
    """The installer redistributes the frozen runtime; its notices must ship."""

    notices = engine.parent / "licenses" / "PYTHON_THIRD_PARTY_NOTICES.txt"
    if not notices.is_file():
        raise FileNotFoundError(notices)
    text = notices.read_text(encoding="utf-8")
    listed = [line for line in text.splitlines() if line.startswith("- ")]
    if not listed or not listed[0].startswith("- Python ") or "- fastapi==" not in text:
        raise RuntimeError(f"{notices} does not list the frozen Python runtime")


def smoke_engine(engine: Path, timeout: int = 60) -> None:
    engine = engine.resolve()
    if not engine.is_file():
        raise FileNotFoundError(engine)
    _check_notices(engine)
    port = _available_port()
    with tempfile.TemporaryDirectory(prefix="dtxwiki-desktop-smoke-") as data_dir:
        environment = os.environ.copy()
        environment["DTXWIKI_DATA_DIR"] = data_dir
        environment["HOME"] = data_dir
        environment["USERPROFILE"] = data_dir
        environment["APPDATA"] = str(Path(data_dir) / "AppData" / "Roaming")
        bridge = subprocess.run(
            [str(engine), "mcp-detect"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout,
            check=True,
        )
        inventory = json.loads(bridge.stdout)
        client_ids = {client["id"] for client in inventory.get("clients", [])}
        expected_client_ids = {
            "claude-code",
            "codex",
            "gemini-cli",
            "antigravity",
        }
        if (
            not inventory.get("ok")
            or client_ids != expected_client_ids
            or any(
                "local" in client or "remote" in client
                for client in inventory.get("clients", [])
            )
        ):
            raise RuntimeError("frozen HTTP MCP bridge inventory is invalid")
        process = subprocess.Popen(
            [str(engine), "--host", "127.0.0.1", "--port", str(port)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        try:
            _wait_until_ready(f"http://127.0.0.1:{port}", process, timeout)
            assert process.stdin is not None
            process.stdin.write("shutdown\n")
            process.stdin.flush()
            process.wait(timeout=10)
            if process.returncode != 0:
                raise RuntimeError(f"engine shutdown returned {process.returncode}")
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            assert process.stdout is not None
            output = process.stdout.read()
            if output:
                print(output, file=sys.stderr)
            raise
        finally:
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
    print(f"desktop engine smoke passed on 127.0.0.1:{port}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, default=_default_engine())
    parser.add_argument("--timeout", type=int, default=60)
    arguments = parser.parse_args()
    smoke_engine(arguments.engine, timeout=arguments.timeout)


if __name__ == "__main__":
    main()
