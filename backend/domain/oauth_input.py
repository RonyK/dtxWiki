"""OAuth input budgets; identifiers and protocol values are never normalized."""

from backend.domain.credential_input import (
    OAUTH_IDENTIFIER_MAX_BYTES,
    OAUTH_REDIRECT_MAX_COUNT,
    OAUTH_SCOPE_MAX_BYTES,
    utf8_size_is_valid,
)


def validate_scope_input(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > OAUTH_SCOPE_MAX_BYTES
        or any(not " " <= character <= "~" for character in value)
    ):
        raise ValueError("scope must contain at most 256 ASCII bytes")
    return value


def validate_identifier(value: object) -> str:
    if not utf8_size_is_valid(value, OAUTH_IDENTIFIER_MAX_BYTES) or any(
        ord(character) < 32 or 127 <= ord(character) <= 159 for character in value
    ):
        raise ValueError("client_id must contain 1-2048 UTF-8 bytes without controls")
    return value


def validate_client_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 200
        or any(
            ord(character) < 32
            or 127 <= ord(character) <= 159
            or 0xD800 <= ord(character) <= 0xDFFF
            or character in "\u2028\u2029"
            for character in value
        )
    ):
        raise ValueError("client_name must contain 1-200 characters without controls")
    return value


def validate_redirect_list(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= OAUTH_REDIRECT_MAX_COUNT
        or not all(isinstance(uri, str) for uri in value)
    ):
        raise ValueError("redirect_uris must contain 1-20 URI strings")
    if any(
        not utf8_size_is_valid(uri, OAUTH_IDENTIFIER_MAX_BYTES)
        or any(
            character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159
            for character in uri
        )
        for uri in value
    ):
        raise ValueError(
            "redirect URI must contain 1-2048 UTF-8 bytes without controls"
        )
    return value


def validate_supported_metadata(payload: dict) -> None:
    for field, supported, maximum, required in (
        (
            "grant_types",
            {"authorization_code", "refresh_token"},
            2,
            "authorization_code",
        ),
        ("response_types", {"code"}, 1, "code"),
    ):
        if field not in payload:
            continue
        values = payload[field]
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= maximum
            or not all(
                isinstance(value, str) and value in supported for value in values
            )
            or required not in values
        ):
            raise ValueError(f"{field} contains unsupported client metadata")
    if "application_type" in payload and payload["application_type"] not in (
        "native",
        "web",
    ):
        raise ValueError("application_type must be native or web")
    if (
        "token_endpoint_auth_method" in payload
        and payload["token_endpoint_auth_method"] != "none"
    ):
        raise ValueError("token_endpoint_auth_method must be none")
    if "client_secret" in payload or "client_secret_expires_at" in payload:
        raise ValueError("public client metadata must not contain client secrets")
