"""Codex #47: URL credential redaction masks userinfo AND query-string creds."""

import pytest

from backend.utils.redact import redact_url


@pytest.mark.unit
def test_redact_masks_userinfo_password():
    assert (
        redact_url("postgresql+pg8000://dtx:s3cret@db.internal:5432/dtx")
        == "postgresql+pg8000://dtx:****@db.internal:5432/dtx"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    ["password", "pwd", "secret", "token", "api_key", "api-key", "apikey"],
)
def test_redact_masks_query_string_credentials(key):
    url = f"postgresql+pg8000://db.internal/dtx?sslmode=require&{key}=hunter2"
    redacted = redact_url(url)
    assert "hunter2" not in redacted
    assert f"{key}=****" in redacted
    # non-credential query params are preserved
    assert "sslmode=require" in redacted


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        "sslpassword",
        "access_token",
        "refresh_token",
        "client_secret",
        "x-api-key",
        "password_file",
    ],
)
def test_redact_masks_query_keys_containing_credential_token(key):
    # Keys whose name merely CONTAINS a credential token (prefix/suffix forms
    # like sslpassword or access_token) must be masked, not only exact names.
    url = f"postgresql+pg8000://db.internal/dtx?sslmode=require&{key}=hunter2"
    redacted = redact_url(url)
    assert "hunter2" not in redacted
    assert f"{key}=****" in redacted
    assert "sslmode=require" in redacted


@pytest.mark.unit
def test_redact_masks_sslpassword_as_first_query_param():
    redacted = redact_url("postgresql+pg8000://db.internal/dtx?sslpassword=keypass")
    assert "keypass" not in redacted
    assert "sslpassword=****" in redacted


@pytest.mark.unit
def test_redact_is_case_insensitive_on_query_keys():
    assert "TopSecret" not in redact_url("https://h/db?PASSWORD=TopSecret")
    assert "KeyPass" not in redact_url("https://h/db?SSLPassword=KeyPass")


@pytest.mark.unit
def test_redact_leaves_credential_free_url_untouched():
    url = "sqlite+pysqlite:////var/lib/dtxwiki/dtxwiki.sqlite"
    assert redact_url(url) == url


@pytest.mark.unit
def test_redact_handles_empty():
    assert redact_url("") == ""


@pytest.mark.unit
def test_redact_masks_password_containing_at_sign():
    # #8: an '@' inside the password no longer leaks the tail (was [^@]+).
    assert (
        redact_url("postgresql://user:p@ssword@host:5432/db")
        == "postgresql://user:****@host:5432/db"
    )
    assert (
        redact_url("postgresql://user:pa@ss@wo@rd@host/db")
        == "postgresql://user:****@host/db"
    )
    assert (
        redact_url("postgresql://u:p@ss@host/db?password=q@x&token=z")
        == "postgresql://u:****@host/db?password=****&token=****"
    )


@pytest.mark.unit
def test_redact_masks_empty_username_password():
    # #5: a credential URL with an empty username (scheme://:pw@host, e.g. the
    # redis://:pw@host form) must still have its password masked.
    assert (
        redact_url("redis://:p@ss@host:6379/0") == "redis://:****@host:6379/0"
    )
    assert (
        redact_url("redis://:simplepw@host:6379/0")
        == "redis://:****@host:6379/0"
    )
