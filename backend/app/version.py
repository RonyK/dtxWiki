"""Runtime version helpers.

The package metadata is the source of truth for installed builds. Raw source
runs fall back to pyproject.toml so ADR A13's dotted-numeric version guard stays
stable without duplicating the release version in Python code.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as metadata_version
from pathlib import Path

PACKAGE_NAME = "dtxwiki"
UNKNOWN_SOURCE_VERSION = "0.0.0"


def package_version() -> str:
    try:
        return metadata_version(PACKAGE_NAME)
    except PackageNotFoundError:
        return source_version()


def source_version() -> str:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except Exception:
        return UNKNOWN_SOURCE_VERSION
