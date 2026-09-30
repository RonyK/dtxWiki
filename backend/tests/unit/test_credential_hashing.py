import pytest
from argon2 import PasswordHasher, Type

from backend.services.credential_hashing import (
    API_TOKEN_PREFIX,
    CredentialHasher,
    create_password_hasher,
)


def fast_hasher() -> CredentialHasher:
    return CredentialHasher(
        PasswordHasher(
            time_cost=1,
            memory_cost=1024,
            parallelism=1,
            hash_len=16,
            salt_len=16,
            type=Type.ID,
        )
    )


def test_password_hash_uses_argon2id_and_verifies_without_plaintext_storage():
    hasher = fast_hasher()

    encoded = hasher.hash_password("correct horse battery staple")

    assert encoded.startswith("$argon2id$")
    assert "correct horse battery staple" not in encoded
    assert hasher.verify_password("correct horse battery staple", encoded)
    assert not hasher.verify_password("wrong", encoded)
    assert not hasher.verify_password("correct horse battery staple", "not-a-hash")


def test_password_needs_rehash_detects_parameter_drift():
    weak_hasher = fast_hasher()
    encoded = weak_hasher.hash_password("change-me")
    stronger_hasher = CredentialHasher(
        PasswordHasher(
            time_cost=2,
            memory_cost=1024,
            parallelism=1,
            hash_len=16,
            salt_len=16,
            type=Type.ID,
        )
    )

    assert not weak_hasher.password_needs_rehash(encoded)
    assert stronger_hasher.password_needs_rehash(encoded)


def test_api_token_generation_lookup_hash_and_verifier_are_safe_for_storage():
    hasher = fast_hasher()

    token = hasher.generate_api_token()
    lookup_hash = hasher.token_lookup_hash(token)
    encoded = hasher.hash_api_token(token)

    assert token.startswith(API_TOKEN_PREFIX)
    assert lookup_hash.startswith("sha256:")
    assert token not in lookup_hash
    assert encoded.startswith("$argon2id$")
    assert token not in encoded
    assert hasher.verify_api_token(token, encoded)
    assert not hasher.verify_api_token(hasher.generate_api_token(), encoded)


def test_token_lookup_hash_is_deterministic_and_prefix_scoped():
    hasher = fast_hasher()
    token = f"{API_TOKEN_PREFIX}fixed-token-with-high-entropy-placeholder"

    assert hasher.token_lookup_hash(token) == hasher.token_lookup_hash(token)
    with pytest.raises(ValueError, match="must start"):
        hasher.token_lookup_hash("oauth-style-token")
    encoded = hasher.hash_api_token(token)
    assert not hasher.verify_api_token("oauth-style-token", encoded)


def test_empty_password_and_short_token_randomness_are_rejected():
    hasher = fast_hasher()

    with pytest.raises(ValueError, match="password"):
        hasher.hash_password("")
    with pytest.raises(ValueError, match="at least 32"):
        hasher.generate_api_token(nbytes=16)


@pytest.mark.real_argon2
def test_production_hasher_keeps_the_owasp_argon2id_cost():
    """Pin the deployed KDF cost.

    The suite-wide `fast_argon2` fixture replaces this cost everywhere else, so
    this is the only test that still observes it. Without it, lowering the
    production parameters would leave every other test green.
    """

    hasher = create_password_hasher()

    assert hasher.time_cost == 3
    assert hasher.memory_cost == 64 * 1024
    assert hasher.parallelism == 4
    assert hasher.hash_len == 32
    assert hasher.salt_len == 16
    assert hasher.type is Type.ID
    assert hasher.hash("pin-the-cost").startswith("$argon2id$v=19$m=65536,t=3,p=4$")
