"""Deploy a Vite bundle without breaking already-open browser tabs.

The live index is replaced only after every other file has been copied and the
entry files referenced by the new index have been verified in the destination.
Old content-hashed assets stay in place until the explicit prune command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

MANIFEST_DIRECTORY = ".index-manifests"
DEFAULT_RETAIN_DAYS = 7
DEFAULT_RETAIN_MANIFESTS = 5


class _IndexEntryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("src"):
            self.references.add(attributes["src"] or "")
            return
        if tag != "link" or not attributes.get("href"):
            return
        rel = set((attributes.get("rel") or "").lower().split())
        if rel.intersection({"modulepreload", "preload", "stylesheet"}):
            self.references.add(attributes["href"] or "")


def deploy_bundle(
    source: Path,
    destination: Path,
    *,
    fresh: bool = False,
) -> dict[str, object]:
    """Publish a built bundle into ``destination``.

    Merge-copy is the point of this helper: a tab that is already open will
    still ask for chunk hashes only the previous build contains, so the old
    assets have to outlive the new index.

    ``fresh`` is for building a distributable artifact instead of serving one.
    A wheel must carry exactly the bundle it was built from; accumulating every
    previous build's chunks would grow it without bound and ship code no index
    references.
    """

    source = source.resolve()
    destination = destination.resolve()
    with _bundle_lock(destination):
        return _deploy_bundle_locked(source, destination, fresh=fresh)


def _deploy_bundle_locked(
    source: Path,
    destination: Path,
    *,
    fresh: bool,
) -> dict[str, object]:
    source_index = source / "index.html"
    destination_index = destination / "index.html"
    if not source_index.is_file():
        raise FileNotFoundError(f"bundle index does not exist: {source_index}")

    index_html = source_index.read_text(encoding="utf-8")
    entry_files = sorted(_local_entry_files(index_html))
    if fresh and destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    _refresh_live_manifest(destination)

    files = [
        path
        for path in source.rglob("*")
        if path.is_file() and path != source_index
    ]
    files.sort(
        key=lambda path: (
            0 if path.relative_to(source).parts[0] == "assets" else 1,
            path.relative_to(source).as_posix(),
        )
    )
    for source_file in files:
        relative = source_file.relative_to(source)
        _atomic_copy(source_file, destination / relative)

    missing = [
        relative.as_posix()
        for relative in entry_files
        if not (destination / relative).is_file()
    ]
    if missing:
        joined = ", ".join(missing)
        raise FileNotFoundError(
            f"new index references missing deployed entry files: {joined}"
        )

    created_at = datetime.now(UTC)
    manifest = _build_manifest(
        source_index,
        assets=sorted(
            path.relative_to(source).as_posix()
            for path in files
            if path.relative_to(source).parts[0] == "assets"
        ),
        created_at=created_at,
    )
    manifest_path = _write_manifest(destination, manifest)
    _remove_duplicate_manifests(
        destination,
        index_sha256=str(manifest["index_sha256"]),
        keep=manifest_path,
    )

    # Keep this as the final filesystem operation: clients must never receive
    # an index that points at files which are absent from the destination.
    _swap_index(source_index, destination_index)
    return manifest


def prune_bundle(
    destination: Path,
    *,
    retain_days: int = DEFAULT_RETAIN_DAYS,
    retain_manifests: int = DEFAULT_RETAIN_MANIFESTS,
    now: datetime | None = None,
) -> dict[str, list[str]]:
    if retain_days < DEFAULT_RETAIN_DAYS:
        raise ValueError(
            f"retain_days must be at least {DEFAULT_RETAIN_DAYS}"
        )
    if retain_manifests < 1:
        raise ValueError("retain_manifests must be at least 1")

    destination = destination.resolve()
    with _bundle_lock(destination):
        return _prune_bundle_locked(
            destination,
            retain_days=retain_days,
            retain_manifests=retain_manifests,
            now=now,
        )


def _prune_bundle_locked(
    destination: Path,
    *,
    retain_days: int,
    retain_manifests: int,
    now: datetime | None,
) -> dict[str, list[str]]:
    manifest_directory = destination / MANIFEST_DIRECTORY
    manifest_paths = sorted(manifest_directory.glob("*.json"), reverse=True)
    current_time = now or datetime.now(UTC)
    cutoff = current_time - timedelta(days=retain_days)
    current_index_hash = _current_index_hash(destination / "index.html")

    records: list[tuple[Path, dict[str, object], datetime]] = []
    for path in manifest_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        records.append((path, payload, created_at))

    if current_index_hash is None or not any(
        payload.get("index_sha256") == current_index_hash
        for _path, payload, _created_at in records
    ):
        raise RuntimeError(
            "no deployment manifest matches the live index; "
            "run a safe deploy before pruning"
        )

    retained_paths: set[Path] = set()
    protected_assets: set[str] = set()
    for position, (path, payload, created_at) in enumerate(records):
        is_current = (
            current_index_hash is not None
            and payload.get("index_sha256") == current_index_hash
        )
        if position < retain_manifests or created_at >= cutoff or is_current:
            retained_paths.add(path)
            protected_assets.update(str(item) for item in payload.get("assets", []))

    deleted_assets: list[str] = []
    assets_directory = destination / "assets"
    if assets_directory.is_dir():
        for asset in sorted(assets_directory.rglob("*"), reverse=True):
            if not asset.is_file():
                continue
            relative = asset.relative_to(destination).as_posix()
            modified_at = datetime.fromtimestamp(
                asset.stat().st_mtime,
                tz=UTC,
            )
            if relative not in protected_assets and modified_at < cutoff:
                asset.unlink()
                deleted_assets.append(relative)
        _remove_empty_directories(assets_directory)

    deleted_manifests: list[str] = []
    for path, _payload, _created_at in records:
        if path not in retained_paths:
            path.unlink()
            deleted_manifests.append(path.relative_to(destination).as_posix())
    if manifest_directory.is_dir() and not any(manifest_directory.iterdir()):
        manifest_directory.rmdir()

    return {
        "assets": deleted_assets,
        "manifests": deleted_manifests,
    }


@contextmanager
def _bundle_lock(destination: Path):
    """Hold a cross-process lock shared by deploy and prune.

    The lock lives beside the destination so ``deploy --fresh`` can replace
    the destination directory without replacing the inode/handle being used
    for coordination.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / f".{destination.name}.deploy.lock"
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
                    lock_file.seek(0)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _local_entry_files(index_html: str) -> set[Path]:
    parser = _IndexEntryParser()
    parser.feed(index_html)
    references: set[Path] = set()
    for reference in parser.references:
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc or reference.startswith("//"):
            continue
        raw_path = unquote(parsed.path).lstrip("/")
        if raw_path.startswith("./"):
            raw_path = raw_path[2:]
        if not raw_path:
            continue
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe index entry path: {reference}")
        references.add(relative)
    return references


