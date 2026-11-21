from importlib.metadata import PackageNotFoundError, version

from packaging.version import Version

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
    # Installed metadata uses PEP 440 spelling for the pyproject.toml version.
    source_version = app_version.source_version()
    assert source_version != app_version.UNKNOWN_SOURCE_VERSION
    assert Version(source_version) == Version(version(app_version.PACKAGE_NAME))


def test_settings_default_app_version_uses_package_version(monkeypatch):
    monkeypatch.delenv("DTXWIKI_APP_VERSION", raising=False)

    assert Settings().app_version == app_version.package_version()
