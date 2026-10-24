"""Invocation-local plugin composition, after command-specific environment setup."""

from __future__ import annotations

from dataclasses import dataclass

import click
from typer.core import TyperGroup
from typer.main import get_group

from backend.app.plugins import PluginConfigurationError, PluginRegistry, load_plugins
from backend.app.settings import Settings

_CONTEXT_KEY = "dtxwiki.invocation"
_ENV_FILE_KEY = "dtxwiki.production_env_file"


@dataclass(frozen=True)
class CliInvocation:
    settings: Settings
    plugins: PluginRegistry


def _invocation(ctx: click.Context) -> CliInvocation:
    root = ctx.find_root()
    current = root.meta.get(_CONTEXT_KEY)
    if not isinstance(current, CliInvocation):
        settings = Settings()
        current = CliInvocation(settings, load_plugins(settings))
        root.meta[_CONTEXT_KEY] = current
    return current


def invocation_settings(**overrides) -> Settings:
    """Share configured extensions without caching state between CLI runs.

    Overrides are still validated by Settings. A database/worker override does
    not require configuring the same plugin a second time.
    """
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return Settings(**overrides)
    current = _invocation(ctx)
    if not overrides:
        return current.settings
    values = current.settings.model_dump()
    values.update(overrides)
    settings = Settings(**values)
    settings._plugin_registry = current.plugins
    return settings


def prepared_environment_file() -> str | None:
    """Reuse a deployment path discovered before plugin command parsing."""
    ctx = click.get_current_context(silent=True)
    return ctx.find_root().meta.get(_ENV_FILE_KEY) if ctx is not None else None


class PluginCommandGroup(TyperGroup):
    """Discover plugin commands in the same context as built-in commands."""

    def _install_plugins(self, ctx: click.Context) -> None:
        if getattr(self, "_plugins_installed", False):
            return
        for plugin in _invocation(ctx).plugins.plugins:
            if plugin.cli is None:
                continue
            if plugin.name in self.commands:
                raise PluginConfigurationError(
                    f"plugin {plugin.name}: CLI command name is already registered"
                )
            self.add_command(get_group(plugin.cli), plugin.name)
        self._plugins_installed = True

    def resolve_command(self, ctx: click.Context, args: list[str]):
        # Click has parsed the root options and supplies the exact remaining
        # command arguments here. Do not scan argv: option values may happen
        # to be named 'update' or '--env-file'.
        if args and args[0] in {"update", "rollback"}:
            from backend.cli.commands import update

            command = super().get_command(ctx, args[0])
            assert command is not None
            with command.make_context(
                args[0], list(args[1:]), parent=ctx, resilient_parsing=True
            ) as child:
                method = child.params.get("method", "auto")
                env_file = child.params.get("env_file")
                if child.params.get("finalize_bootstrap") and env_file is None:
                    install = update.install_method.detect_install_method(
                        override=None if method == "auto" else method
                    )
                    if install.kind == "systemd":
                        env_file = str(update._legacy_systemd_env_file())
                update._load_prod_env(method, env_file)
                ctx.find_root().meta[_ENV_FILE_KEY] = env_file
        self._install_plugins(ctx)
        return super().resolve_command(ctx, args)

    def list_commands(self, ctx: click.Context) -> list[str]:
        self._install_plugins(ctx)
        return super().list_commands(ctx)
