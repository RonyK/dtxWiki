"""Bare Git storage with independent handles and explicit pack installation."""

from __future__ import annotations

import filecmp
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pygit2

from backend.context_versions.adapters.repository_guard import management_guard
from backend.context_versions.codecs import codec_for
from backend.context_versions.domain import (
    ContentDescriptor,
    ContentPayload,
    ContextVersionStorageError,
    LooseCandidate,
    PackCandidate,
    PackResult,
    PruneResult,
    RepositoryStats,
    StoredFile,
)

# Library-wide options are configured at import, never toggled by requests.
pygit2.settings.enable_fsync_gitdir(True)
pygit2.settings.enable_strict_hash_verification(True)

_HEAD_PREFIX = "refs/heads/context/"
_HEX = re.compile(r"[0-9a-f]+\Z")


@contextmanager
def _storage_errors():
    try:
        yield
    except ContextVersionStorageError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise ContextVersionStorageError("context_snapshot_invalid") from exc
    except pygit2.GitError as exc:
        code = (
            "context_repository_busy"
            if "lock" in str(exc).lower()
            else "context_repository_unavailable"
        )
        raise ContextVersionStorageError(code) from exc
    except OSError as exc:
        raise ContextVersionStorageError("context_repository_unavailable") from exc


class GitSnapshotStore:
    def __init__(self, repositories_root: Path) -> None:
        self.repositories_root = Path(repositories_root).expanduser().resolve()

    @property
    def libgit2_version(self) -> str:
        return pygit2.LIBGIT2_VERSION

    def library_version(self) -> str:
        return f"pygit2 {pygit2.__version__}; libgit2 {pygit2.LIBGIT2_VERSION}"

    def repository_path(self, workspace_id: UUID) -> Path:
        return self.repositories_root / f"{UUID(str(workspace_id))}.git"

    def exists(self, workspace_id: UUID) -> bool:
        path = self.repository_path(workspace_id)
        if path.is_symlink():
            raise ContextVersionStorageError("context_snapshot_invalid")
        return path.is_dir()

    def management_guard(self, workspace_id: UUID):
        return management_guard(self.repositories_root, UUID(str(workspace_id)))

    @contextmanager
    def _repository(self, workspace_id: UUID, *, create: bool = False):
        path = self.repository_path(workspace_id)
        with _storage_errors():
            if not self.exists(workspace_id):
                if not create:
                    raise ContextVersionStorageError("context_snapshot_invalid")
                with self.management_guard(workspace_id):
                    if not self.exists(workspace_id):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        initialized = pygit2.init_repository(path, bare=True)
                        initialized.free()
                        # Persist the new repository entry as well as its Git
                        # contents before a publication can commit its SQL OID.
                        _fsync_directory(path)
                        _fsync_directory(path.parent)
                        _fsync_directory(path.parent.parent)
            repository = pygit2.Repository(
                path,
                flags=pygit2.enums.RepositoryOpenFlag.NO_SEARCH,
            )
            try:
                if not repository.is_bare:
                    raise ContextVersionStorageError("context_snapshot_invalid")
                yield repository
            finally:
                repository.free()

    def commit(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        version_id: UUID,
        created_at: datetime,
        payload: ContentPayload,
        parents: Sequence[str],
    ) -> str:
        codec = codec_for(payload.descriptor)
        codec.decode(payload.data)
        with self._repository(workspace_id, create=True) as repository:
            parent_oids = []
            for oid in parents:
                parent = _commit(repository, oid)
                _page_blob(repository, parent, page_id)
                parent_oids.append(parent.id)
            blob_oid = repository.create_blob(payload.data)
            content = repository.TreeBuilder()
            content.insert(codec.filename, blob_oid, pygit2.GIT_FILEMODE_BLOB)
            page = repository.TreeBuilder()
            page.insert(
                str(UUID(str(page_id))), content.write(), pygit2.GIT_FILEMODE_TREE
            )
            root = repository.TreeBuilder()
            root.insert("context", page.write(), pygit2.GIT_FILEMODE_TREE)
            instant = (
                created_at.replace(tzinfo=UTC)
                if created_at.tzinfo is None
                else created_at
            )
            signature = pygit2.Signature(
                "dtxWiki",
                "git@noreply.dtxwiki.com",
                int(instant.timestamp()),
                0,
            )
            return str(
                repository.create_commit(
                    None,
                    signature,
                    signature,
                    f"Context snapshot {UUID(str(version_id))}\n",
                    root.write(),
                    parent_oids,
                )
            )

    def read(
        self,
        *,
        workspace_id: UUID,
        page_id: UUID,
        commit_oid: str,
        descriptor: ContentDescriptor,
    ) -> bytes:
        codec = codec_for(descriptor)
        with self._repository(workspace_id) as repository:
            blob = _page_blob(
                repository, _commit(repository, commit_oid), page_id, codec.filename
            )
            data = blob.data
            codec.decode(data)
            return data

    def set_head(
        self,
        workspace_id: UUID,
        page_id: UUID,
        commit_oid: str | None,
    ) -> None:
        if commit_oid is None and not self.exists(workspace_id):
            return
        with self._repository(workspace_id) as repository:
            ref = f"{_HEAD_PREFIX}{UUID(str(page_id))}"
            if commit_oid is None:
                if ref in repository.references:
                    repository.references.delete(ref)
                return
            commit = _commit(repository, commit_oid)
            _page_blob(repository, commit, page_id)
            repository.create_reference(ref, commit.id, force=True)

    def list_heads(self, workspace_id: UUID) -> dict[UUID, str]:
        if not self.exists(workspace_id):
            return {}
        with self._repository(workspace_id) as repository:
            result = {}
            for name in repository.references:
                if name.startswith(_HEAD_PREFIX):
                    page_id = UUID(name.removeprefix(_HEAD_PREFIX))
                    result[page_id] = str(repository.references[name].resolve().target)
            return result

    def inspect(self, workspace_id: UUID) -> RepositoryStats:
        if not self.exists(workspace_id):
            return RepositoryStats(exists=False)
        with self._repository(workspace_id):
            root = self.repository_path(workspace_id)
            loose = _loose_candidates(root)
            packs = tuple((root / "objects" / "pack").glob("*.pack"))
            return RepositoryStats(
                exists=True,
                loose_object_count=len(loose),
                loose_bytes=sum(item.file.byte_size for item in loose),
                pack_count=len(packs),
                pack_bytes=sum(path.stat().st_size for path in packs),
            )

    def pack(self, workspace_id: UUID, root_oids: Iterable[str]) -> PackResult:
        """Create a self-contained pack; caller holds the management guard."""
        with self._repository(workspace_id) as repository:
            root = self.repository_path(workspace_id)
            loose = _loose_candidates(root)
            packs = _pack_candidates(root)
            objects = _reachable_objects(repository, root_oids)
            if not objects:
                return PackResult(workspace_id, (), frozenset(), loose, packs)
            with tempfile.TemporaryDirectory(
                prefix=".pack-stage-", dir=root
            ) as temporary:
                stage = Path(temporary)
                pack_dir = stage / "pack"
                pack_dir.mkdir()
                builder = pygit2.PackBuilder(repository)
                builder.set_threads(1)
                for oid in sorted(objects):
                    builder.add(pygit2.Oid(hex=oid))
                builder.write(pack_dir)
                del builder
                odb = _pack_odb(stage)
                try:
                    packed = frozenset(str(oid) for oid in odb)
                    if packed != objects:
                        raise ContextVersionStorageError("context_snapshot_invalid")
                    for oid in packed:
                        native_oid = pygit2.Oid(hex=oid)
                        if odb.read(native_oid) != repository.odb.read(native_oid):
                            raise ContextVersionStorageError("context_snapshot_invalid")
                finally:
                    del odb
                files = tuple(sorted(pack_dir.iterdir()))
                if {file.suffix for file in files} != {".pack", ".idx"} or len(
                    files
                ) != 2:
                    raise ContextVersionStorageError("context_snapshot_invalid")
                destination = root / "objects" / "pack"
                destination.mkdir(exist_ok=True)
                installed = []
                # Readers cannot see an index before its complete pack exists.
                for source in sorted(files, key=lambda item: item.suffix == ".idx"):
                    _fsync_file(source)
                    target = destination / source.name
                    if not target.exists() or not filecmp.cmp(
                        source, target, shallow=False
                    ):
                        os.replace(source, target)
                    installed.append(str(target.relative_to(root)))
                _fsync_directory(destination)
                _fsync_directory(root / "objects")
                installed_files = tuple(
                    _stored_file(root, root / relative) for relative in installed
                )
                return PackResult(
                    workspace_id,
                    tuple(installed),
                    packed,
                    loose,
                    packs,
                    installed_files,
                )

    def prune_packed(self, workspace_id: UUID, result: PackResult) -> PruneResult:
        """Remove only captured representations covered by the verified new pack."""
        if result.workspace_id != workspace_id:
            raise ContextVersionStorageError("context_snapshot_invalid")
        if not result.pack_files:
            return PruneResult()
        with self._repository(workspace_id):
            root = self.repository_path(workspace_id)
            protected = frozenset(result.pack_files)
            _verify_installed_files(root, result)
            candidates = [
                item.file
                for item in result.loose_candidates
                if item.oid in result.packed_oids
            ]
            for pack in result.pack_candidates:
                if (
                    pack.object_oids <= result.packed_oids
                    and all(file.relative_path not in protected for file in pack.files)
                    and all(_unchanged(root, file) for file in pack.files)
                ):
                    candidates.extend(pack.files)
            removed_files = removed_bytes = 0
            for file in candidates:
                if file.relative_path in protected or not _unchanged(root, file):
                    continue
                _safe_member(root, file.relative_path).unlink()
                removed_files += 1
                removed_bytes += file.byte_size
            _fsync_directory(root / "objects" / "pack")
            return PruneResult(removed_files, removed_bytes)

    def purge_workspace(self, workspace_id: UUID) -> list[str]:
        """Caller holds the management guard through DB deletion and this call."""
        with _storage_errors():
            if not self.exists(workspace_id):
                return []
            path = self.repository_path(workspace_id)
            shutil.rmtree(path)
            _fsync_directory(path.parent)
            return [str(path)]


