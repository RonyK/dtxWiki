"""Build the native dtxWiki sidecar for the current host and architecture."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import tomllib
from pathlib import Path

import PyInstaller.__main__
from python_notices import NOTICES_NAME, write_notices

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"
ENGINE_OUTPUT = DESKTOP / "build" / "engine"
PYINSTALLER_WORK = DESKTOP / "build" / "pyinstaller"
PYINSTALLER_DIST = PYINSTALLER_WORK / "dist"


def _version(path: Path, keys: tuple[str, ...]) -> str:
    if path.suffix == ".toml":
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
    for key in keys:
        value = value[key]
    return str(value)


def _verify_inputs() -> None:
    python_version = _version(ROOT / "pyproject.toml", ("project", "version"))
    desktop_version = _version(DESKTOP / "package.json", ("version",))
    if python_version != desktop_version:
        raise RuntimeError(
            "desktop/package.json and pyproject.toml versions must match "
            f"({desktop_version} != {python_version})"
        )
    static_index = ROOT / "backend" / "static" / "index.html"
    if not static_index.is_file():
        raise FileNotFoundError(
            "backend/static/index.html is missing. Build and deploy the frontend "
            "bundle before building the desktop engine."
        )


def _add_data(source: Path, destination: str) -> str:
    return f"{source}{os.pathsep}{destination}"


def main() -> None:
    _verify_inputs()
    shutil.rmtree(ENGINE_OUTPUT, ignore_errors=True)
    shutil.rmtree(PYINSTALLER_DIST, ignore_errors=True)
    PYINSTALLER_WORK.mkdir(parents=True, exist_ok=True)

    arguments = [
        str(DESKTOP / "engine_entry.py"),
        "--name=dtxwiki-engine",
        "--onedir",
        "--noconfirm",
        "--clean",
        f"--paths={ROOT}",
        f"--distpath={PYINSTALLER_DIST}",
        f"--workpath={PYINSTALLER_WORK / 'work'}",
        f"--specpath={PYINSTALLER_WORK}",
        "--collect-submodules=backend",
        "--exclude-module=backend.tests",
        "--copy-metadata=dtxwiki",
        f"--add-data={_add_data(ROOT / 'backend' / 'alembic.ini', 'backend')}",
        "--add-data="
        + _add_data(ROOT / "backend" / "migrations", "backend/migrations"),
        f"--add-data={_add_data(ROOT / 'backend' / 'catalog', 'backend/catalog')}",
        f"--add-data={_add_data(ROOT / 'backend' / 'static', 'backend/static')}",
    ]
    PyInstaller.__main__.run(arguments)

    staged_output = PYINSTALLER_DIST / "dtxwiki-engine"
    if not staged_output.is_dir():
        raise FileNotFoundError(f"PyInstaller did not create {staged_output}")
    shutil.move(str(staged_output), str(ENGINE_OUTPUT))

    # Written from this build's own Analysis TOC, so the notices name what was
    # actually frozen. Beside the executable, not in _internal, so a user can
    # find it; electron-builder copies the whole engine directory.
    notices = write_notices(
        PYINSTALLER_WORK / "work" / "dtxwiki-engine" / "Analysis-00.toc",
        ENGINE_OUTPUT / "licenses" / NOTICES_NAME,
    )
    print(f"wrote {notices}")

    executable = ENGINE_OUTPUT / (
        "dtxwiki-engine.exe" if sys.platform == "win32" else "dtxwiki-engine"
    )
    if not executable.is_file():
        raise FileNotFoundError(f"PyInstaller did not create {executable}")
    if sys.platform != "win32":
        executable.chmod(executable.stat().st_mode | 0o111)
    print(
        "built desktop engine "
        f"for {platform.system()} {platform.machine()}: {executable}"
    )


if __name__ == "__main__":
    main()
