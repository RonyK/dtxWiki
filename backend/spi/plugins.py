"""Public contracts for explicitly enabled ``dtxwiki.plugins`` extensions.

Entry points export a :class:`DtxwikiPlugin` instance, or a zero-argument
factory returning one. Installing a distribution does not enable its plugin.
API and UI routes are mounted by core in a plugin-owned namespace.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

if TYPE_CHECKING:
    import typer
    from fastapi import APIRouter, FastAPI
    from pydantic import BaseModel
    from sqlalchemy.orm import DeclarativeBase
    from starlette.types import ASGIApp

    from backend.spi.auth import AuthMethodPlugin


type JobValue = (
    str | int | float | bool | None | tuple["JobValue", ...] | Mapping[str, "JobValue"]
)


def _freeze(value: object) -> JobValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    raise ValueError("worker payload must contain finite JSON values")


def _thaw(value: JobValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class JobInput:
    job_id: UUID
    workspace_id: UUID
    job_type: str
    trigger_type: str
    payload: Mapping[str, JobValue]
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise ValueError("worker payload must be an object")
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True)
class WorkerContext:
    """Invocation facts, without core ORM or queue mutation capabilities.

    Plugins are trusted in-process extensions and use their own configured
    resources. This contract reduces coupling; it is not process isolation.
    """

    now: datetime


@dataclass(frozen=True)
class JobResult:
    detail: Mapping[str, JobValue]

    def __post_init__(self) -> None:
        if not isinstance(self.detail, Mapping):
            raise ValueError("worker result must be an object")
        object.__setattr__(self, "detail", _freeze(self.detail))

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self.detail)


class PluginJobError(RuntimeError):
    """A stable diagnostic code, never a remote exception message."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            raise ValueError("worker error code is invalid")
        self.code = code
        super().__init__(code)


class RetryableJobError(PluginJobError):
    """Core retries this failure within its configured attempt budget."""


class PermanentJobError(PluginJobError):
    """Core marks this failure terminal on its first attempt."""


@dataclass(frozen=True)
class JobFailure:
    code: str
    retryable: bool


@dataclass(frozen=True)
class JobHandler:
    """Typed business callback; core owns claim, completion and failure."""

    business: Callable[[WorkerContext, JobInput], JobResult]
    on_failure: Callable[[WorkerContext, JobInput, JobFailure], None] | None = None


@dataclass(frozen=True)
class PluginMount:
    """Routes relative to ``/api/v1/ext/<name>`` and ``/ext/<name>/``."""

    api_router: APIRouter | None = None
    ui_app: ASGIApp | None = None


@dataclass(frozen=True)
class PluginMigrations:
    """An independent Alembic ``versions/`` directory and model metadata.

    Core supplies the migration environment and owns the version table. The
    plugin's models must use their own DeclarativeBase, without foreign keys
    outside their metadata. Current core supports control-plane tables in
    the single-database topology. Workspace-scoped tables are rejected until
    blocker reporting and workspace deletion cover plugin data, including
    disabled plugins. Shard-fleet migration is separate.
    """

    script_location: Path
    base: type[DeclarativeBase]
    plane: Literal["control"]


@dataclass(frozen=True)
class DtxwikiPlugin:
    """A trusted extension, loaded only from the operator's allowlist.

    Names are lowercase Python identifiers of at most 47 characters.
    Compatibility uses PEP 440
    specifiers. Settings are parsed from DTXWIKI_PLUGIN_<NAME>_<FIELD> before
    configure runs. Hooks share the configured instance for that Settings
    object; hooks must never mutate core's model metadata or global routes.
    """

    name: str
    version: str
    requires_core: str
    requires_spi: str
    settings_model: type[BaseModel] | None = None
    configure: Callable[[BaseModel | None], None] | None = None
    auth_methods: Callable[[], Iterable[AuthMethodPlugin]] | None = None
    mount: Callable[[], PluginMount] | None = None
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[Any]] | None = None
    worker_handlers: Callable[[], Mapping[str, JobHandler]] | None = None
    cli: typer.Typer | None = None
    migrations: PluginMigrations | None = None


__all__ = [
    "DtxwikiPlugin",
    "JobFailure",
    "JobHandler",
    "JobInput",
    "JobResult",
    "JobValue",
    "PermanentJobError",
    "PluginJobError",
    "PluginMigrations",
    "PluginMount",
    "RetryableJobError",
    "WorkerContext",
]
