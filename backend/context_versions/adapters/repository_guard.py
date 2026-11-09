"""Use the existing strict process-shared resource lock for repository admin."""

from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from backend.context_versions.domain import ContextVersionStorageError
from backend.services.ops.runtime_lock import RuntimeLockHeldError, acquire_path


@contextmanager
def management_guard(root: Path, workspace_id: UUID):
    try:
        guard = acquire_path(
            root / ".locks" / f"{workspace_id}.lock",
            "exclusive",
            strict=True,
        )
    except RuntimeLockHeldError as exc:
        raise ContextVersionStorageError("context_repository_busy") from exc
    except OSError as exc:
        raise ContextVersionStorageError("context_repository_unavailable") from exc
    with guard:
        yield
