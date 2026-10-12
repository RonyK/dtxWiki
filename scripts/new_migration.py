#!/usr/bin/env python3
"""Create an Alembic revision in its major/minor release directory."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from alembic import command
from alembic.config import Config


def create_revision(release: str, message: str, *, root: Path | None = None):
    """Generate a unique ID; release versions group files, never order them."""

    if re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", release) is None:
        raise ValueError("release must be MAJOR.MINOR or MAJOR.MINOR.PATCH")
    major, minor = (int(part) for part in release.split(".")[:2])
    root = root or Path(__file__).resolve().parents[1]
    migrations = root / "backend" / "migrations"
    versions = migrations / "versions"
    target = versions / f"v{major}_{minor}"
    locations = {
        target,
        *(
            path
            for path in versions.iterdir()
            if path.name.startswith("v") and path.is_dir()
        ),
    }
    config = Config(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("path_separator", "os")
    config.set_main_option("recursive_version_locations", "true")
    config.set_main_option(
        "version_locations", os.pathsep.join(str(path) for path in sorted(locations))
    )
    return command.revision(config, message=message, version_path=str(target))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("-m", "--message", required=True)
    args = parser.parse_args()
    try:
        create_revision(args.release, args.message)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
