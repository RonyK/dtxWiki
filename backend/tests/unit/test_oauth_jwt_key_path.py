"""OAuth JWT signing key path regression tests."""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.app.settings import Settings
from backend.services import oauth_service


@pytest.fixture(autouse=True)
def _restore_private_key_cache():
    """Both tests below write ``oauth_service._PRIVATE_KEY_PEM``, a MODULE-level
    global shared by every test in the same process.

    Without this restore, the first test leaks the literal string
    ``"cached-but-invalid"`` into that global for the rest of the worker's
    run, and any later test that mints a JWT from a ``Settings`` with no
    explicit ``oauth_jwt_private_key_path`` (so it falls back to this cache)
    dies with ``InvalidKeyError: Could not parse the provided public key``.
    Under ``pytest -n4``, whether that later test lands on this worker at all
    is luck of the draw, so the damage showed up as an intermittent,
    unreproducible-in-isolation failure somewhere else entirely.
    """

    original = oauth_service._PRIVATE_KEY_PEM
    yield
    oauth_service._PRIVATE_KEY_PEM = original


def _write_rsa_private_key(path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    path.write_text(pem, encoding="utf-8")
    return pem


def test_private_key_path_takes_precedence_over_generated_cache(tmp_path):
    key_path = tmp_path / "jwt.pem"
    expected_pem = _write_rsa_private_key(key_path)
    settings = Settings(oauth_jwt_private_key_path=str(key_path))

    oauth_service._PRIVATE_KEY_PEM = "cached-but-invalid"

    assert oauth_service._private_key_pem(settings) == expected_pem


def test_public_key_is_stable_across_generated_cache_reset(tmp_path):
    key_path = tmp_path / "jwt.pem"
    _write_rsa_private_key(key_path)
    settings = Settings(oauth_jwt_private_key_path=str(key_path))

    oauth_service._PRIVATE_KEY_PEM = None
    public_key_before = oauth_service._public_key_pem(settings)
    oauth_service._PRIVATE_KEY_PEM = None
    public_key_after = oauth_service._public_key_pem(settings)

    assert public_key_after == public_key_before
