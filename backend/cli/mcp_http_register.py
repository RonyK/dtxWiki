from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import tomlkit
from tomlkit.exceptions import ParseError

from backend.catalog.mcp_apps import (
    CatalogRepository,
    CompatibilityEvaluator,
    ServerMcpCapabilities,
    SetupContext,
    SetupPlanner,
    SetupPlanningError,
    SetupRenderer,
    get_mcp_app_catalog_repository,
)
from backend.catalog.mcp_apps.renderer import SetupRenderError
from backend.cli.integrations._common import (
    OWNER_ONLY_FILE_MODE,
    atomic_write_text,
    restrict_to_owner,
)
from backend.cli.mcp_url import (
    is_loopback_hostname,
    is_private_network_ip,
    normalize_mcp_endpoint_url,
)

DEFAULT_SERVER_NAME = "dtxwiki"
CODEX_BEARER_TOKEN_ENV_VAR = "DTXWIKI_MCP_PAT"
GEMINI_BEARER_TOKEN_ENV_VAR = "DTXWIKI_MCP_PAT"


@dataclass(frozen=True)
class RegisterHttpResult:
    client: str
    config_path: Path | None
    backup_path: Path | None
    setup_text: str
    command: str | None
    wrote_config: bool
    fallback_reason: str | None = None
    secret_env_names: tuple[str, ...] = ()
    replaced_registration: str | None = None


@dataclass(frozen=True)
class LocalConfigAdapter:
    """Optional app-specific filesystem adapter, separate from setup planning."""

    path_factory: Callable[[], Path]
    writer: Callable[..., None]
    forbidden_server_name_characters: frozenset[str] = frozenset()
    # Reports a same-named server already in the file so an overwrite is
    # announced instead of silently discarding somebody's endpoint.
    existing_registration_reader: Callable[[Path, str], str | None] | None = None


def register_http_server(
    *,
    url: str,
    client: str,
    token: str | None,
    name: str = DEFAULT_SERVER_NAME,
    dry_run: bool = False,
    echo_only: bool = False,
    allow_insecure_local: bool = False,
) -> RegisterHttpResult:
    normalized_client = _normalize_client(client)
    config_adapter = _local_config_adapter(normalized_client)
    normalized_name = _normalize_name(name, adapter=config_adapter)
    normalized_url = _normalize_url(url, allow_insecure_local=allow_insecure_local)
    setup_text, command, secret_env_names = _render_cli_setup(
        url=normalized_url,
        client=normalized_client,
        token=token,
        name=normalized_name,
        redact_token=True,
    )
    config_path = config_adapter.path_factory() if config_adapter is not None else None
    replaced = _existing_registration(
        config_path, adapter=config_adapter, name=normalized_name
    )
    setup_text = _annotate_setup_text(
        setup_text,
        replaced_registration=replaced,
        client=normalized_client,
        name=normalized_name,
        writes_config=not (dry_run or echo_only) and config_adapter is not None,
    )

    if echo_only:
        return RegisterHttpResult(
            client=normalized_client,
            config_path=config_path,
            backup_path=None,
            setup_text=setup_text,
            command=command,
            wrote_config=False,
            fallback_reason="echo-only requested",
            secret_env_names=secret_env_names,
            replaced_registration=replaced,
        )

    if dry_run:
        return RegisterHttpResult(
            client=normalized_client,
            config_path=config_path,
            backup_path=(
                _backup_path(config_path)
                if config_path is not None and config_path.exists()
                else None
            ),
            setup_text=setup_text,
            command=command,
            wrote_config=False,
            fallback_reason=(
                "no local config adapter; copy the rendered setup"
                if config_adapter is None
                else None
            ),
            secret_env_names=secret_env_names,
            replaced_registration=replaced,
        )

    if config_adapter is None or config_path is None:
        return RegisterHttpResult(
            client=normalized_client,
            config_path=None,
            backup_path=None,
            setup_text=setup_text,
            command=command,
            wrote_config=False,
            fallback_reason="no local config adapter; copy the rendered setup",
            secret_env_names=secret_env_names,
            replaced_registration=replaced,
        )

    try:
        backup_path = _write_config(
            client=normalized_client,
            adapter=config_adapter,
            config_path=config_path,
            url=normalized_url,
            token=token,
            name=normalized_name,
        )
    except Exception as exc:
        return RegisterHttpResult(
            client=normalized_client,
            config_path=config_path,
            backup_path=None,
            setup_text=setup_text,
            command=command,
            wrote_config=False,
            fallback_reason=f"config patch failed: {exc}",
            secret_env_names=secret_env_names,
            replaced_registration=replaced,
        )

    return RegisterHttpResult(
        client=normalized_client,
        config_path=config_path,
        backup_path=backup_path,
        setup_text=setup_text,
        command=command,
        wrote_config=True,
        secret_env_names=secret_env_names,
        replaced_registration=replaced,
    )


