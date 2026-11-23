import base64
import json
import logging
from dataclasses import replace
from uuid import uuid4

import pytest

from backend.llm.credential_vault import (
    CredentialBinding,
    CredentialVaultError,
    FileCredentialVault,
)


def test_llm_credential_envelope_is_bound_and_remains_readable_during_rotation(
    tmp_path,
    caplog,
    monkeypatch,
):
    vault_logger = logging.getLogger("backend.llm.credential_vault")
    monkeypatch.setattr(
        vault_logger, "handlers", [*vault_logger.handlers, caplog.handler]
    )
    monkeypatch.setattr(vault_logger, "level", logging.WARNING)
    monkeypatch.setattr(vault_logger, "disabled", False)
    path = tmp_path / "keyring.json"
    keys = {"old": _key(1), "new": _key(2)}

    def write_keyring(active_key_id, retained_keys):
        path.write_text(
            json.dumps({"active_key_id": active_key_id, "keys": retained_keys})
        )
        path.chmod(0o600)

    write_keyring("old", {"old": keys["old"]})
    vault = FileCredentialVault(keyring_file=str(path), solo_local=False)
    credential = {"api_key": "test-credential-never-written-in-plaintext"}
    binding = CredentialBinding("user", uuid4(), uuid4(), 1)
    path.chmod(0o444)
    first = vault.encrypt(credential, binding=binding)
    assert credential["api_key"] not in json.dumps(first)
    assert first["key_id"] == "old"
    assert first["nonce"] != vault.encrypt(credential, binding=binding)["nonce"]

    path.chmod(0o600)
    write_keyring("new", keys)
    with pytest.raises(CredentialVaultError, match="cannot be decrypted"):
        vault.decrypt(None, binding=binding)
    with pytest.raises(CredentialVaultError, match="cannot be decrypted"):
        vault.decrypt([], binding=binding)
    assert vault.decrypt(first, binding=binding) == credential
    assert vault.encrypt(credential, binding=binding)["key_id"] == "new"
    with pytest.raises(CredentialVaultError, match="cannot be decrypted"):
        vault.decrypt(first, binding=replace(binding, connection_id=uuid4()))

    write_keyring("new", {"new": keys["new"]})
    with pytest.raises(CredentialVaultError, match="cannot be decrypted"):
        vault.decrypt(first, binding=binding)
    assert "unknown key_id" in caplog.text
    path.unlink()
    with pytest.raises(CredentialVaultError):
        vault.encrypt(credential, binding=binding)
    assert "keyring: missing" in caplog.text
    assert credential["api_key"] not in caplog.text
    from unittest.mock import patch

    with patch("backend.llm.credential_vault.data_dir", return_value=tmp_path):
        default = tmp_path / "llm-credential-keyring.json"
        default.write_text(
            json.dumps({"active_key_id": "new", "keys": {"new": keys["new"]}})
        )
        default.chmod(0o644)
        assert (
            FileCredentialVault(keyring_file=None, solo_local=False).encrypt(
                credential, binding=binding
            )["key_id"]
            == "new"
        )


def _key(value):
    return base64.urlsafe_b64encode(bytes([value]) * 32).decode()
