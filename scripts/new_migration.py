#!/usr/bin/env python3
"""Create an Alembic revision without choosing its eventual release."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config


def create_revision(message: str, *, root: Path | None = None):
    """Append a unique ID under unreleased until release preparation."""

    root = root or Path(__file__).resolve().parents[1]
    migrations = root / "backend" / "migrations"
    versions = migrations / "versions"
    target = versions / "unreleased"
    locations = {
        target,
        *(
            path
            for path in versions.iterdir()
            if (path.name.startswith("v") or path.name == "unreleased")
            and path.is_dir()
        ),
    }
    config = Config(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("path_separator", "os")
    config.set_main_option("recursive_version_locations", "true")
    config.set_main_option(
        "version_locations", os.pathsep.join(str(path) for path in sorted(locations))
    )
    target.mkdir(exist_ok=True)
    helpers = migrations / "revision_modules" / "unreleased"
    helpers.mkdir(parents=True, exist_ok=True)
    package = helpers / "__init__.py"
    if not package.exists():
        package.write_text(
            '"""Helpers for migrations awaiting a release assignment."""\n',
            encoding="utf-8",
        )
    return command.revision(config, message=message, version_path=str(target))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-m", "--message", required=True)
    args = parser.parse_args()
    try:
        create_revision(args.message)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
