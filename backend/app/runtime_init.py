"""Short-lived serving initialization; private stdin is its lifetime lease."""

from __future__ import annotations

import _thread
import signal
import sys
from contextlib import ExitStack, redirect_stdout

# Keep the EOF/backstop machinery available before importing SQLAlchemy,
# migration machinery, cryptography, or the serving application.
from backend.app.runtime_protocol import ChildControl, ChildShutdownRequested


def main() -> None:
    control = ChildControl("init")

    def interrupted(_signum, _frame):
        raise ChildShutdownRequested("initialization stopped")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    control.set_interrupt(lambda: _thread.interrupt_main(signal.SIGTERM))
    try:
        packet = control.read_config()
        from backend.app.bootstrap import bootstrap_core
        from backend.app.runtime_credentials import prepare_serving_credentials
        from backend.app.runtime_resources import RuntimeResources
        from backend.app.settings import Settings

        settings = Settings(**packet["settings"])
        resources = RuntimeResources.for_settings(
            settings, managed=settings.worker_mode == "managed"
        )
        with ExitStack() as guards, redirect_stdout(sys.stderr):
            guards.enter_context(resources.acquire_activity())
            if settings.auto_bootstrap:
                guards.enter_context(resources.acquire_consumer())
            control.registered()
            control.wait_for_start()
            # Refuse invalid serving config before schema or key writes.
            settings.validate_for_startup()
            settings = prepare_serving_credentials(settings)
            bootstrap_core(settings)
            if control.stop_requested.is_set():
                raise ChildShutdownRequested("initialization stopped")
            key_path = settings.oauth_jwt_private_key_path
            control.emit(
                {
                    "status": "result",
                    "app_version_synced": True,
                    "config": {"oauth_jwt_private_key_path": key_path},
                }
            )
    except ChildShutdownRequested:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
