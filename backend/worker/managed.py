"""Private worker child with an independent activity lease and start barrier."""

from __future__ import annotations

import _thread
import os
import signal
import sys
from contextlib import redirect_stdout

from backend.app.runtime_protocol import ChildControl, ChildShutdownRequested


def main() -> None:
    # Install EOF handling before importing DB/providers/native converters.
    control = ChildControl("worker")
    try:
        packet = control.read_config()
        with redirect_stdout(sys.stderr):
            from backend.app.runtime_resources import RuntimeResources
            from backend.app.settings import Settings
            from backend.worker.entry import (
                _run_loop,
                _ShutdownSignals,
                _worker_runtime,
            )
            from backend.worker.runtime import WorkerShutdownRequested

            settings = Settings(**packet["settings"])
            resources = RuntimeResources.for_settings(settings, managed=True)
            os.environ.update(resources.export_environment())
            os.environ["_DTXWIKI_MANAGED_WORKER_PID"] = str(os.getpid())
            shutdown = _ShutdownSignals()
            with resources.acquire_activity(), shutdown.installed():
                control.set_interrupt(lambda: _thread.interrupt_main(signal.SIGTERM))
                control.registered()
                control.wait_for_start()

                def ready() -> None:
                    if shutdown.requested or control.stop_requested.is_set():
                        raise WorkerShutdownRequested("parent requested shutdown")
                    control.emit({"status": "ready"})

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
    except ChildShutdownRequested:
        pass


if __name__ == "__main__":
    main()
