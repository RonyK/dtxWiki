"""Short SQL phases around immutable Git pack creation."""

from dataclasses import asdict
from uuid import UUID

from backend.context_versions.domain import ContextVersionStorageError
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.repositories.workspace_scoped import select_projection_in_workspace
from backend.services.db_write_intent import acquire_write_intent


class ContextRepositoryMaintenance:
    def __init__(self, *, session_for, store, writer) -> None:
        self.session_for = session_for
        self.store = store
        self.writer = writer

    @staticmethod
    def _positions(session, workspace_id: UUID):
        pages = session.execute(
            select_projection_in_workspace(
                ContextPage,
                workspace_id=workspace_id,
                columns=(ContextPage.id, ContextPage.current_published_version_id),
            )
        ).all()
        versions = {
            version_id: (page_id, oid)
            for version_id, page_id, oid in session.execute(
                select_projection_in_workspace(
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    columns=(
                        ContextPageVersion.id,
                        ContextPageVersion.page_id,
                        ContextPageVersion.commit_oid,
                    ),
                    predicates=(ContextPageVersion.commit_oid.is_not(None),),
                )
            )
        }
        heads = {}
        for page_id, version_id in pages:
            if version_id is not None:
                stored = versions.get(version_id)
                if stored is None or stored[0] != page_id:
                    raise ContextVersionStorageError("context_snapshot_invalid")
                heads[page_id] = stored[1]
        return heads, frozenset(oid for _, oid in versions.values())

    def run(self, workspace_id: UUID) -> dict:
        if not self.store.exists(workspace_id):
            return {"skipped": "repository_absent"}
        try:
            with self.store.management_guard(workspace_id):
                return self._run_guarded(workspace_id)
        except ContextVersionStorageError as exc:
            if exc.code == "context_repository_busy":
                return {"skipped": "repository_busy"}
            raise

    def _run_guarded(self, workspace_id: UUID) -> dict:
        factory = self.session_for(workspace_id)
        with factory.begin() as session:
            if session.get_bind().dialect.name != "sqlite":
                return {"skipped": "online_maintenance_requires_sqlite"}
            acquire_write_intent(session)
            if session.get(Workspace, workspace_id) is None:
                return {"skipped": "workspace_absent"}
            heads, roots = self._positions(session, workspace_id)
            for page_id in self.store.list_heads(workspace_id).keys() | heads.keys():
                self.writer.align_head(workspace_id, page_id, heads.get(page_id))
        if not roots:
            return {"stats": asdict(self.store.inspect(workspace_id))}
        packed = self.store.pack(workspace_id, roots)
        removed = None
        with factory.begin() as session:
            acquire_write_intent(session)
            if session.get(Workspace, workspace_id) is not None and self._positions(
                session, workspace_id
            ) == (heads, roots):
                removed = self.store.prune_packed(workspace_id, packed)
        return {
            "stats": asdict(self.store.inspect(workspace_id)),
            "pruned": asdict(removed) if removed else None,
        }