def register_http_servers(
    *,
    urls: list[str],
    client: str,
    token: str | None,
    names: list[str] | None = None,
    dry_run: bool = False,
    echo_only: bool = False,
    allow_insecure_local: bool = False,
) -> list[RegisterHttpResult]:
    if not urls:
        raise ValueError("at least one --url is required")
    if names:
        if len(names) != len(urls):
            raise ValueError("--name must be repeated once per --url")
        resolved_names = names
    elif len(urls) == 1:
        resolved_names = [DEFAULT_SERVER_NAME]
    else:
        raise ValueError(
            "--name must be repeated once per --url when registering several servers"
        )

    # Validate the complete batch before touching any client config so a typo in
    # the last server cannot leave an earlier server partially registered.
    normalized_client = _normalize_client(client)
    config_adapter = _local_config_adapter(normalized_client)
    normalized_names: list[str] = []
    for url, name in zip(urls, resolved_names, strict=True):
        normalized_url = _normalize_url(
            url, allow_insecure_local=allow_insecure_local
        )
        _validate_auth_transport(normalized_url, token=token)
        normalized_names.append(_normalize_name(name, adapter=config_adapter))

    duplicate_names = _duplicate_values(normalized_names)
    if duplicate_names:
        rendered = ", ".join(repr(name) for name in duplicate_names)
        raise ValueError(
            f"--name values must be unique after normalization; duplicate: {rendered}"
        )

    return [
        register_http_server(
            url=url,
            client=normalized_client,
            token=token,
            name=name,
            dry_run=dry_run,
            echo_only=echo_only,
            allow_insecure_local=allow_insecure_local,
        )
        for url, name in zip(urls, normalized_names, strict=True)
    ]


def manual_command(
    *,
    url: str,
    client: str,
    token: str | None,
    name: str,
    redact_token: bool = False,
    repository: CatalogRepository | None = None,
) -> str:
    _setup_text, command, _secret_env_names = _render_cli_setup(
        url=url,
        client=client,
        token=token,
        name=name,
        redact_token=redact_token,
        repository=repository,
    )
    if command is None:
        raise ValueError(f"{client} setup does not provide a shell command")
    return command


