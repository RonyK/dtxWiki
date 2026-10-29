"""Password verification that treats external-only users as unknown accounts."""

from backend.services.credential_hashing import CredentialHasher

DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$u2au8gUQ3qKl1XimMaOpFQ"
    "$Kvoeh33YAsJ5V/VMP8lB4wS0V3JS85RQUdHFy8Ew0CA"
)


def verify_password(
    hasher: CredentialHasher, password: str, password_hash: str | None
) -> bool:
    verified = hasher.verify_password(password, password_hash or DUMMY_PASSWORD_HASH)
    return password_hash is not None and verified
