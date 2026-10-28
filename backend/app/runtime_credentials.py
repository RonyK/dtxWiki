"""Durable local credentials prepared by the supervised init process."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from backend.app.data_dir import data_dir
from backend.app.settings import Settings


def prepare_serving_credentials(settings: Settings) -> Settings:
    """Keep configured credentials; publish local defaults without rotation."""
    # Resolution creates the existing atomic session-secret file only in
    # solo_local. Configured inline/file secrets retain their precedence.
    _ = settings.effective_session_secret
    if settings.oauth_jwt_private_key_path:
        _validate_key(Path(settings.oauth_jwt_private_key_path).read_bytes())
        return settings
    if settings.oauth_jwt_private_key_pem:
        _validate_key(settings.oauth_jwt_private_key_pem.encode())
        return settings
    if settings.app_mode != "solo_local":
        return settings

    root = data_dir()
    # Desktop shipped this filename before CLI initialization became shared.
    # Preserve the existing signing identity when opening that data root.
    legacy = root / "desktop-oauth-jwt.pem"
    path = legacy if legacy.exists() else root / "oauth-jwt.pem"
    if not path.exists():
        _publish_key(path)
    _validate_key(path.read_bytes())
    if os.name != "nt":
        path.chmod(0o600)
    return settings.model_copy(update={"oauth_jwt_private_key_path": str(path)})


def _validate_key(pem: bytes) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("OAuth signing key must be an RSA private key")


def _publish_key(path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".oauth-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(pem)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Publish only a fully written private file. Another initializer's
            # key always wins over replacing an existing signing identity.
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
