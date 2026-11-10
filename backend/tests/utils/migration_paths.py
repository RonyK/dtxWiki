"""Explicit, test-owned Git roots for real Alembic commands."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic.config import Config


def configure_test_repositories(config: Config) -> None:
    """Bind the directory lifetime to this config, never the user's data path."""
    # backend/tests/conftest.py overrides this variable with tmp_path for
    # every test. Match runtime composition when the traversal later uses it.
    test_data_dir = os.environ.get("DTXWIKI_DATA_DIR")
    if test_data_dir:
        config.attributes["context_repositories_root"] = (
            Path(test_data_dir).resolve() / "repositories"
        )
        return
    directory = TemporaryDirectory(prefix="dtxwiki-migration-test-")
    config.attributes["_context_repository_test_directory"] = directory
    config.attributes["context_repositories_root"] = (
        Path(directory.name) / "repositories"
    )
