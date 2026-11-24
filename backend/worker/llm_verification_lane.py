"""The worker's lane for LLM connection verifications.

Verifications wait on a remote provider for up to tens of seconds, so they run
on their own small thread pool instead of the job loop: a slow provider never
holds up Library, embedding or maintenance jobs, and those never hold up a
verification someone is waiting for on the settings screen.
"""

from __future__ import annotations

import logging
import threading
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from backend.app.settings import Settings
from backend.services.llm_verification_service import (
    LLMVerificationService,
    WorkspaceSessions,
)

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 2
_POLL_SECONDS = 1.0


class LLMVerificationLane:
    def __init__(
        self,
        service: LLMVerificationService,
        *,
        control: sessionmaker[Session],
        workspace_sessions: WorkspaceSessions,
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        self._service = service
        self._control = control
        self._workspace_sessions = workspace_sessions
        self._concurrency = concurrency
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._running: set[threading.Thread] = set()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="llm-verification-lane", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        # Calls in flight run on daemon threads and are abandoned at exit:
        # their lease runs out and they end as outcome_unknown instead of
        # holding the worker's shutdown for a slow provider.
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def tick(self) -> None:
        """Retire lost operations, then start as many queued ones as fit."""
        self._service.expire_stale(self._control)
        self._running = {thread for thread in self._running if thread.is_alive()}
        free = self._concurrency - len(self._running)
        if free <= 0 or self._stop.is_set():
            return
        for operation_id, token in self._service.claim(self._control, limit=free):
            thread = threading.Thread(
                target=self._execute,
                args=(operation_id, token),
                name="llm-verification",
                daemon=True,
            )
            self._running.add(thread)
            thread.start()

    def _execute(self, operation_id: UUID, token: UUID) -> None:
        try:
            self._service.execute(
                self._control, self._workspace_sessions, operation_id, token
            )
        except Exception:
            # The lease expires and the operation ends as outcome_unknown.
            logger.exception("LLM verification %s failed", operation_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("LLM verification lane tick failed")
            self._stop.wait(self._poll_seconds)


def build_llm_verification_lane(
    settings: Settings,
    *,
    control: sessionmaker[Session],
    workspace_sessions: WorkspaceSessions,
) -> LLMVerificationLane:
    from backend.app.plugins import load_plugins
    from backend.llm.adapter_registry import LLMAdapterRegistry
    from backend.llm.adapters import build_builtin_llm_adapters
    from backend.services.llm_connection_service import build_llm_connection_service

    registry = LLMAdapterRegistry(
        build_builtin_llm_adapters(), load_plugins(settings).llm_adapters()
    )
    connections = build_llm_connection_service(settings, registry)
    service = LLMVerificationService(
        connections=connections, registry=registry, vault=connections.vault
    )
    return LLMVerificationLane(
        service, control=control, workspace_sessions=workspace_sessions
    )


__all__ = ["LLMVerificationLane", "build_llm_verification_lane"]
