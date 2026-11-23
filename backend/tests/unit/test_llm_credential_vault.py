import base64
import json
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
):
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
    first = vault.encrypt(credential, binding=binding)
    assert credential["api_key"] not in json.dumps(first)
    assert first["key_id"] == "old"
    assert first["nonce"] != vault.encrypt(credential, binding=binding)["nonce"]

    write_keyring("new", keys)
    assert vault.decrypt(first, binding=binding) == credential
    assert vault.encrypt(credential, binding=binding)["key_id"] == "new"
    with pytest.raises(CredentialVaultError, match="cannot be decrypted"):
        vault.decrypt(first, binding=replace(binding, connection_id=uuid4()))

    write_keyring("new", {"new": keys["new"]})
    with pytest.raises(CredentialVaultError, match="cannot be decrypted"):
        vault.decrypt(first, binding=binding)


def _key(value):
    return base64.urlsafe_b64encode(bytes([value]) * 32).decode()
