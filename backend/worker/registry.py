from __future__ import annotations

from backend.spi.plugins import JobHandler

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