def _render_cli_setup(
    *,
    url: str,
    client: str,
    token: str | None,
    name: str,
    redact_token: bool = False,
    repository: CatalogRepository | None = None,
) -> tuple[str, str | None, tuple[str, ...]]:
    catalog = repository or get_mcp_app_catalog_repository()
    normalized_client = _normalize_client(client, repository=catalog)
    normalized_name = _normalize_name(
        name,
        adapter=_local_config_adapter(normalized_client, repository=catalog),
    )
    normalized_url = _normalize_url(url, allow_insecure_local=True)
    _validate_auth_transport(normalized_url, token=token)
    shell = _current_shell()
    context = SetupContext(
        endpoint_reachability=_endpoint_reachability(normalized_url),
        platform=_current_platform(),
        shell=shell,
    )
    app = catalog.find_app(normalized_client)
    if app is None:  # pragma: no cover - guarded by _normalize_client
        raise ValueError(f"unsupported client: {normalized_client}")
    desired_auth = "access-key" if token else "oauth"
    options = CompatibilityEvaluator().evaluate(
        app.variants,
        ServerMcpCapabilities(
            oauth_available=True,
            transports=frozenset({"streamable-http"}),
            endpoint_reachability=frozenset({context.endpoint_reachability}),
        ),
        context,
        lifecycle=app.lifecycle,
    )
    candidates = [
        option
        for option in options
        if option.available
        and option.surface == "cli"
        and option.transport == "streamable-http"
        and option.auth == desired_auth
    ]
    if not candidates:
        raise ValueError(
            f"{normalized_client} has no compatible {desired_auth} CLI setup"
        )
    selected = candidates[0]
    try:
        plan = SetupPlanner(catalog).plan_for_app(
            app_id=normalized_client,
            variant_id=selected.variant_id,
            context=context,
        )
        rendered = SetupRenderer(catalog).render(
            plan,
            mcp_url=normalized_url,
            shell=shell,
            access_token=("<redacted>" if token and redact_token else token),
            server_name=normalized_name,
        )
    except (SetupPlanningError, SetupRenderError) as exc:
        raise ValueError(f"catalog setup could not be rendered: {exc}") from exc
    # In-app commands are typed into the client prompt rather than a shell,
    # so they are printed as guidance instead of joining `command_text`, which
    # callers paste into a terminal.
    setup_artifacts: list[tuple[str, str]] = []
    commands: list[str] = []
    secret_env_names_set: set[str] = set()
    automatic_registration = False
    for artifact in rendered:
        secret_env_names_set.update((artifact.env or {}).keys())
        if artifact.kind == "command":
            setup_text = artifact.command
        elif artifact.kind == "json-config":
            setup_text = (
                f"{artifact.label or 'MCP server entry'} "
                f"(mcpServers[{json.dumps(normalized_name, ensure_ascii=False)}]):\n"
                f"{artifact.value}"
                if artifact.value is not None
                else None
            )
        elif artifact.kind == "copy-mcp-url":
            setup_text = (
                f"{artifact.label or 'MCP URL'}: {artifact.value}"
                if artifact.value is not None
                else None
            )
        elif artifact.kind == "deep-link":
            setup_text = (
                f"{artifact.label or 'Open'}: {artifact.value}"
                if artifact.value is not None
                else None
            )
        elif artifact.kind == "app-command":
            setup_text = (
                f"{artifact.value} (type this inside the app)"
                if artifact.value is not None
                else None
            )
        elif artifact.kind == "instruction":
            setup_text = artifact.value
        else:
            raise ValueError(
                f"unknown catalog CLI setup artifact kind: {artifact.kind}"
            )

        if setup_text is None:
            continue
        setup_artifacts.append((artifact.step, setup_text))
        if artifact.kind == "command":
            commands.append(setup_text)
        if artifact.kind in {"command", "json-config"}:
            automatic_registration = True

    if not setup_artifacts:
        raise ValueError("catalog CLI setup must render at least one setup artifact")

    setup_texts: list[str] = []
    steps = {step for step, _setup_text in setup_artifacts}
    show_step_headers = {"register", "authenticate"}.issubset(steps)
    # A header on every step transition, not once per step: an interleaved
    # recipe (register, authenticate, register) must never show a register
    # artifact under the "Sign in:" header.
    previous_step: str | None = None
    for step, setup_text in setup_artifacts:
        if show_step_headers and step != previous_step:
            setup_texts.append(
                "Register the server:" if step == "register" else "Sign in:"
            )
            previous_step = step
        setup_texts.append(setup_text)
    if not automatic_registration:
        setup_texts.append(
            "Note: no automatic registration was performed; complete the steps "
            "above in the app."
        )

    secret_env_names = tuple(sorted(secret_env_names_set))
    setup_text = "\n".join(setup_texts)
    command_text = "\n".join(commands) if commands else None
    return setup_text, command_text, secret_env_names


def config_path_for_client(client: str) -> Path:
    adapter = _local_config_adapter(client)
    if adapter is None:
        raise ValueError(f"no local config adapter for client: {client}")
    return adapter.path_factory()


def local_http_client_ids(
    *, repository: CatalogRepository | None = None
) -> tuple[str, ...]:
    """Return active Streamable HTTP clients with reviewed config adapters."""

    catalog = repository or get_mcp_app_catalog_repository()
    return tuple(
        sorted(
            app.id
            for app in catalog.snapshot().apps
            if app.lifecycle == "active"
            and app.local_config_adapter_id in _LOCAL_CONFIG_ADAPTERS
            and any(
                variant.surface == "cli" and variant.transport == "streamable-http"
                for variant in app.variants
            )
        )
    )


def claude_code_http_config_path() -> Path:
    if sys.platform == "win32":
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            return Path(userprofile) / ".claude.json"
    return Path.home() / ".claude.json"


def codex_http_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home) / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def gemini_cli_http_config_path() -> Path:
    return Path.home() / ".gemini" / "settings.json"


def antigravity_http_config_path() -> Path:
    return Path.home() / ".gemini" / "config" / "mcp_config.json"


def _write_config(
    *,
    client: str,
    adapter: LocalConfigAdapter | None = None,
    config_path: Path,
    url: str,
    token: str | None,
    name: str,
) -> Path | None:
    resolved_adapter = adapter or _local_config_adapter(client)
    if resolved_adapter is None:
        raise ValueError(f"no local config adapter for client: {client}")
    backup_path = _backup_once(config_path)
    resolved_adapter.writer(config_path, url=url, token=token, name=name)
    return backup_path


