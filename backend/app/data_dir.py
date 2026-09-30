"""Resolve dtxWiki single-user data paths."""

import os
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("DTXWIKI_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".dtxwiki"


def db_dir() -> Path:
    return data_dir() / "data"


def logs_dir() -> Path:
    return data_dir() / "logs"


def config_toml_path() -> Path:
    return data_dir() / "config.toml"


def default_sqlite_path() -> Path:
    return db_dir() / "dtxwiki.sqlite"


def default_sqlite_url() -> str:
    return f"sqlite+pysqlite:///{default_sqlite_path()}"
