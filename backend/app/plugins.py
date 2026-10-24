"""Allowlisted plugin discovery and composition for every runtime entry point."""

from __future__ import annotations

import os
import re
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import event

from backend.spi.plugins import DtxwikiPlugin, PluginMigrations, PluginMount

if TYPE_CHECKING:
    import typer
    from fastapi import FastAPI

    from backend.app.settings import Settings

# PostgreSQL identifiers have a 63-byte limit; the version-table prefix uses
# 16 bytes, leaving 47 ASCII bytes for a collision-free plugin name.
_NAME = re.compile(r"[a-z][a-z0-9_]{0,46}\Z")
_PREFIX = "DTXWIKI_PLUGIN_"


class PluginConfigurationError(RuntimeError):
    """A plugin selection, contract, or configuration cannot be served."""


@dataclass(frozen=True)
class PluginRegistry:
    plugins: tuple[DtxwikiPlugin, ...] = ()
    environment_keys: frozenset[str] = frozenset()
    _selection: tuple[str, str] | None = None

    def auth_methods(self) -> tuple:
        return tuple(
            method
            for plugin in self.plugins
            if plugin.auth_methods is not None
            for method in plugin.auth_methods()
        )

    def capabilities(self) -> tuple[str, ...]:
        return tuple(plugin.name for plugin in self.plugins)

    def mount(self, app: FastAPI) -> None:
        for plugin in self.plugins:
            if plugin.mount is None:
                continue
            mount = plugin.mount()
            if not isinstance(mount, PluginMount):
                raise PluginConfigurationError(
                    f"plugin {plugin.name}: mount must return PluginMount"
                )
            if mount.api_router is not None:
                app.include_router(
                    mount.api_router, prefix=f"/api/v1/ext/{plugin.name}"
                )
            if mount.ui_app is not None:
                app.mount(f"/ext/{plugin.name}", mount.ui_app, name=plugin.name)

    @asynccontextmanager
    async def lifespan(self, app: FastAPI):
        async with AsyncExitStack() as stack:
            for plugin in self.plugins:
                if plugin.lifespan is not None:
                    await stack.enter_async_context(plugin.lifespan(app))
            yield

    def worker_handlers(self) -> dict:
        # Importing runtime installs the built-in handlers before collision
        # checks, including when a web-only process loads an extension.
        if any(plugin.worker_handlers is not None for plugin in self.plugins):
            from backend.worker import runtime  # noqa: F401

        from backend.worker.registry import JobHandler, get_handler

        handlers = {}
        for plugin in self.plugins:
            if plugin.worker_handlers is None:
                continue
            for name, handler in plugin.worker_handlers().items():
                if (
                    not name
                    or name in handlers
                    or get_handler(name) is not None
                    or not isinstance(handler, JobHandler)
                ):
                    raise PluginConfigurationError(
                        f"plugin {plugin.name}: invalid or duplicate "
                        f"worker handler {name!r}"
                    )
                handlers[name] = handler
        return handlers

    def install_cli(self, app: typer.Typer) -> None:
        names = {group.name for group in app.registered_groups}
        names.update(
            item.name or item.callback.__name__.replace("_", "-")
            for item in app.registered_commands
        )
        for plugin in self.plugins:
            if plugin.cli is None:
                continue
            if plugin.name in names:
                raise PluginConfigurationError(
                    f"plugin {plugin.name}: CLI command name is already registered"
                )
            app.add_typer(plugin.cli, name=plugin.name)
            names.add(plugin.name)

    def migrate(self, settings: Settings, *, allow_upgrade: bool) -> None:
        """Run or verify independent chains after core's schema is current."""
        from backend.app.db import make_engine
        from backend.services.ops.migration_preflight import make_non_mutating_engine

        chains = [plugin for plugin in self.plugins if plugin.migrations is not None]
        if not chains:
            return
        engine = (
            make_engine(settings.database_url)
            if allow_upgrade
            else make_non_mutating_engine(settings.database_url)
        )
        sqlite_upgrade = allow_upgrade and engine.dialect.name == "sqlite"
        if sqlite_upgrade:
            # pysqlite otherwise autocommits a revision's leading DDL before
            # the first stamp INSERT. A failed plugin must roll back its DDL.
            @event.listens_for(engine, "connect")
            def _explicit_transactions(dbapi_connection, _record):
                dbapi_connection.isolation_level = None
                dbapi_connection.execute("PRAGMA foreign_keys=OFF")

            @event.listens_for(engine, "begin")
            def _begin(connection):
                connection.exec_driver_sql("BEGIN IMMEDIATE")

        try:
            for plugin in chains:
                migrations = plugin.migrations
                assert migrations is not None
                config = Config()
                config.set_main_option("path_separator", "os")
                config.set_main_option(
                    "script_location",
                    str(Path(__file__).with_name("plugin_migrations")),
                )
                config.set_main_option(
                    "version_locations",
                    str(Path(migrations.script_location) / "versions"),
                )
                script = ScriptDirectory.from_config(config)
                head = script.get_current_head()
                if head is None:
                    raise PluginConfigurationError(
                        f"plugin {plugin.name}: migration chain has no head"
                    )
                table = f"alembic_version_{plugin.name}"
                with engine.begin() as connection:
                    current = MigrationContext.configure(
                        connection, opts={"version_table": table}
                    ).get_current_heads()
                    if current == (head,):
                        continue
                    if not allow_upgrade:
                        raise PluginConfigurationError(
                            f"plugin {plugin.name}: migrations are not current; "
                            "back up the database and run 'dtxwiki bootstrap'"
                        )
                    # Unknown plugin revisions are never restamped or overwritten.
                    for revision in current:
                        script.get_revision(revision)
                    config.attributes.update(
                        connection=connection,
                        version_table=table,
                        target_metadata=migrations.base.metadata,
                    )
                    violations_before = (
                        _foreign_key_violations(connection) if sqlite_upgrade else None
                    )
                    command.upgrade(config, "head")
                    if sqlite_upgrade and (
                        _foreign_key_violations(connection) - violations_before
                    ):
                        raise PluginConfigurationError(
                            f"plugin {plugin.name}: migration created "
                            "foreign key violations"
                        )
        finally:
            engine.dispose()


