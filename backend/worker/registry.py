from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobHandler:
    """Internal built-in ABI; public plugin callbacks use backend.spi types."""

    business: Callable[..., dict[str, Any]]
    completes_in_service: bool
    on_failure: Callable[..., None] | None = None


_HANDLERS: dict[str, JobHandler] = {}


def register(job_type: str, handler: JobHandler) -> None:
    if not job_type:
        raise ValueError("job_type must not be empty")
    if job_type in _HANDLERS:
        raise ValueError(f"worker job type already registered: {job_type}")
    _HANDLERS[job_type] = handler


def get_handler(job_type: str) -> JobHandler | None:
    return _HANDLERS.get(job_type)


def registered_handlers() -> dict[str, JobHandler]:
    return dict(_HANDLERS)