def _write_claude_config(
    config_path: Path,
    *,
    url: str,
    token: str | None,
    name: str,
) -> None:
    config = _read_json_or_empty(config_path)
    # Claude's reviewed JSON MCP shape mirrors `claude mcp add --transport
    # http`: OAuth needs no non-standard flag and is completed from `/mcp`.
    block: dict[str, object] = {"type": "http", "url": url}
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if headers:
        block["headers"] = headers
    config.setdefault("mcpServers", {})[name] = block
    atomic_write_text(
        config_path,
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        mode=OWNER_ONLY_FILE_MODE,
    )


def _write_gemini_config(
    config_path: Path,
    *,
    url: str,
    token: str | None,
    name: str,
) -> None:
    config = _read_json_or_empty(config_path)
    block: dict[str, object] = {"httpUrl": url}
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer ${GEMINI_BEARER_TOKEN_ENV_VAR}"
    if headers:
        block["headers"] = headers
    _upsert_json_server(config, name=name, block=block)
    atomic_write_text(
        config_path,
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        mode=OWNER_ONLY_FILE_MODE,
    )


def _write_antigravity_config(
    config_path: Path,
    *,
    url: str,
    token: str | None,
    name: str,
) -> None:
    # Antigravity 1.1.x may create a literal JSON `null` placeholder before the
    # first MCP server is added. Treat only that valid empty sentinel as `{}`;
    # malformed JSON and every other non-object still fail closed.
    config = _read_json_or_empty(config_path, allow_null=True)
    block: dict[str, object] = {"serverUrl": url}
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if headers:
        block["headers"] = headers
    _upsert_json_server(config, name=name, block=block)
    atomic_write_text(
        config_path,
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        mode=OWNER_ONLY_FILE_MODE,
    )


def _write_codex_config(
    config_path: Path,
    *,
    url: str,
    name: str,
    token: str | None = None,
) -> None:
    if config_path.exists() and config_path.read_text(encoding="utf-8").strip():
        doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    servers = doc.get("mcp_servers")
    if servers is None:
        servers = tomlkit.table()
        doc["mcp_servers"] = servers

    block = tomlkit.table()
    block.add("url", url)
    if token:
        block.add("bearer_token_env_var", CODEX_BEARER_TOKEN_ENV_VAR)
        block.add(
            tomlkit.comment(
                f"Export {CODEX_BEARER_TOKEN_ENV_VAR}; do not store PATs here."
            )
        )
    servers[name] = block
    atomic_write_text(config_path, tomlkit.dumps(doc), mode=OWNER_ONLY_FILE_MODE)


def _write_codex_config_adapter(
    config_path: Path,
    *,
    url: str,
    token: str | None,
    name: str,
) -> None:
    _write_codex_config(config_path, url=url, name=name, token=token)


