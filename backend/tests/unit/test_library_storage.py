import os
from urllib.parse import urlparse

import pytest

import backend.services.library_storage as library_storage
from backend.services.library_storage import (
    LIBRARY_FILE_ROOTS_ENV,
    LibraryStorageError,
    read_text_ref,
)


def test_file_library_item_ref_reads_inside_allowed_root(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "source.md"
    source.write_text("# Source", encoding="utf-8")
    monkeypatch.setenv(LIBRARY_FILE_ROOTS_ENV, str(allowed))

    assert read_text_ref(source.as_uri()) == "# Source"


def test_file_library_item_ref_rejects_path_outside_allowed_root(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()
    source = denied / "secret.md"
    source.write_text("secret", encoding="utf-8")
    monkeypatch.setenv(LIBRARY_FILE_ROOTS_ENV, str(allowed))

    with pytest.raises(LibraryStorageError, match="outside allowed roots"):
        read_text_ref(source.as_uri())


def test_file_library_item_ref_supports_multiple_allowed_roots(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    source = second / "source.md"
    source.write_text("second", encoding="utf-8")
    monkeypatch.setenv(
        LIBRARY_FILE_ROOTS_ENV,
        os.pathsep.join([str(first), str(second)]),
    )

    assert read_text_ref(source.as_uri()) == "second"


def test_file_library_item_ref_handles_leading_slash_uri_path(tmp_path, monkeypatch):
    # Regression for Windows file:// URL handling: urlparse("file:///C:/...").path
    # returns "/C:/..." with a leading slash, and Path("/C:/...") was being
    # interpreted as a drive-relative path so _is_allowed_file_ref rejected the
    # round-tripped URI. url2pathname normalises this for both POSIX and Windows.
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "source.md"
    source.write_text("regression", encoding="utf-8")
    monkeypatch.setenv(LIBRARY_FILE_ROOTS_ENV, str(allowed))

    uri = source.as_uri()
    assert urlparse(uri).path.startswith("/")
    assert read_text_ref(uri) == "regression"


def test_file_library_item_ref_decodes_uri_path_before_allowed_root_check(
    tmp_path, monkeypatch
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "source.md"
    source.write_text("decoded", encoding="utf-8")
    monkeypatch.setenv(LIBRARY_FILE_ROOTS_ENV, str(allowed))

    windows_uri_path = "/C:/Users/example/AppData/Local/Temp/source.md"

    def decode_uri_path(uri_path: str) -> str:
        if uri_path == windows_uri_path:
            return str(source)
        return uri_path

    monkeypatch.setattr(library_storage, "url2pathname", decode_uri_path)

    assert library_storage.read_text_ref(f"file://{windows_uri_path}") == "decoded"