def _foreign_key_violations(connection) -> Counter:
    return Counter(
        (row[0], row[2])
        for row in connection.exec_driver_sql("PRAGMA foreign_key_check")
    )


def load_plugins(settings: Settings) -> PluginRegistry:
    """Discover metadata without importing disabled entry points.

    The same Settings instance retains its registry so web bootstrap and the
    web app cannot configure two plugin instances with different state.
    """
    cached = getattr(settings, "_plugin_registry", None)
    selection = (settings.plugins, settings.app_version)
    if cached is not None and cached._selection == selection:
        _validate_environment(cached.environment_keys)
        return cached
    raw = settings.plugins.strip()
    names = [name.strip() for name in raw.split(",")] if raw else []
    if any(not _NAME.fullmatch(name) for name in names):
        raise PluginConfigurationError(
            "DTXWIKI_PLUGINS requires lowercase plugin names (1..47 characters)"
        )
    duplicate = [name for name, count in Counter(names).items() if count > 1]
    if duplicate:
        raise PluginConfigurationError(
            f"duplicate enabled plugins: {', '.join(duplicate)}"
        )
    entries = metadata.entry_points(group="dtxwiki.plugins") if names else ()
    selected = {}
    for entry in entries:
        if entry.name not in names:
            continue
        if entry.name in selected:
            raise PluginConfigurationError(
                f"duplicate plugin entry point: {entry.name}"
            )
        selected[entry.name] = entry
    missing = set(names) - selected.keys()
    if missing:
        raise PluginConfigurationError(
            f"enabled plugins are not installed: {', '.join(sorted(missing))}"
        )

    plugins = []
    allowed_keys: set[str] = set()
    configurations = []
    model_tables: set[str] = set()
    for name in names:
        entry_object = selected[name].load()
        plugin = entry_object() if callable(entry_object) else entry_object
        if not isinstance(plugin, DtxwikiPlugin) or plugin.name != name:
            raise PluginConfigurationError(
                f"plugin {name}: entry point must export a matching DtxwikiPlugin"
            )
        _validate_compatibility(plugin, settings.app_version)
        _validate_migrations(plugin)
        if plugin.migrations is not None:
            tables = set(plugin.migrations.base.metadata.tables)
            if model_tables.intersection(tables):
                raise PluginConfigurationError(
                    f"plugin {name}: model table collides with another plugin"
                )
            model_tables.update(tables)
        config, keys = _settings_for(plugin)
        if allowed_keys.intersection(keys):
            raise PluginConfigurationError(
                f"plugin {name}: settings environment key collides with another plugin"
            )
        allowed_keys.update(keys)
        configurations.append(config)
        plugins.append(plugin)
    _validate_environment(allowed_keys)
    for plugin, config in zip(plugins, configurations, strict=True):
        if plugin.configure is not None:
            plugin.configure(config)
    registry = PluginRegistry(tuple(plugins), frozenset(allowed_keys), selection)
    # Validate contributed handlers before serving even when no worker starts.
    registry.worker_handlers()
    settings._plugin_registry = registry
    return registry


