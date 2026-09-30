"""Closed security policy shared by catalog validation and local rendering."""

import re

# Secret-bearing JSON is deliberately much narrower than arbitrary JSON. A new
# credential sink is a trust-boundary change and therefore requires an explicit
# code/security review rather than a data-only catalog edit.
APPROVED_JSON_ACCESS_TOKEN_PATHS = frozenset({("bearerToken",)})
APPROVED_JSON_AUTHORIZATION_BEARER_PATHS = frozenset(
    {("headers", "Authorization")}
)
_SAFE_COMMAND_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SAFE_SECRET_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_AUTHORIZATION_BEARER_PREFIX = "Authorization: Bearer "
# "/bin/sh -c ..." starts with "/" but is a shell invocation, not a client
# slash command; the pin is a shape, not a prefix.
_SLASH_COMMAND_SHAPE = re.compile(r"^/[a-z][a-z0-9-]*(\s|$)")


def is_slash_command_shaped(value: str) -> bool:
    """Accept only text whose first token is a client slash command."""

    return _SLASH_COMMAND_SHAPE.match(value) is not None


def has_unsafe_control_characters(value: str) -> bool:
    """Reject every C0 control, DEL, and the Unicode line separators.

    URL parsers strip some of these before validating, and terminals or app
    prompts interpret others (tab completion, ANSI escapes), so none of them
    belong in copyable setup text or the inputs substituted into it.
    """

    return any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or character in "\u2028\u2029"
        for character in value
    )


def is_approved_json_access_token_sink(
    path: tuple[str, ...],
    slot: str,
) -> bool:
    """Allow only reviewed direct-token and complete Bearer-header sinks."""

    return (path in APPROVED_JSON_ACCESS_TOKEN_PATHS and slot == "access-token") or (
        path in APPROVED_JSON_AUTHORIZATION_BEARER_PATHS
        and slot == "authorization-bearer"
    )


def is_safe_command_env_name(env_name: str) -> bool:
    """Mirror the closed environment-name grammar used by the Web renderer."""

    return _SAFE_COMMAND_ENV_NAME.fullmatch(env_name) is not None


def is_safe_secret_env_name(env_name: str) -> bool:
    """Accept only the closed environment-name grammar used for secrets."""

    return _SAFE_SECRET_ENV_NAME.fullmatch(env_name) is not None


def is_approved_command_env_secret_sink(env_name: str, atoms: tuple) -> bool:
    """Allow a bare secret only in a syntactically safe environment variable."""

    return (
        is_safe_secret_env_name(env_name)
        and len(atoms) == 1
        and getattr(atoms[0], "slot", None) == "access-token-env"
    )


def is_approved_command_argv_secret_sink(atoms: tuple) -> bool:
    """Retain only the reviewed Claude HTTP Authorization header shape."""

    return (
        len(atoms) == 2
        and getattr(atoms[0], "literal", None) == _AUTHORIZATION_BEARER_PREFIX
        and getattr(atoms[1], "slot", None) == "access-token"
    )


__all__ = [
    "APPROVED_JSON_ACCESS_TOKEN_PATHS",
    "APPROVED_JSON_AUTHORIZATION_BEARER_PATHS",
    "has_unsafe_control_characters",
    "is_approved_command_argv_secret_sink",
    "is_approved_command_env_secret_sink",
    "is_approved_json_access_token_sink",
    "is_safe_command_env_name",
    "is_safe_secret_env_name",
    "is_slash_command_shaped",
]
