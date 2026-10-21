"""Size envelopes for credentials, separate from human password policy."""

RECOVERY_TOKEN_MAX_BYTES = 1024
PAT_MAX_BYTES = 256
PURGE_GRANT_MAX_BYTES = 256
OAUTH_OPAQUE_TOKEN_MAX_BYTES = 1024
OAUTH_JWT_MAX_BYTES = 16 * 1024
SESSION_TOKEN_MAX_BYTES = 4 * 1024
AUTHORIZATION_HEADER_MAX_BYTES = 20 * 1024
COOKIE_MAX_BYTES = 4096
OAUTH_IDENTIFIER_MAX_BYTES = 2048
OAUTH_REDIRECT_MAX_COUNT = 20
OAUTH_SCOPE_MAX_BYTES = 256
OAUTH_QUERY_MAX_BYTES = 16 * 1024
WORKSPACE_GRANTS_MAX_COUNT = 256


def ascii_token_is_valid(value: object, maximum: int) -> bool:
    """Reject before encoding, parsing or hashing; tokens contain no whitespace."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all("!" <= character <= "~" for character in value)
    )


def require_ascii_token(value: object, maximum: int, label: str) -> str:
    if not ascii_token_is_valid(value, maximum):
        raise ValueError(f"{label} must contain 1-{maximum} non-whitespace ASCII bytes")
    return value


def utf8_size_is_valid(value: object, maximum: int, *, minimum: int = 1) -> bool:
    if not isinstance(value, str) or len(value) > maximum:
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return minimum <= size <= maximum


def authorization_header_is_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= AUTHORIZATION_HEADER_MAX_BYTES
        and all(character == "\t" or " " <= character <= "~" for character in value)
    )
