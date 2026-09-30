import hashlib
import secrets
from dataclasses import dataclass, field

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

API_TOKEN_PREFIX = "dtxw_"
TOKEN_LOOKUP_NAMESPACE = "dtxwiki.api_token.v1"
# PURGE_AUTHZ_PLAN S-5: a separate namespace/prefix from the PAT constants
# above -- a workspace purge grant is not an API token (it is never issued
# through token-issuance surfaces, is single-use, and lives 5 minutes), so it
# must not collide with, or be confusable with, a PAT lookup digest.
PURGE_GRANT_PREFIX = "wpg_"
PURGE_GRANT_LOOKUP_NAMESPACE = "dtxwiki.workspace_purge_grant.v1"


def create_password_hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=3,
        memory_cost=64 * 1024,
        parallelism=4,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


@dataclass(frozen=True)
class CredentialHasher:
    password_hasher: PasswordHasher = field(default_factory=create_password_hasher)

    def hash_password(self, password: str) -> str:
        _require_secret(password, "password")
        return self.password_hasher.hash(password)

    def verify_password(self, password: str, encoded_hash: str) -> bool:
        return self._verify_secret(password, encoded_hash)

    def password_needs_rehash(self, encoded_hash: str) -> bool:
        try:
            return self.password_hasher.check_needs_rehash(encoded_hash)
        except (InvalidHashError, VerificationError):
            return True

    def generate_api_token(self, nbytes: int = 32) -> str:
        if nbytes < 32:
            raise ValueError("API tokens require at least 32 random bytes")
        return f"{API_TOKEN_PREFIX}{secrets.token_urlsafe(nbytes)}"

    def token_lookup_hash(self, token: str) -> str:
        _require_api_token(token)
        payload = f"{TOKEN_LOOKUP_NAMESPACE}\0{token}".encode()
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def generate_purge_grant_token(self, nbytes: int = 32) -> str:
        if nbytes < 32:
            raise ValueError("purge grant tokens require at least 32 random bytes")
        return f"{PURGE_GRANT_PREFIX}{secrets.token_urlsafe(nbytes)}"

    def purge_grant_lookup_hash(self, token: str) -> str:
        """SHA-256 lookup digest, not Argon2id: S-5's reasoning is that a
        32-byte-random, single-use, 5-minute-lived grant has no brute-force
        surface Argon2's cost buys anything against, so paying it would only
        slow every purge confirmation down. Comparison is the conditional
        DELETE's SQL equality (S-4), not a constant-time compare in Python.
        """

        _require_purge_grant_token(token)
        payload = f"{PURGE_GRANT_LOOKUP_NAMESPACE}\0{token}".encode()
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def hash_api_token(self, token: str) -> str:
        _require_api_token(token)
        return self.password_hasher.hash(token)

    def verify_api_token(self, token: str, encoded_hash: str) -> bool:
        if not token.startswith(API_TOKEN_PREFIX):
            return False
        return self._verify_secret(token, encoded_hash)

    def hash_secret(self, secret: str) -> str:
        """Argon2id-hash an arbitrary secret with no prefix constraint.

        Used by OAuth refresh tokens (``rt_`` prefix) and other internal
        verifiers that follow the same lookup-digest + Argon2id verifier
        pattern as PATs but cannot reuse ``API_TOKEN_PREFIX``.
        """

        _require_secret(secret, "secret")
        return self.password_hasher.hash(secret)

    def verify_secret(self, secret: str, encoded_hash: str) -> bool:
        """Argon2id-verify an arbitrary secret with no prefix constraint."""

        return self._verify_secret(secret, encoded_hash)

    def _verify_secret(self, secret: str, encoded_hash: str) -> bool:
        if not secret or not encoded_hash:
            return False
        try:
            return self.password_hasher.verify(encoded_hash, secret)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False


def _require_secret(secret: str, label: str) -> None:
    if not secret:
        raise ValueError(f"{label} must not be empty")


def _require_api_token(token: str) -> None:
    _require_secret(token, "API token")
    if not token.startswith(API_TOKEN_PREFIX):
        raise ValueError(f"API token must start with {API_TOKEN_PREFIX}")


def _require_purge_grant_token(token: str) -> None:
    _require_secret(token, "workspace purge grant token")
    if not token.startswith(PURGE_GRANT_PREFIX):
        raise ValueError(
            f"workspace purge grant token must start with {PURGE_GRANT_PREFIX}"
        )