def _commit(repository: pygit2.Repository, oid: str) -> pygit2.Commit:
    commit = repository[pygit2.Oid(hex=oid)]
    if not isinstance(commit, pygit2.Commit):
        raise ContextVersionStorageError("context_snapshot_invalid")
    return commit


def _only_entry(tree: pygit2.Tree, name: str, mode: int):
    if len(tree) != 1:
        raise ContextVersionStorageError("context_snapshot_invalid")
    entry = tree[name]
    if entry.filemode != mode:
        raise ContextVersionStorageError("context_snapshot_invalid")
    return entry


def _page_blob(repository, commit, page_id: UUID, filename: str | None = None):
    context = _only_entry(commit.tree, "context", pygit2.GIT_FILEMODE_TREE)
    page = _only_entry(
        repository[context.id], str(UUID(str(page_id))), pygit2.GIT_FILEMODE_TREE
    )
    contents = repository[page.id]
    if len(contents) != 1:
        raise ContextVersionStorageError("context_snapshot_invalid")
    entry = next(iter(contents))
    if (
        entry.filemode != pygit2.GIT_FILEMODE_BLOB
        or (filename is not None and entry.name != filename)
        or not entry.name.startswith("content.")
    ):
        raise ContextVersionStorageError("context_snapshot_invalid")
    blob = repository[entry.id]
    if not isinstance(blob, pygit2.Blob):
        raise ContextVersionStorageError("context_snapshot_invalid")
    return blob


