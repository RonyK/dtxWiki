"""Native Windows base/venv containment probe; no package installation needed.

This checks the real interpreter in the runtime's specific job, because a venv
redirector has a different PID and an inherited outer job proves nothing about
the runtime's own kill-on-close scope. It does not substitute for pipx/uvx CLI
installation or frozen installer acceptance.
"""

from __future__ import annotations

import ctypes
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "backend" / "app" / "process_tree.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("dtxwiki_job_probe", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    if os.name != "nt":
        raise SystemExit("Run with a native Windows Python interpreter")
    module = _load_module()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.IsProcessInJob.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    kernel.IsProcessInJob.restype = ctypes.c_int
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int

    def open_process(pid):
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def in_job(handle, job):
        result = ctypes.c_int()
        if not kernel.IsProcessInJob(handle, job, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(result.value)

    script = (
        "import importlib.util,json,os,sys,threading;"
        f"spec=importlib.util.spec_from_file_location('child_job',{str(SOURCE)!r});"
        "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);"
        "module.join_runtime_job();"
        "print(json.dumps({'actual':os.getpid(),'parent':os.getppid()}),flush=True);"
        "threading.Event().wait()"
    )
    with tempfile.TemporaryDirectory(prefix="dtxwiki-native-job-") as scratch:
        venv.EnvBuilder(with_pip=False).create(scratch)
        executables = (
            ("base", sys.executable, script),
            ("venv", str(Path(scratch) / "Scripts" / "python.exe"), script),
            (
                "venv-host-job",
                str(Path(scratch) / "Scripts" / "python.exe"),
                script.replace(
                    "module.join_runtime_job();", "module.guard_managed_parent();"
                ),
            ),
        )
        for kind, executable, child_script in executables:
            tree = module.prepare_worker_tree()
            process = subprocess.Popen(
                [executable, "-c", child_script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, **tree.environment()},
                text=True,
            )
            handles = []
            try:
                actual = json.loads(process.stdout.readline())
                tree.attach(process.pid)
                # Observe this runtime's job, not any inherited PowerShell job.
                job = inspect.getclosurevars(tree._close).nonlocals["job"]
                handles = [open_process(pid) for pid in (process.pid, actual["actual"])]
                membership = [in_job(handle, job) for handle in handles]
                if membership != [True, True]:
                    raise RuntimeError(f"{kind}: launcher/interpreter outside job")
                tree.close()
                for handle in handles:
                    if kernel.WaitForSingleObject(handle, 5_000) != 0:
                        raise RuntimeError(
                            f"{kind}: job close did not terminate a process"
                        )
                process.wait(timeout=5)
                print(
                    json.dumps(
                        {
                            "kind": kind,
                            "launcher_pid": process.pid,
                            "actual": actual,
                            "specific_job_membership": membership,
                            "job_close_confirmed_exit": True,
                        }
                    ),
                    flush=True,
                )
            finally:
                tree.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for handle in handles:
                    kernel.CloseHandle(handle)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

        # A separate owner must be killed: explicit close alone is not proof
        # that a disappearing runtime leaves no live redirector/interpreter.
        parent_script = (
            "import importlib.util,json,os,subprocess,threading;"
            f"spec=importlib.util.spec_from_file_location('owner_job',{str(SOURCE)!r});"
            "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);"
            "tree=module.prepare_worker_tree();"
            f"child=subprocess.Popen([{executables[1][1]!r},'-c',{script!r}],"
            "stdin=subprocess.PIPE,stdout=subprocess.PIPE,"
            "env={**os.environ,**tree.environment()},text=True);"
            "actual=json.loads(child.stdout.readline());tree.attach(child.pid);"
            "print(json.dumps({'launcher_pid':child.pid,'actual':actual}),flush=True);"
            "threading.Event().wait()"
        )
        owner = subprocess.Popen(
            [sys.executable, "-c", parent_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        handles = []
        try:
            observed = json.loads(owner.stdout.readline())
            handles = [
                open_process(observed["launcher_pid"]),
                open_process(observed["actual"]["actual"]),
            ]
            owner.kill()
            owner.wait(timeout=5)
            for handle in handles:
                if kernel.WaitForSingleObject(handle, 5_000) != 0:
                    raise RuntimeError("owner death left a live venv process")
            print(
                json.dumps(
                    {"kind": "venv-owner-death", **observed, "confirmed_exit": True}
                ),
                flush=True,
            )
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=5)
            for handle in handles:
                kernel.CloseHandle(handle)
            owner.stdout.close()
            owner.stderr.close()


if __name__ == "__main__":
    main()