def _existing_json_registration(config_path: Path, name: str) -> str | None:
    """Describe a same-named server already in a JSON config, or None.

    Reading is best effort: a file this cannot parse is one the writer is
    about to fail on anyway, and refusing to register over an unreadable file
    would be a new failure mode rather than the missing warning being fixed.
    """

    try:
        config = _read_json_or_empty(config_path, allow_null=True)
    except (OSError, ValueError):
        return None
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return None
    entry = servers[name]
    if isinstance(entry, dict):
        for key in ("url", "httpUrl", "serverUrl"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
    return "an entry with no URL"


def _existing_codex_registration(config_path: Path, name: str) -> str | None:
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        doc = tomlkit.parse(text)
    except ParseError:
        return None
    servers = doc.get("mcp_servers")
    if not isinstance(servers, dict) or name not in servers:
        return None
    entry = servers[name]
    url = entry.get("url") if isinstance(entry, dict) else None
    return url if isinstance(url, str) and url else "an entry with no URL"


def _existing_registration(
    config_path: Path | None,
    *,
    adapter: LocalConfigAdapter | None,
    name: str,
) -> str | None:
    if (
        config_path is None
        or adapter is None
        or adapter.existing_registration_reader is None
    ):
        return None
    return adapter.existing_registration_reader(config_path, name)


_LOCAL_CONFIG_ADAPTERS = {
    "antigravity-json": LocalConfigAdapter(
        path_factory=lambda: antigravity_http_config_path(),
        writer=_write_antigravity_config,
        existing_registration_reader=_existing_json_registration,
    ),
    "claude-json": LocalConfigAdapter(
        path_factory=lambda: claude_code_http_config_path(),
        writer=_write_claude_config,
        existing_registration_reader=_existing_json_registration,
    ),
    "codex-toml": LocalConfigAdapter(
        path_factory=lambda: codex_http_config_path(),
        writer=_write_codex_config_adapter,
        existing_registration_reader=_existing_codex_registration,
    ),
    "gemini-json": LocalConfigAdapter(
        path_factory=lambda: gemini_cli_http_config_path(),
        writer=_write_gemini_config,
        forbidden_server_name_characters=frozenset({"_"}),
        existing_registration_reader=_existing_json_registration,
    ),
}


def _local_config_adapter(
    client: str,
    *,
    repository: CatalogRepository | None = None,
) -> LocalConfigAdapter | None:
    """Resolve a reviewed filesystem format by catalog capability, not app ID.

    Ordinary apps can reuse an existing adapter through catalog data. A new
    local configuration format extends the closed adapter vocabulary and is an
    intentional renderer/security change.
    """

    catalog = repository or get_mcp_app_catalog_repository()
    app = catalog.find_app(client)
    if app is None or app.local_config_adapter_id is None:
        return None
    return _LOCAL_CONFIG_ADAPTERS[app.local_config_adapter_id]


def _read_json_or_empty(path: Path, *, allow_null: bool = False) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data is None and allow_null:
        return {}
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")
    return data


def _upsert_json_server(
    config: dict,
    *,
    name: str,
    block: dict[str, object],
) -> None:
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be a JSON object")
    servers[name] = block


def _backup_once(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = _backup_path(path)
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    # copy2 carries the original's mode over, and the original may predate
    # this tool (0644): the backup holds the same bearer token, so it is
    # tightened like the config file itself.
    restrict_to_owner(backup)
    return backup


def _backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return path.with_name(f"{path.name}.bak.{stamp}")


def _normalize_client(
    client: str,
    *,
    repository: CatalogRepository | None = None,
) -> str:
    normalized = client.strip().lower()
    catalog = repository or get_mcp_app_catalog_repository()
    app = catalog.find_app(normalized)
    supported = sorted(
        candidate.id
        for candidate in catalog.snapshot().apps
        if candidate.lifecycle == "active"
        and any(
            variant.surface == "cli" and variant.transport == "streamable-http"
            for variant in candidate.variants
        )
    )
    if app is None or normalized not in supported:
        raise ValueError(f"--client must be one of: {', '.join(supported)}")
    return normalized


def _current_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _current_shell() -> str:
    return "powershell" if sys.platform == "win32" else "bash"


def _endpoint_reachability(url: str) -> str:
    hostname = urlparse(url).hostname or ""
    if is_loopback_hostname(hostname):
        return "loopback"
    if urlparse(url).scheme == "http" and is_private_network_ip(hostname):
        return "private-network"
    return "public-https"


def _normalize_name(
    name: str,
    *,
    adapter: LocalConfigAdapter | None,
) -> str:
    normalized = name.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("--name must contain 1 to 128 characters")
    if any(character in normalized for character in "\r\n\0"):
        raise ValueError("--name contains unsafe control characters")
    if adapter and any(
        character in normalized
        for character in adapter.forbidden_server_name_characters
    ):
        raise ValueError("--name must not contain underscores for this client")
    return normalized


def _duplicate_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def _annotate_setup_text(
    setup_text: str,
    *,
    replaced_registration: str | None,
    client: str,
    name: str,
    writes_config: bool,
) -> str:
    notes: list[str] = []
    if replaced_registration is not None:
        notes.append(
            f"Warning: {client} already registers an MCP server named {name!r} "
            f"({replaced_registration}); this setup "
            f"{'replaces' if writes_config else 'will replace'} it."
        )
    if not notes:
        return setup_text
    return "\n".join([setup_text, *notes])


def _normalize_url(url: str, *, allow_insecure_local: bool = False) -> str:
    return normalize_mcp_endpoint_url(
        url,
        allow_http_loopback=allow_insecure_local,
        allow_http_private_network=allow_insecure_local,
    )


def _validate_auth_transport(url: str, *, token: str | None) -> None:
    if urlparse(url).scheme == "http" and not (token and token.strip()):
        raise ValueError("plaintext HTTP registration requires a PAT")


def _shell_quote(value: str) -> str:
    if not value:
        return "''"
    if all(ch.isalnum() or ch in "-_./:=" for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"
