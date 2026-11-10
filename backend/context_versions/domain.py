"""Storage values shared by Context content use cases and adapters."""

from dataclasses import dataclass
from uuid import UUID


class ContextVersionStorageError(RuntimeError):
    """A storage failure with a stable, transport-independent error code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = self.error_code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class ContentDescriptor:
    content_format: str = "markdown"
    format_version: int = 1
    media_type: str = "text/markdown"
    encoding: str | None = "utf-8"


@dataclass(frozen=True)
class ContentPayload:
    data: bytes
    descriptor: ContentDescriptor = ContentDescriptor()


@dataclass(frozen=True)
class SnapshotRecord:
    commit_oid: str
    content_byte_size: int
    descriptor: ContentDescriptor


@dataclass(frozen=True)
class RepositoryStats:
    exists: bool
    loose_object_count: int = 0
    loose_bytes: int = 0
    pack_count: int = 0
    pack_bytes: int = 0


@dataclass(frozen=True)
class StoredFile:
    relative_path: str
    byte_size: int
    device: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True)
class LooseCandidate:
    file: StoredFile
    oid: str


@dataclass(frozen=True)
class PackCandidate:
    files: tuple[StoredFile, StoredFile]
    object_oids: frozenset[str]


@dataclass(frozen=True)
class PackResult:
    workspace_id: UUID
    pack_files: tuple[str, ...]
    packed_oids: frozenset[str]
    loose_candidates: tuple[LooseCandidate, ...] = ()
    pack_candidates: tuple[PackCandidate, ...] = ()
    installed_files: tuple[StoredFile, ...] = ()


@dataclass(frozen=True)
class PruneResult:
    removed_files: int = 0
    removed_bytes: int = 0
