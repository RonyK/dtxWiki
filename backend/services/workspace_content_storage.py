"""Compose workspace byte cleanup without owning its database deletion."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from backend.context_versions.ports import WorkspaceRepositoryPurger


class WorkspaceContentStorageError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = "purge_unavailable"):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = 503


class WorkspaceByteStore(Protocol):
    def purge_workspace(self, workspace_id: UUID) -> list[str]: ...


class WorkspaceByteStoreProvider(Protocol):
    def store_for(self, workspace_id: UUID) -> WorkspaceByteStore: ...


class PreparedWorkspacePurge(AbstractContextManager):
    """Hold the repository guard from before SQL deletion through both purges."""

    def __init__(
        self,
        workspace_id: UUID,
        library: WorkspaceByteStore,
        repositories: WorkspaceRepositoryPurger,
    ) -> None:
        self.workspace_id = workspace_id
        self.library = library
        self.repositories = repositories
        self._guard: AbstractContextManager | None = None

    def __enter__(self):
        try:
            self._guard = self.repositories.management_guard(self.workspace_id)
            self._guard.__enter__()
        except Exception as exc:
            raise WorkspaceContentStorageError(
                "workspace repository is busy or unavailable",
                error_code=getattr(exc, "code", "context_repository_unavailable"),
            ) from exc
        return self

    def __exit__(self, *exc_info):
        if self._guard is not None:
            try:
                self._guard.__exit__(*exc_info)
            finally:
                self._guard = None

    def purge(self) -> None:
        if self._guard is None:
            raise RuntimeError("workspace purge requires the repository guard")
        failures = []
        for store in (self.library, self.repositories):
            try:
                store.purge_workspace(self.workspace_id)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise WorkspaceContentStorageError(
                "workspace content cleanup failed",
                error_code="workspace_content_cleanup_failed",
            ) from failures[0]


class WorkspaceContentStorage:
    def __init__(
        self,
        *,
        blob_store_provider: WorkspaceByteStoreProvider,
        repository_purger: WorkspaceRepositoryPurger,
    ) -> None:
        self.blob_store_provider = blob_store_provider
        self.repository_purger = repository_purger

    def prepare(self, workspace_id: UUID) -> PreparedWorkspacePurge:
        library = self.blob_store_provider.store_for(workspace_id)
        if not callable(getattr(library, "purge_workspace", None)) or not all(
            callable(getattr(self.repository_purger, method, None))
            for method in ("management_guard", "purge_workspace")
        ):
            raise WorkspaceContentStorageError(
                "storage backend does not support workspace purge"
            )
        return PreparedWorkspacePurge(workspace_id, library, self.repository_purger)
