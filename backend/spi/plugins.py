"""Public contracts for explicitly enabled ``dtxwiki.plugins`` extensions.

Entry points export a :class:`DtxwikiPlugin` instance, or a zero-argument
factory returning one. Installing a distribution does not enable its plugin.
API and UI routes are mounted by core in a plugin-owned namespace.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import typer
    from fastapi import APIRouter, FastAPI
    from pydantic import BaseModel
    from sqlalchemy.orm import DeclarativeBase
    from starlette.types import ASGIApp

    from backend.spi.auth import AuthMethodPlugin


@dataclass(frozen=True)
class JobHandler:
    """Worker business handler; core continues to own queue transactions."""

    business: Callable[..., dict[str, Any]]
    completes_in_service: bool
    on_failure: Callable[..., None] | None = None


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


__all__ = ["DtxwikiPlugin", "JobHandler", "PluginMigrations", "PluginMount"]
