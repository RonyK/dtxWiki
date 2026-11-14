"""Storage values shared by Context content use cases and adapters."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
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
    # None preserves the content-only format; an empty list is archived metadata.
    block_map: list[dict] | None = None


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


class ContextMergeError(RuntimeError):
    """Invalid merge input or a native calculation failure; never a resolution."""

    def __init__(self, code: str) -> None:
        self.code = self.error_code = code
        super().__init__(code)


MergeChoice = Literal["keep_both", "latest"]
MergeSide = Literal["ours", "theirs"]


@dataclass(frozen=True)
class PageMetadata:
    title: str = ""
    page_type: str = ""
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    placement_parent_node_id: UUID | None = None


@dataclass(frozen=True)
class MergeSnapshot:
    workspace_id: UUID
    page_id: UUID
    version_id: UUID
    body_md: str
    metadata: PageMetadata
    last_saved_at: datetime
    block_map: list[dict] | None = None


@dataclass(frozen=True)
class VersionNode:
    workspace_id: UUID
    page_id: UUID
    version_id: UUID
    parents: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class TextConflict:
    ours: str
    theirs: str
    keep_both_start: int
    keep_both_end: int
    latest_start: int
    latest_end: int


@dataclass(frozen=True)
class TextMergeResult:
    clean: str | None
    keep_both: str
    latest: str
    conflicts: tuple[TextConflict, ...] = ()


@dataclass(frozen=True)
class MergeCandidate:
    body_md: str
    metadata: PageMetadata
    block_map: list[dict]
    result_hash: str
    conflicts: tuple[TextConflict, ...] = ()


@dataclass(frozen=True)
class MergeOutcome:
    clean: MergeCandidate | None
    keep_both: MergeCandidate
    latest: MergeCandidate
    metadata_conflicts: tuple[str, ...] = ()
    # A virtual base may itself need a choice even if the final text is clean.
    base_conflicted: bool = False

    @property
    def requires_choice(self) -> bool:
        return self.clean is None
