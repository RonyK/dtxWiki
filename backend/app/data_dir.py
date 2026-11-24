"""Resolve dtxWiki single-user data paths."""

import os
from collections.abc import Mapping
from pathlib import Path


def data_dir(*, env: Mapping[str, str] | None = None) -> Path:
    override = (os.environ if env is None else env).get("DTXWIKI_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".dtxwiki"


def db_dir(*, base: Path | None = None) -> Path:
    return (data_dir() if base is None else base) / "data"


def logs_dir() -> Path:
    return data_dir() / "logs"


def config_toml_path() -> Path:
    return data_dir() / "config.toml"


def default_sqlite_path(*, base: Path | None = None) -> Path:
    return db_dir(base=base) / "dtxwiki.sqlite"


def default_sqlite_url() -> str:
    return f"sqlite+pysqlite:///{default_sqlite_path()}"
