"""Account input contracts. Lengths count Unicode code points, except email bytes."""

DISPLAY_NAME_MAX_LENGTH = 32
EMAIL_MAX_BYTES = 254
EMAIL_LOCAL_MAX_BYTES = 64
PASSWORD_MAX_LENGTH = 128
NEW_PASSWORD_MIN_LENGTH = 8
TRIM_CHARACTERS = (
    "\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
)


class DisplayNamePolicyError(ValueError):
    pass


class EmailPolicyError(ValueError):
    pass


class PasswordInputPolicyError(ValueError):
    pass


def _invalid_scalar(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _invalid_single_line(value: str) -> bool:
    return any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        or character in "\u2028\u2029"
        for character in value
    )


def normalize_email(value: str) -> str:
    if not isinstance(value, str) or _invalid_single_line(value):
        raise EmailPolicyError("email contains invalid characters")
    normalized = value.strip(TRIM_CHARACTERS).lower()
    if (
        normalized.count("@") != 1
        or any(character in TRIM_CHARACTERS for character in normalized)
        or any(character in '<>,;"' for character in normalized)
    ):
        raise EmailPolicyError("email must contain one address without spaces")
    local, domain = normalized.split("@")
    if not local or not domain:
        raise EmailPolicyError("email must have a local part and domain")
    if len(normalized.encode("utf-8")) > EMAIL_MAX_BYTES:
        raise EmailPolicyError("email must be at most 254 UTF-8 bytes")
    if len(local.encode("utf-8")) > EMAIL_LOCAL_MAX_BYTES:
        raise EmailPolicyError("email local part must be at most 64 UTF-8 bytes")
    return normalized


def validate_display_name(value: str) -> str:
    if not isinstance(value, str) or _invalid_single_line(value):
        raise DisplayNamePolicyError("display name contains invalid characters")
    if value != value.strip(TRIM_CHARACTERS):
        raise DisplayNamePolicyError("display name must not have surrounding spaces")
    if not 1 <= len(value) <= DISPLAY_NAME_MAX_LENGTH:
        raise DisplayNamePolicyError("display name must be between 1 and 32 characters")
    return value


def normalize_display_name(value: str | None, email: str) -> str:
    if value is not None and (
        not isinstance(value, str) or _invalid_single_line(value)
    ):
        raise DisplayNamePolicyError("display name contains invalid characters")
    normalized = (value or "").strip(TRIM_CHARACTERS)
    if normalized:
        return validate_display_name(normalized)
    candidate = email.split("@", maxsplit=1)[0].strip(TRIM_CHARACTERS)
    try:
        return validate_display_name(candidate)
    except DisplayNamePolicyError:
        return "User"


def validate_password(value: str, *, new: bool = False) -> str:
    minimum = NEW_PASSWORD_MIN_LENGTH if new else 1
    if not isinstance(value, str) or _invalid_scalar(value):
        raise PasswordInputPolicyError("password contains invalid Unicode characters")
    if not minimum <= len(value) <= PASSWORD_MAX_LENGTH:
        raise PasswordInputPolicyError(
            f"password must be between {minimum} and {PASSWORD_MAX_LENGTH} characters"
        )
    return value
