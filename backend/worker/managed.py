"""Private worker child protocol used by the CLI and desktop runtime.

Configuration travels over stdin, never in argv or a temporary settings file.
The same pipe stays open for the parent's lifetime: EOF requests shutdown even
when the parent was killed and could not run its cleanup code.
"""

from __future__ import annotations

import _thread
import json
import os
import signal
import sys
import threading
from contextlib import redirect_stdout

from backend.app.settings import Settings
from backend.worker.entry import _run_loop, _ShutdownSignals, _worker_runtime
from backend.worker.runtime import WorkerShutdownRequested

# The parent also bounds normal shutdown. This backstop applies when it died
# abruptly and cannot kill a child stuck in a third-party converter or SMTP.
PARENT_LOST_GRACE_SECONDS = 10.0
_parent_lost = threading.Event()


def _terminate_orphan_tree() -> None:
    if os.name != "nt" and os.getpgrp() == os.getpid():
        os.killpg(os.getpgrp(), signal.SIGKILL)
    os._exit(1)


def _watch_parent() -> None:
    for line in sys.stdin:
        if line.strip() == "shutdown":
            break
    else:
        _parent_lost.set()
    # Do not call shutdown.handle from this thread: its exception must run in
    # the worker's main thread so the dispatcher can return an interrupted job.
    backstop = threading.Timer(PARENT_LOST_GRACE_SECONDS, _terminate_orphan_tree)
    backstop.daemon = True
    backstop.start()
    _thread.interrupt_main(signal.SIGTERM)


def main() -> None:
    packet = json.loads(sys.stdin.readline())
    if packet.get("protocol") != 1:
        raise RuntimeError("unsupported managed worker protocol")
    settings = Settings(**packet["settings"])
    os.environ["_DTXWIKI_MANAGED_WORKER_PID"] = str(os.getpid())
    control_output = sys.stdout

    def ready() -> None:
        if shutdown.requested:
            raise WorkerShutdownRequested("parent requested shutdown")
        control_output.write('{"status":"ready"}\n')
        control_output.flush()

    shutdown = _ShutdownSignals()
    with (
        redirect_stdout(sys.stderr),
        shutdown.installed(),
    ):
        threading.Thread(
            target=_watch_parent, name="dtxwiki-worker-parent-watch", daemon=True
        ).start()
        try:
            with _worker_runtime(settings, skip_bootstrap=True):
                _run_loop(
                    settings,
                    shutdown,
                    workspace_id=None,
                    job_type=None,
                    poll_interval_seconds=1.0,
                    max_jobs=None,
                    idle_exit_after=None,
                    on_ready=ready,
                    report_idle=False,
                )
        except WorkerShutdownRequested:
            pass
        finally:
            if _parent_lost.is_set():
                _terminate_orphan_tree()


if __name__ == "__main__":
    main()