def _refresh_live_manifest(destination: Path) -> None:
    index = destination / "index.html"
    if not index.is_file():
        return

    index_sha256 = hashlib.sha256(index.read_bytes()).hexdigest()
    manifest_directory = destination / MANIFEST_DIRECTORY
    matching: list[tuple[Path, dict[str, object]]] = []
    for path in manifest_directory.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("index_sha256") == index_sha256:
            matching.append((path, payload))

    if matching:
        matching.sort(key=lambda record: record[0], reverse=True)
        assets = [str(item) for item in matching[0][1].get("assets", [])]
    else:
        assets_directory = destination / "assets"
        assets = (
            sorted(
                path.relative_to(destination).as_posix()
                for path in assets_directory.rglob("*")
                if path.is_file()
            )
            if assets_directory.is_dir()
            else []
        )

    refreshed = _build_manifest(
        index,
        assets=assets,
        created_at=datetime.now(UTC),
    )
    refreshed_path = _write_manifest(destination, refreshed)
    for path, _payload in matching:
        if path != refreshed_path:
            path.unlink()


def _build_manifest(
    index: Path,
    *,
    assets: list[str],
    created_at: datetime,
) -> dict[str, object]:
    index_html = index.read_text(encoding="utf-8")
    return {
        "created_at": created_at.isoformat(),
        "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
        "entry_files": sorted(
            path.as_posix() for path in _local_entry_files(index_html)
        ),
        "assets": sorted(assets),
    }


def _write_manifest(
    destination: Path,
    manifest: dict[str, object],
) -> Path:
    created_at = datetime.fromisoformat(str(manifest["created_at"]))
    path = (
        destination
        / MANIFEST_DIRECTORY
        / f"{created_at.strftime('%Y%m%dT%H%M%S.%fZ')}-{secrets.token_hex(4)}.json"
    )
    _atomic_write_json(path, manifest)
    return path


def _remove_duplicate_manifests(
    destination: Path,
    *,
    index_sha256: str,
    keep: Path,
) -> None:
    manifest_directory = destination / MANIFEST_DIRECTORY
    for path in manifest_directory.glob("*.json"):
        if path == keep:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("index_sha256") == index_sha256:
            path.unlink()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{secrets.token_hex(6)}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _swap_index(source_index: Path, destination_index: Path) -> None:
    _atomic_copy(source_index, destination_index)


def _current_index_hash(index: Path) -> str | None:
    if not index.is_file():
        return None
    return hashlib.sha256(index.read_bytes()).hexdigest()


def _remove_empty_directories(root: Path) -> None:
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        reverse=True,
    ):
        if not any(directory.iterdir()):
            directory.rmdir()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    deploy = commands.add_parser("deploy")
    deploy.add_argument("source", type=Path)
    deploy.add_argument("destination", type=Path)
    deploy.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "Replace the destination instead of merging into it. For building "
            "a wheel, never for a destination that is being served."
        ),
    )

    prune = commands.add_parser("prune")
    prune.add_argument("destination", type=Path)
    prune.add_argument(
        "--retain-days",
        type=int,
        default=DEFAULT_RETAIN_DAYS,
    )
    prune.add_argument(
        "--retain-manifests",
        type=int,
        default=DEFAULT_RETAIN_MANIFESTS,
    )
    return parser


def main() -> None:
    arguments = _build_parser().parse_args()
    if arguments.command == "deploy":
        manifest = deploy_bundle(
            arguments.source,
            arguments.destination,
            fresh=arguments.fresh,
        )
        print(
            "deployed index after verifying "
            f"{len(manifest['entry_files'])} entry files"
        )
        return

    deleted = prune_bundle(
        arguments.destination,
        retain_days=arguments.retain_days,
        retain_manifests=arguments.retain_manifests,
    )
    print(
        f"pruned {len(deleted['assets'])} assets and "
        f"{len(deleted['manifests'])} manifests"
    )


if __name__ == "__main__":
    main()