def _reachable_objects(repository, roots: Iterable[str]) -> frozenset[str]:
    todo = list(roots)
    found = set()
    while todo:
        oid = todo.pop()
        if oid in found:
            continue
        obj = repository[pygit2.Oid(hex=oid)]
        found.add(str(obj.id))
        if isinstance(obj, pygit2.Commit):
            todo.append(str(obj.tree_id))
            todo.extend(str(parent) for parent in obj.parent_ids)
        elif isinstance(obj, pygit2.Tree):
            todo.extend(str(entry.id) for entry in obj)
        elif not isinstance(obj, pygit2.Blob):
            raise ContextVersionStorageError("context_snapshot_invalid")
    return frozenset(found)


def _stored_file(root: Path, path: Path) -> StoredFile:
    if path.is_symlink() or not path.is_file():
        raise ContextVersionStorageError("context_snapshot_invalid")
    stat = path.stat()
    return StoredFile(
        str(path.relative_to(root)),
        stat.st_size,
        stat.st_dev,
        stat.st_ino,
        stat.st_mtime_ns,
    )


def _loose_candidates(root: Path) -> tuple[LooseCandidate, ...]:
    candidates = []
    for directory in (root / "objects").iterdir():
        if len(directory.name) != 2 or not _HEX.fullmatch(directory.name):
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise ContextVersionStorageError("context_snapshot_invalid")
        for path in directory.iterdir():
            oid = directory.name + path.name
            if len(oid) == 40 and _HEX.fullmatch(oid):
                candidates.append(LooseCandidate(_stored_file(root, path), oid))
    return tuple(candidates)


