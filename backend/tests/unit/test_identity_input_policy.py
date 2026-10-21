from types import SimpleNamespace

import pytest

from backend.domain.identity_policy import (
    DisplayNamePolicyError,
    EmailPolicyError,
    PasswordInputPolicyError,
    normalize_display_name,
    normalize_email,
    validate_display_name,
    validate_password,
)
from backend.schemas.auth import LoginRequest, SignupRequest
from backend.services.credential_hashing import CredentialHasher
from backend.services.session_service import (
    DisplayNameInputPolicyError,
    EmailInputPolicyError,
    PasswordPolicyError,
    SessionService,
)
from backend.services.user_admin_service import UserAdminService


def test_display_name_code_point_boundary_and_explicit_trim():
    # An astral character is 4 UTF-8 bytes and 2 UTF-16 units: one row catches
    # either wrong unit.
    assert normalize_display_name("\u3000" + "😀" * 32 + " ", "a@b") == "😀" * 32
    with pytest.raises(DisplayNamePolicyError):
        normalize_display_name("😀" * 33, "a@b")
    with pytest.raises(DisplayNamePolicyError):
        validate_display_name(" 😀")


# One row per disjunct of the single-line predicate (C0, C1, line separator,
# surrogate).
def test_display_name_rejects_raw_control_before_fallback():
    for bad in ("\n", "\x85", "\u2028", "\ud800"):
        with pytest.raises(DisplayNamePolicyError):
            normalize_display_name(bad, "a@b")


def test_optional_name_fallback_never_truncates_email():
    assert normalize_display_name(None, "a" * 32 + "@b") == "a" * 32
    assert normalize_display_name("\u3000", "a" * 33 + "@b") == "User"
    assert normalize_display_name("family 👨‍👩‍👧‍👦", "a@b") == "family 👨‍👩‍👧‍👦"


def test_email_counts_normalized_utf8_bytes_and_local_part_separately():
    assert normalize_email(" A@B ") == "a@b"
    assert normalize_email("한" * 21 + "a@b") == "한" * 21 + "a@b"
    with pytest.raises(EmailPolicyError):
        normalize_email("한" * 21 + "aa@b")
    boundary = "a@" + "b" * 252
    assert len(normalize_email(boundary).encode()) == 254
    with pytest.raises(EmailPolicyError):
        normalize_email(boundary + "b")
    # U+0130 lowercases to two code points and three UTF-8 bytes.
    with pytest.raises(EmailPolicyError):
        normalize_email("İ" * 22 + "@b")


def test_email_rejects_one_address_contract_violations():
    for bad in ("a@@b", "a b@c", "a@b\n"):
        with pytest.raises(EmailPolicyError):
            normalize_email(bad)


def test_password_preserves_exact_secret_and_rejects_maximum_without_exception():
    assert validate_password("😀" * 128, new=True) == "😀" * 128
    assert validate_password("   exact secret   ", new=True) == "   exact secret   "
    assert validate_password("x") == "x"
    for value, is_new in [
        ("a" * 14, True),
        ("a" * 129, False),
        ("\ud800", False),
    ]:
        with pytest.raises(PasswordInputPolicyError):
            validate_password(value, new=is_new)


def _unexpected(*args, **kwargs):
    raise AssertionError("invalid input reached database or password hashing")


def test_signup_and_cli_service_reject_before_database_or_kdf():
    factory = SimpleNamespace(begin=_unexpected)
    hasher = SimpleNamespace(hash_password=_unexpected, verify_password=_unexpected)
    signup = SessionService(
        session_factory=factory,
        credential_hasher=hasher,
        token_codec=None,
        signup_enabled=True,
    )
    admin = UserAdminService(session_factory=factory, credential_hasher=hasher)
    valid = {"email": "a@b", "password": "a" * 15, "display_name": "Alice"}
    for overrides, error in (
        ({"display_name": "a" * 33}, DisplayNameInputPolicyError),
        ({"email": "a" * 65 + "@b"}, EmailInputPolicyError),
        ({"password": "a" * 14}, PasswordPolicyError),
    ):
        for service in (signup.signup, admin.add_user):
            with pytest.raises(error):
                service(**{**valid, **overrides})


def test_password_specific_helpers_and_login_reject_before_verification():
    hasher = CredentialHasher(
        password_hasher=SimpleNamespace(hash=_unexpected, verify=_unexpected)
    )
    with pytest.raises(PasswordInputPolicyError):
        hasher.hash_password("x" * 129)
    assert hasher.verify_password("x" * 129, "encoded-old-password-hash") is False
    service = SessionService(
        session_factory=SimpleNamespace(begin=_unexpected),
        credential_hasher=hasher,
        token_codec=None,
    )
    with pytest.raises(PasswordPolicyError):
        service.login(email="a@b", password="x" * 129)
    with pytest.raises(PasswordPolicyError):
        service.reauthenticate(user_id=None, current_password="x" * 129)


def test_request_schema_uses_normalized_name_and_email_byte_contract():
    request = SignupRequest(
        email=" 한글@EXAMPLE.COM ",
        password="x" * 15,
        display_name=" " + "😀" * 32 + " ",
    )
    assert request.email == "한글@example.com"
    assert request.display_name == "😀" * 32
    schema = SignupRequest.model_json_schema()["properties"]
    assert "maxLength" not in schema["email"]
    assert schema["email"]["x-normalized-maxBytes"] == 254
    assert schema["display_name"]["x-normalized-maxLength"] == 32
    assert LoginRequest(email="a@b", password="x" * 128).password == "x" * 128
