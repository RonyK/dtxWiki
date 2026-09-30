"""Fail-closed local renderer for reviewed setup artifacts.

Rendering is intentionally separate from planning: the persisted catalog and
HTTP plan remain secret-free.  A browser or CLI process may inject a one-time
secret into these narrow sinks, return copyable text, and must never persist or
execute the result automatically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlparse

from backend.catalog.mcp_apps.models import (
    AccessTokenAtom,
    AppCommandArtifact,
    CommandArtifact,
    CopyMcpUrlArtifact,
    DeepLinkArtifact,
    InstructionArtifact,
    JsonConfigArtifact,
    LiteralAtom,
    McpUrlAtom,
    ServerNameAtom,
    SetupStep,
)
from backend.catalog.mcp_apps.planner import SetupPlan
from backend.catalog.mcp_apps.repository import CatalogRepository
from backend.catalog.mcp_apps.security import (
    has_unsafe_control_characters,
    is_approved_command_argv_secret_sink,
    is_approved_command_env_secret_sink,
    is_approved_json_access_token_sink,
    is_safe_command_env_name,
    is_slash_command_shaped,
)
from backend.cli.mcp_url import normalize_mcp_endpoint_url


class SetupRenderError(RuntimeError):
    error_code = "mcp_setup_render_failed"


SUPPORTED_SETUP_RECIPE_VERSIONS = frozenset({1})


@dataclass(frozen=True)
class RenderedArtifact:
    kind: str
    label: str | None = None
    value: str | None = None
    command: str | None = None
    argv: tuple[str, ...] | None = None
    env: dict[str, str] | None = None
    step: SetupStep = "register"


class SetupRenderer:
    def __init__(self, repository: CatalogRepository) -> None:
        self.repository = repository

    def render(
        self,
        plan: SetupPlan,
        *,
        mcp_url: str,
        shell: str,
        access_token: str | None = None,
        server_name: str = "dtxwiki",
        locale: str = "en",
    ) -> tuple[RenderedArtifact, ...]:
        if plan.recipe_version not in SUPPORTED_SETUP_RECIPE_VERSIONS:
            raise SetupRenderError(
                f"unsupported setup recipe version: {plan.recipe_version}"
            )
        normalized_url = _validated_mcp_url(mcp_url)
        normalized_server_name = _validated_server_name(server_name)
        if shell not in {"powershell", "bash", "zsh"}:
            raise SetupRenderError("unsupported shell")
        if access_token is not None and any(ch in access_token for ch in "\r\n\0"):
            raise SetupRenderError("access token contains unsafe control characters")
        if plan.auth == "access-key" and not access_token:
            raise SetupRenderError("access token is required for this setup plan")
        if plan.auth == "oauth" and access_token is not None:
            raise SetupRenderError("OAuth setup must not receive an access token")

        rendered: list[RenderedArtifact] = []
        for artifact in plan.artifacts:
            if isinstance(artifact, CommandArtifact):
                for argument in artifact.argv:
                    if any(
                        isinstance(atom, AccessTokenAtom) for atom in argument
                    ) and not is_approved_command_argv_secret_sink(argument):
                        raise SetupRenderError(
                            "access token uses an unapproved command sink"
                        )
                for env_name, atoms in (artifact.env or {}).items():
                    if not is_safe_command_env_name(env_name):
                        raise SetupRenderError("unsafe command environment name")
                    if any(
                        isinstance(atom, AccessTokenAtom) for atom in atoms
                    ) and not is_approved_command_env_secret_sink(env_name, atoms):
                        raise SetupRenderError(
                            "access token uses an unapproved command sink"
                        )
                executable = next(
                    (
                        item
                        for item in self.repository.snapshot().executables
                        if item.id == artifact.executable_id
                    ),
                    None,
                )
                if executable is None or executable.invocation != "direct-argv":
                    raise SetupRenderError("unknown or unsafe executable")
                argv = (
                    executable.command_name,
                    *(
                        _render_atoms(
                            argument,
                            mcp_url=normalized_url,
                            access_token=access_token,
                            server_name=normalized_server_name,
                        )
                        for argument in artifact.argv
                    ),
                )
                env = {
                    key: _render_atoms(
                        atoms,
                        mcp_url=normalized_url,
                        access_token=access_token,
                        server_name=normalized_server_name,
                    )
                    for key, atoms in (artifact.env or {}).items()
                }
                rendered.append(
                    RenderedArtifact(
                        kind="command",
                        command=" ".join(_shell_quote(value, shell) for value in argv),
                        argv=tuple(argv),
                        env=env or None,
                        step=artifact.step,
                    )
                )
            elif isinstance(artifact, AppCommandArtifact):
                # Typed into the app's own prompt, so it is never shell-quoted:
                # quoting would corrupt the literal the user has to type.
                value = _render_atoms(
                    artifact.value,
                    mcp_url=normalized_url,
                    access_token=None,
                    server_name=normalized_server_name,
                )
                # The catalog gate only sees atoms; the single-line slash
                # contract must also hold on the slot-substituted result.
                # Shape, not prefix: "/bin/sh ..." is not a slash command.
                if not is_slash_command_shaped(
                    value
                ) or has_unsafe_control_characters(value):
                    raise SetupRenderError(
                        "rendered in-app command must be a single slash-command line"
                    )
                rendered.append(
                    RenderedArtifact(
                        kind=artifact.kind,
                        value=value,
                        step=artifact.step,
                    )
                )
            elif isinstance(artifact, CopyMcpUrlArtifact):
                rendered.append(
                    RenderedArtifact(
                        kind=artifact.kind,
                        label=_localized(artifact.label, locale),
                        value=_render_atoms(
                            artifact.value,
                            mcp_url=normalized_url,
                            access_token=access_token,
                            server_name=normalized_server_name,
                        ),
                        step=artifact.step,
                    )
                )
            elif isinstance(artifact, JsonConfigArtifact):
                rendered.append(
                    RenderedArtifact(
                        kind=artifact.kind,
                        label=_localized(artifact.label, locale),
                        value=json.dumps(
                            _render_json_template(
                                artifact.value,
                                mcp_url=normalized_url,
                                access_token=access_token,
                                server_name=normalized_server_name,
                                path=(),
                            ),
                            ensure_ascii=False,
                            indent=2,
                        ),
                        step=artifact.step,
                    )
                )
            elif isinstance(artifact, DeepLinkArtifact):
                value = _render_atoms(
                    artifact.url,
                    mcp_url=normalized_url,
                    access_token=None,
                    server_name=normalized_server_name,
                )
                parsed = urlparse(value)
                if parsed.scheme != "https" or not parsed.netloc:
                    raise SetupRenderError("deep link must resolve to public HTTPS")
                rendered.append(
                    RenderedArtifact(
                        kind=artifact.kind,
                        label=_localized(artifact.label, locale),
                        value=value,
                        step=artifact.step,
                    )
                )
            elif isinstance(artifact, InstructionArtifact):
                rendered.append(
                    RenderedArtifact(
                        kind=artifact.kind,
                        value=_localized(artifact.body, locale),
                        step=artifact.step,
                    )
                )
            else:  # pragma: no cover - discriminated model rejects this earlier
                raise SetupRenderError("unknown setup artifact")
        if len(rendered) != len(plan.artifacts):
            raise SetupRenderError("setup plan was only partially rendered")
        return tuple(rendered)


def _render_atoms(
    atoms,
    *,
    mcp_url: str,
    access_token: str | None,
    server_name: str,
) -> str:
    parts: list[str] = []
    for atom in atoms:
        if isinstance(atom, LiteralAtom):
            parts.append(atom.literal)
        elif isinstance(atom, McpUrlAtom):
            parts.append(mcp_url)
        elif isinstance(atom, ServerNameAtom):
            parts.append(server_name)
        elif isinstance(atom, AccessTokenAtom):
            if access_token is None:
                raise SetupRenderError("access token slot is unresolved")
            parts.append(
                f"Bearer {access_token}"
                if atom.slot == "authorization-bearer"
                else access_token
            )
        else:  # pragma: no cover - closed Pydantic union
            raise SetupRenderError("unknown setup slot")
    return "".join(parts)


def _render_json_template(
    value,
    *,
    mcp_url: str,
    access_token: str | None,
    server_name: str,
    path: tuple[str, ...],
):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [
            _render_json_template(
                item,
                mcp_url=mcp_url,
                access_token=access_token,
                server_name=server_name,
                path=(*path, str(index)),
            )
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        raise SetupRenderError("JSON setup contains a non-JSON value")
    if path and "bearerToken" in value:
        raise SetupRenderError("nested bearerToken is not an approved JSON field")
    if set(value) == {"literal"}:
        return str(value["literal"])
    if set(value) == {"slot"}:
        slot = value["slot"]
        if slot == "mcp-url":
            return mcp_url
        if slot == "server-name":
            return server_name
        if slot in {
            "access-token",
            "access-token-env",
            "authorization-bearer",
        }:
            if not is_approved_json_access_token_sink(path, slot):
                raise SetupRenderError("access token uses an unapproved JSON sink")
            if access_token is None:
                raise SetupRenderError("access token slot is unresolved")
            return (
                f"Bearer {access_token}"
                if slot == "authorization-bearer"
                else access_token
            )
        raise SetupRenderError("unknown JSON setup slot")
    return {
        str(key): _render_json_template(
            item,
            mcp_url=mcp_url,
            access_token=access_token,
            server_name=server_name,
            path=(*path, str(key)),
        )
        for key, item in value.items()
    }


def _validated_mcp_url(value: str) -> str:
    # urlsplit strips tab/CR/LF before parsing, so a URL that embeds them
    # would pass the parsed checks below while the raw string this function
    # returns still carries them into rendered slot values. Reject them raw.
    if has_unsafe_control_characters(value):
        raise SetupRenderError("MCP URL contains unsafe control characters")
    try:
        return normalize_mcp_endpoint_url(value, allow_http_loopback=True)
    except ValueError as exc:
        raise SetupRenderError(str(exc)) from exc


def _validated_server_name(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise SetupRenderError("server name must contain 1 to 128 characters")
    if has_unsafe_control_characters(normalized):
        raise SetupRenderError("server name contains unsafe control characters")
    return normalized


def _shell_quote(value: str, shell: str) -> str:
    if not value:
        return "''"
    if all(character.isalnum() or character in "-_./:=" for character in value):
        return value
    if shell == "powershell":
        return "'" + value.replace("'", "''") + "'"
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _localized(value, locale: str) -> str:
    return value.ko if locale == "ko" else value.en


__all__ = ["RenderedArtifact", "SetupRenderError", "SetupRenderer"]