def _pack_odb(objects_dir: Path):
    odb = pygit2.Odb()
    odb.add_backend(pygit2.OdbBackendPack(str(objects_dir)), 1)
    return odb


def _pack_object_ids(
    root: Path,
    pack: Path,
    index: Path,
    *,
    expected: frozenset[str] | None = None,
) -> frozenset[str]:
    with tempfile.TemporaryDirectory(prefix=".pack-check-", dir=root) as temporary:
        objects = Path(temporary)
        destination = objects / "pack"
        destination.mkdir()
        # Hard links avoid copying a potentially large immutable old pack.
        os.link(pack, destination / pack.name)
        os.link(index, destination / index.name)
        odb = _pack_odb(objects)
        try:
            object_ids = frozenset(str(oid) for oid in odb)
            if not object_ids:
                # An ignored index proves nothing about the pack's contents.
                # Never treat unknown contents as a wholly covered old pack.
                raise ContextVersionStorageError("context_snapshot_invalid")
            if expected is not None and object_ids != expected:
                raise ContextVersionStorageError("context_snapshot_invalid")
            for oid in object_ids:
                odb.read(pygit2.Oid(hex=oid))
            return object_ids
        finally:
            # Release native mmap handles before removing temporary hard links,
            # including on platforms that cannot unlink an open mapped file.
            del odb


def _pack_candidates(root: Path) -> tuple[PackCandidate, ...]:
    result = []
    for pack in sorted((root / "objects" / "pack").glob("*.pack")):
        index = pack.with_suffix(".idx")
        files = (_stored_file(root, index), _stored_file(root, pack))
        objects = _pack_object_ids(root, pack, index)
        result.append(PackCandidate(files, objects))
    return tuple(result)


def _safe_member(root: Path, relative: str) -> Path:
    path = root / relative
    if (
        Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or path.is_symlink()
    ):
        raise ContextVersionStorageError("context_snapshot_invalid")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ContextVersionStorageError("context_snapshot_invalid")
    return path


def _unchanged(root: Path, file: StoredFile) -> bool:
    path = _safe_member(root, file.relative_path)
    if not path.exists():
        return False
    return _stored_file(root, path) == file


def _verify_installed_files(root: Path, result: PackResult) -> None:
    """Cheap final check under SQL lock; pack verification ran before install.

    The workspace management guard spans build and pruning. Ordinary snapshot
    writers do not replace pack files, so unchanged installed file identities
    preserve the verification proof without rereading all history under SQL.
    """
    paths = tuple(_safe_member(root, relative) for relative in result.pack_files)
    if len(paths) != 2 or {path.suffix for path in paths} != {".pack", ".idx"}:
        raise ContextVersionStorageError("context_snapshot_invalid")
    if len(result.installed_files) != 2 or {
        file.relative_path for file in result.installed_files
    } != set(result.pack_files):
        raise ContextVersionStorageError("context_snapshot_invalid")
    if not all(_unchanged(root, file) for file in result.installed_files):
        raise ContextVersionStorageError("context_snapshot_invalid")


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