def _validate_compatibility(plugin: DtxwikiPlugin, core_version: str) -> None:
    from backend.spi import SPI_VERSION

    try:
        Version(plugin.version)
        for requirement, current in (
            (plugin.requires_core, core_version),
            (plugin.requires_spi, SPI_VERSION),
        ):
            if not requirement or Version(current) not in SpecifierSet(requirement):
                raise PluginConfigurationError(
                    f"plugin {plugin.name}: incompatible core/SPI requirement"
                )
    except (InvalidVersion, InvalidSpecifier, TypeError):
        raise PluginConfigurationError(
            f"plugin {plugin.name}: invalid PEP 440 version requirement"
        ) from None


def _settings_for(plugin: DtxwikiPlugin) -> tuple[BaseModel | None, set[str]]:
    model = plugin.settings_model
    if model is None:
        return None, set()
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise PluginConfigurationError(
            f"plugin {plugin.name}: settings_model must be a Pydantic model"
        )
    values = {}
    keys = set()
    prefix = f"{_PREFIX}{plugin.name.upper()}_"
    try:
        for name, field in model.model_fields.items():
            key = f"{prefix}{name.upper()}"
            if key in keys:
                raise PluginConfigurationError(
                    f"plugin {plugin.name}: ambiguous settings environment field"
                )
            keys.add(key)
            if key in os.environ:
                adapter = TypeAdapter(field.rebuild_annotation())
                raw = os.environ[key]
                try:
                    value = adapter.validate_python(raw)
                except ValidationError:
                    value = adapter.validate_json(raw)
                values[name] = value
        # validate_by_name keeps field names stable even for aliased settings.
        return model.model_validate(values, by_name=True), keys
    except (ValidationError, ValueError):
        # Pydantic error text may contain secret environment values.
        raise PluginConfigurationError(
            f"plugin {plugin.name}: invalid settings"
        ) from None


def _validate_environment(allowed: set[str] | frozenset[str]) -> None:
    unknown = {key for key in os.environ if key.startswith(_PREFIX)} - allowed
    if unknown:
        raise PluginConfigurationError(
            f"unknown plugin environment keys: {', '.join(sorted(unknown))}"
        )


def _validate_migrations(plugin: DtxwikiPlugin) -> None:
    migrations = plugin.migrations
    if migrations is None:
        return
    from sqlalchemy.orm import DeclarativeBase

    from backend.models.base import Base

    if not isinstance(migrations, PluginMigrations):
        raise PluginConfigurationError(f"plugin {plugin.name}: invalid migration plane")
    if migrations.plane != "control":
        raise PluginConfigurationError(
            f"plugin {plugin.name}: only control-plane migrations are supported; "
            "workspace plugins require workspace deletion lifecycle support"
        )
    plugin_metadata = getattr(migrations.base, "metadata", None)
    if (
        not isinstance(migrations.base, type)
        or not issubclass(migrations.base, DeclarativeBase)
        or plugin_metadata is Base.metadata
    ):
        raise PluginConfigurationError(
            f"plugin {plugin.name}: migrations require an independent DeclarativeBase"
        )
    if not (Path(migrations.script_location) / "versions").is_dir():
        raise PluginConfigurationError(
            f"plugin {plugin.name}: migration versions directory is missing"
        )
    for table in plugin_metadata.tables.values():
        if table.name in Base.metadata.tables or table.name.startswith(
            "alembic_version"
        ):
            raise PluginConfigurationError(
                f"plugin {plugin.name}: model table collides with core"
            )
        for foreign_key in table.foreign_keys:
            if (
                foreign_key.target_fullname.rsplit(".", 1)[0]
                not in plugin_metadata.tables
            ):
                raise PluginConfigurationError(
                    f"plugin {plugin.name}: foreign keys outside plugin "
                    "metadata are forbidden"
                )
        if "workspace_id" in table.c:
            raise PluginConfigurationError(
                f"plugin {plugin.name}: workspace-scoped tables require "
                "workspace deletion lifecycle support"
            )


__all__ = ["PluginConfigurationError", "PluginRegistry", "load_plugins"]
