"""Encryption for registered LLM credentials using a dedicated keyring."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypedDict
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.app.data_dir import data_dir
from backend.app.runtime_credentials import publish_private_credential_file

logger = logging.getLogger(__name__)

_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MAX_KEYRING_BYTES = 64 * 1024
_NONCE_BYTES = 12

ConnectionScope = Literal["user", "workspace"]


class EncryptedCredential(TypedDict):
    key_id: str
    nonce: str
    ciphertext: str


class CredentialVaultError(RuntimeError):
    """A keyring or encrypted credential cannot be used."""


@dataclass(frozen=True)
class CredentialBinding:
    scope: ConnectionScope
    owner_id: UUID
    connection_id: UUID
    secret_revision: int
    purpose: Literal["active", "pending"] = "active"

    def associated_data(self) -> bytes:
        return json.dumps(
            [
                "dtxwiki.llm.credential.v1",
                self.scope,
                str(self.owner_id),
                str(self.connection_id),
                self.secret_revision,
                self.purpose,
            ],
            separators=(",", ":"),
        ).encode()


class CredentialVault(Protocol):
    def encrypt(
        self, credential: Mapping[str, str], *, binding: CredentialBinding
    ) -> EncryptedCredential: ...

    def decrypt(
        self, encrypted: Mapping[str, str], *, binding: CredentialBinding
    ) -> dict[str, str]: ...


class FileCredentialVault:
    """Read a dedicated AES-256 keyring only when a credential is used."""

    def __init__(self, *, keyring_file: str | None, solo_local: bool) -> None:
        self._keyring_file = keyring_file
        self._solo_local = solo_local

    def encrypt(
        self, credential: Mapping[str, str], *, binding: CredentialBinding
    ) -> EncryptedCredential:
        key_id, keys = self._read_keyring(create_local=True)
        nonce = os.urandom(_NONCE_BYTES)
        plaintext = json.dumps(dict(credential), separators=(",", ":")).encode()
        ciphertext = AESGCM(keys[key_id]).encrypt(
            nonce, plaintext, binding.associated_data()
        )
        return {
            "key_id": key_id,
            "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext),
        }

    def decrypt(
        self, encrypted: Mapping[str, str], *, binding: CredentialBinding
    ) -> dict[str, str]:
        _, keys = self._read_keyring(create_local=False)
        if not isinstance(encrypted, Mapping):
            logger.warning("LLM credential: malformed envelope")
            raise CredentialVaultError("LLM credential cannot be decrypted")
        if (
            not isinstance(encrypted.get("key_id"), str)
            or encrypted["key_id"] not in keys
        ):
            logger.warning("LLM credential keyring: unknown key_id")
            raise CredentialVaultError("LLM credential cannot be decrypted")
        try:
            nonce = _decode(encrypted["nonce"])
            if len(nonce) != _NONCE_BYTES:
                raise ValueError
            plaintext = AESGCM(keys[encrypted["key_id"]]).decrypt(
                nonce, _decode(encrypted["ciphertext"]), binding.associated_data()
            )
            credential = json.loads(plaintext)
            if not isinstance(credential, dict) or not all(
                isinstance(value, str) for value in credential.values()
            ):
                raise ValueError
        except (InvalidTag, LookupError, ValueError, TypeError, binascii.Error):
            logger.warning("LLM credential: malformed or authentication failed")
            raise CredentialVaultError("LLM credential cannot be decrypted") from None
        return credential

    def _read_keyring(self, *, create_local: bool) -> tuple[str, dict[str, bytes]]:
        if self._keyring_file:
            path = Path(self._keyring_file)
            initialize = False
        else:
            path = data_dir() / "llm-credential-keyring.json"
            initialize = self._solo_local and create_local

        try:
            try:
                raw = _read_private_keyring_file(path)
            except FileNotFoundError:
                if not initialize:
                    raise
                create_keyring(path)
                raw = _read_private_keyring_file(path)

            keyring = json.loads(raw)
            if not isinstance(keyring, dict) or not isinstance(
                keyring.get("keys"), dict
            ):
                raise ValueError
            keys = {}
            for key_id, encoded in keyring["keys"].items():
                if (
                    not isinstance(key_id, str)
                    or not _KEY_ID.fullmatch(key_id)
                    or not isinstance(encoded, str)
                ):
                    raise ValueError
                key = _decode(encoded)
                if len(key) != 32:
                    raise ValueError
                keys[key_id] = key
            active = keyring.get("active_key_id")
            if not isinstance(active, str) or active not in keys:
                raise ValueError
        except (OSError, ValueError, binascii.Error, RecursionError) as exc:
            reason = (
                "missing"
                if isinstance(exc, FileNotFoundError)
                else "permission"
                if isinstance(exc, PermissionError)
                else "unreadable"
                if isinstance(exc, OSError)
                else "malformed"
            )
            logger.warning("LLM credential keyring: %s", reason)
            raise CredentialVaultError(
                "LLM credential keyring is unavailable"
            ) from None
        return active, keys


def create_keyring(path: Path) -> None:
    """Publish a keyring once, without replacing an existing one."""
    if path.exists():
        return
    key_id = uuid4().hex
    keyring = {
        "active_key_id": key_id,
        "keys": {key_id: _encode(AESGCM.generate_key(bit_length=256))},
    }
    publish_private_credential_file(path, json.dumps(keyring).encode())


def _read_private_keyring_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        mode = os.fstat(handle.fileno()).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError
        raw = handle.read(_MAX_KEYRING_BYTES + 1)
    if len(raw) > _MAX_KEYRING_BYTES:
        raise ValueError
    return raw


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
