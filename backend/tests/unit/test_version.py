from importlib.metadata import PackageNotFoundError, version

from backend.app import version as app_version
from backend.app.settings import Settings


def test_package_version_uses_installed_metadata(monkeypatch):
    monkeypatch.setattr(app_version, "metadata_version", lambda name: "1.2.3")

    assert app_version.package_version() == "1.2.3"


def test_package_version_falls_back_to_source_version(monkeypatch):
    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(app_version, "metadata_version", missing)
    monkeypatch.setattr(app_version, "source_version", lambda: "2.3.4")

    assert app_version.package_version() == "2.3.4"


def test_source_version_reads_pyproject_version():
    # No release version is spelled out here, so a version bump never has to
    # edit this test. The installed distribution's metadata was built from the
    # same pyproject.toml, so a source reader that fell back to
    # UNKNOWN_SOURCE_VERSION, or read the wrong key, disagrees with it.
    assert app_version.source_version() == version(app_version.PACKAGE_NAME)
    assert app_version.source_version() != app_version.UNKNOWN_SOURCE_VERSION


def test_settings_default_app_version_uses_package_version(monkeypatch):
    monkeypatch.delenv("DTXWIKI_APP_VERSION", raising=False)

    assert Settings().app_version == app_version.package_version()
