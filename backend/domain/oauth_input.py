"""OAuth input budgets; identifiers and protocol values are never normalized."""

from backend.domain.credential_input import (
    OAUTH_IDENTIFIER_MAX_BYTES,
    OAUTH_METADATA_LIST_MAX_COUNT,
    OAUTH_REDIRECT_MAX_COUNT,
    OAUTH_SCOPE_MAX_BYTES,
    utf8_size_is_valid,
)


def validate_scope_input(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("scope must be a string")
    if len(value) > OAUTH_SCOPE_MAX_BYTES or any(
        not " " <= character <= "~" for character in value
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


SUPPORTED_GRANT_TYPES = ("authorization_code", "refresh_token")
SUPPORTED_RESPONSE_TYPES = ("code",)


def supported_values(values: list[str], supported: tuple[str, ...]) -> tuple[str, ...]:
    """The part of a client's list this server implements, in server order.

    Client metadata describes everything a client can do with any server, so
    it may name grants or response types this server does not offer. Those
    are ignored rather than fatal (Client ID Metadata Document §6); a client
    metadata document registers this intersection, while DCR registers the
    server's own values (RFC 7591 §3.2.1).
    """

    return tuple(value for value in supported if value in values)


def validate_supported_metadata(payload: dict) -> None:
    for field, required in (
        ("grant_types", "authorization_code"),
        ("response_types", "code"),
    ):
        if field not in payload:
            continue
        values = payload[field]
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= OAUTH_METADATA_LIST_MAX_COUNT
            or not all(isinstance(value, str) for value in values)
            or required not in values
        ):
            raise ValueError(f"{field} contains unsupported client metadata")
    for field in ("application_type", "token_endpoint_auth_method"):
        if payload.get(field) is not None and not isinstance(payload[field], str):
            raise ValueError(f"{field} must be a string")
