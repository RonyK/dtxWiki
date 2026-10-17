"""Probe the real Windows updater handoff through interpreter/console launchers.

Run with native Windows Python from a checkout containing its dependencies.
This makes a temporary venv and SQLite database; it neither installs packages
nor runs an update. A temporary import shim lets an existing distlib pip.exe
exercise the handoff without executing pip. Metadata travels only over private
process pipes/environment and is omitted from the emitted proof records.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import threading
import venv
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend.app.runtime_resources import RuntimeResources  # noqa: E402
from backend.services.ops import runtime_lock  # noqa: E402
from backend.services.ops.maintenance_handoff import (  # noqa: E402
    HANDOFF_ENV,
    HandoffError,
    finalizer_handoff,
    verify_finalizer_handoff,
)

_PATHS_ENV = "_DTXWIKI_PROBE_ACTIVITY_PATHS"
_CHILD_ENV = "_DTXWIKI_PROBE_CHILD"


def _child() -> None:
    paths = tuple(Path(path) for path in json.loads(os.environ[_PATHS_ENV]))
    result = {"actual_pid": os.getpid(), "actual_ppid": os.getppid()}
    try:
        result["inherited"] = verify_finalizer_handoff(paths)
    except HandoffError:
        result["error"] = "HandoffError"
    result["metadata_consumed"] = HANDOFF_ENV not in os.environ
    # A successful handoff must leave the updater's actual EX guards intact.
    # The finalizer does not reacquire them; an independent SH still conflicts.
    if result.get("inherited"):
        blocked = []
        for path in paths:
            try:
                lock = runtime_lock.acquire_path(path, "shared", strict=True)
            except runtime_lock.RuntimeLockHeldError:
                blocked.append(True)
            else:
                lock.release()
                blocked.append(False)
        result["parent_exclusive_guards_intact"] = all(blocked) and bool(blocked)
    print(json.dumps(result), flush=True)


def _issuer() -> None:
    paths = tuple(Path(path) for path in json.loads(os.environ[_PATHS_ENV]))
    from backend.app.runtime_resources import acquire_paths

    with acquire_paths(paths, "exclusive") as held, finalizer_handoff(held) as value:
        # The caller captures this stdout privately and never prints metadata.
        print(json.dumps({"metadata": value, "pid": os.getpid()}), flush=True)
        sys.stdin.readline()


def _run_child(
    command: list[str],
    environment: dict[str, str],
    paths: tuple[Path, ...],
    metadata: str | None,
    label: str,
    *,
    expected: str,
) -> dict:
    environment = {**environment, _PATHS_ENV: json.dumps(list(map(str, paths)))}
    environment.pop(HANDOFF_ENV, None)
    if metadata is not None:
        environment[HANDOFF_ENV] = metadata
    process = subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    if process.returncode != 0:
        raise RuntimeError(f"{label}: child exited {process.returncode}: {stderr}")
    result = json.loads(stdout)
    result["launcher_pid"] = process.pid
    if not result["metadata_consumed"]:
        raise RuntimeError(f"{label}: private handoff metadata survived verification")
    if expected == "accepted":
        if result.get("inherited") is not True:
            raise RuntimeError(f"{label}: valid parent handoff was rejected")
        if not result["parent_exclusive_guards_intact"]:
            raise RuntimeError(f"{label}: parent released an activity guard")
    elif expected == "legacy":
        if result.get("inherited") is not False:
            raise RuntimeError(f"{label}: missing metadata did not select legacy path")
    elif result.get("error") != "HandoffError":
        raise RuntimeError(f"{label}: unsafe handoff was accepted")
    print(json.dumps({"case": label, **result}), flush=True)
    return result


def main() -> None:
    if os.name != "nt":
        raise SystemExit("Run with a native Windows Python interpreter")
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        _child()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--issuer":
        _issuer()
        return

    console_candidates = (
        Path(sys.prefix) / "Scripts" / "pip.exe",
        Path(getattr(sys, "_base_executable", sys.executable)).parent
        / "Scripts"
        / "pip.exe",
    )
    console = next((path for path in console_candidates if path.is_file()), None)
    if console is None:
        raise RuntimeError("An existing native distlib pip.exe is required")
    base_executable = str(getattr(sys, "_base_executable", sys.executable))

    previous_root = os.environ.get("DTXWIKI_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory(prefix="dtxwiki-native-handoff-") as scratch:
            root = Path(scratch)
            os.environ["DTXWIKI_DATA_DIR"] = str(root / "data")
            database = root / "db.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE probe_marker (value INTEGER)")
                connection.commit()
            finally:
                connection.close()
            resources = RuntimeResources.for_settings(
                SimpleNamespace(database_url=f"sqlite:///{database}")
            )
            venv_root = root / "venv"
            venv.EnvBuilder(with_pip=False).create(venv_root)
            child_script = root / "child.py"
            child_script.write_text(
                "import runpy,sys\n"
                "sys.argv = [sys.argv[0], '--child']\n"
                f"probe = {str(Path(__file__).resolve())!r}\n"
                "runpy.run_path(probe, run_name='__main__')\n",
                encoding="utf-8",
            )
            # Only this subprocess import path changes. The installed pip
            # package and its executable remain untouched.
            package = root / "pip" / "_internal" / "cli"
            package.mkdir(parents=True)
            for directory in (root / "pip", package.parent, package):
                (directory / "__init__.py").write_text("", encoding="utf-8")
            (package / "main.py").write_text(
                "def main():\n"
                "    import os, runpy\n"
                f"    probe = os.environ[{_CHILD_ENV!r}]\n"
                "    runpy.run_path(probe, run_name='__main__')\n",
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    (str(root), str(ROOT), sysconfig.get_paths()["purelib"])
                ),
                _CHILD_ENV: str(child_script),
            }
            commands = (
                ("base", [base_executable, str(child_script)]),
                (
                    "venv-redirector",
                    [str(venv_root / "Scripts" / "python.exe"), str(child_script)],
                ),
                ("distlib-console", [str(console)]),
            )
            with resources.acquire_activity("exclusive") as held:
                for label, command in commands:
                    with finalizer_handoff(held) as metadata:
                        result = _run_child(
                            command,
                            environment,
                            held.paths,
                            metadata,
                            label,
                            expected="accepted",
                        )
                    if label == "base":
                        if result["actual_pid"] != result["launcher_pid"]:
                            raise RuntimeError("base: unexpected launcher intermediary")
                    elif (
                        result["actual_pid"] == result["launcher_pid"]
                        or result["actual_ppid"] == os.getpid()
                    ):
                        raise RuntimeError(
                            f"{label}: launcher boundary was not exercised"
                        )
                for label, requested in (
                    (
                        "wrong-root",
                        tuple(
                            root / "other-root" / ".runtime.lock"
                            if path == resources.root / ".runtime.lock"
                            else path
                            for path in held.paths
                        ),
                    ),
                    (
                        "wrong-database",
                        tuple(
                            path
                            if path == resources.root / ".runtime.lock"
                            else root / "other-db.activity.lock"
                            for path in held.paths
                        ),
                    ),
                ):
                    with finalizer_handoff(held) as metadata:
                        _run_child(
                            commands[0][1],
                            environment,
                            requested,
                            metadata,
                            label,
                            expected="rejected",
                        )
                _run_child(
                    commands[0][1],
                    environment,
                    held.paths,
                    "invalid",
                    "malformed",
                    expected="rejected",
                )
                _run_child(
                    commands[0][1],
                    environment,
                    held.paths,
                    None,
                    "legacy-no-metadata",
                    expected="legacy",
                )
                with finalizer_handoff(held) as stale:
                    pass
                _run_child(
                    commands[0][1],
                    environment,
                    held.paths,
                    stale,
                    "closed-parent-context",
                    expected="rejected",
                )

            # Obtain valid metadata from an actual lock-owning parent, kill it
            # before finalization starts, then prove its stale marker is refused.
            issuer = subprocess.Popen(
                [base_executable, str(Path(__file__).resolve()), "--issuer"],
                env={
                    **environment,
                    _PATHS_ENV: json.dumps(list(map(str, resources.activity_paths))),
                },
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            ready = queue.Queue()
            reader = threading.Thread(
                target=lambda: ready.put(issuer.stdout.readline()), daemon=True
            )
            reader.start()
            try:
                record = json.loads(ready.get(timeout=15))
            finally:
                issuer.kill()
                issuer.wait(timeout=5)
                issuer.communicate(timeout=5)
                reader.join(timeout=5)
            if record["pid"] != issuer.pid:
                raise RuntimeError("dead-parent probe killed a launcher, not the owner")
            with resources.acquire_activity("exclusive") as held:
                _run_child(
                    commands[0][1],
                    environment,
                    held.paths,
                    record["metadata"],
                    "dead-parent-before-finalization",
                    expected="rejected",
                )
    finally:
        if previous_root is None:
            os.environ.pop("DTXWIKI_DATA_DIR", None)
        else:
            os.environ["DTXWIKI_DATA_DIR"] = previous_root


if __name__ == "__main__":
    main()
