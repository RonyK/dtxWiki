"""SQL publication bridge; callers hold the existing publication transaction."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.context_versions.application import ContextVersionWriter
from backend.context_versions.domain import (
    ContentDescriptor,
    ContentPayload,
    ContextVersionStorageError,
    SnapshotRecord,
)
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.repositories.workspace_scoped import (
    get_in_workspace,
    select_in_workspace,
    select_projection_in_workspace,
)
from backend.services.read_errors import ReadServiceError


class ContextStorageServiceError(ReadServiceError):
    def __init__(self, code: str) -> None:
        self.error_code = code
        super().__init__("Context version storage operation failed")


def resolve_writer(
    session: Session, writer: ContextVersionWriter | None = None
) -> ContextVersionWriter:
    resolved = (
        writer if writer is not None else session.info.get("context_version_writer")
    )
    if resolved is None:
        raise ContextStorageServiceError("context_repository_unavailable")
    return resolved


def descriptor_for(version: ContextPageVersion) -> ContentDescriptor:
    return ContentDescriptor(
        content_format=version.content_format,
        format_version=version.format_version,
        media_type=version.media_type,
        encoding=version.encoding,
    )


def prepare_publication(
    session: Session,
    page: ContextPage,
    version: ContextPageVersion,
    writer: ContextVersionWriter | None = None,
) -> SnapshotRecord:
    """Prepare immutable bytes before changing SQL snapshot states."""
    writer = resolve_writer(session, writer)
    previous = (
        get_in_workspace(
            session,
            ContextPageVersion,
            workspace_id=page.workspace_id,
            resource_id=page.current_published_version_id,
        )
        if page.current_published_version_id
        else None
    )
    if previous is not None and (
        previous.page_id != page.id or not previous.commit_oid
    ):
        raise ContextStorageServiceError("context_snapshot_invalid")
    try:
        if not writer.repository_exists(page.workspace_id):
            has_objects = session.scalar(
                select_projection_in_workspace(
                    ContextPageVersion,
                    workspace_id=page.workspace_id,
                    columns=(ContextPageVersion.id,),
                    predicates=(ContextPageVersion.commit_oid.is_not(None),),
                ).limit(1)
            )
            if has_objects is not None:
                raise ContextStorageServiceError("context_snapshot_invalid")
        return writer.prepare_snapshot(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_id=version.id,
            created_at=version.created_at,
            payload=ContentPayload(
                data=version.body_md.encode("utf-8"), descriptor=descriptor_for(version)
            ),
            parents=[previous.commit_oid]
            if previous is not None and previous.id != version.id
            else [],
            existing_oid=version.commit_oid,
        )
    except ContextVersionStorageError as exc:
        raise ContextStorageServiceError(exc.code) from exc


def align_publication(
    session: Session, page: ContextPage, commit_oid: str | None, writer=None
) -> None:
    try:
        resolve_writer(session, writer).align_head(
            page.workspace_id, page.id, commit_oid
        )
    except ContextVersionStorageError as exc:
        raise ContextStorageServiceError(exc.code) from exc


def verify_context_storage(session: Session, store) -> dict[str, int]:
    """Installation operator verification, scoped independently per workspace."""
    checked = workspaces = 0
    for workspace_id in session.scalars(select(Workspace.id)):
        checked += _verify_workspace(session, store, workspace_id)
        workspaces += 1
    return {"verified_snapshots": checked, "workspaces": workspaces}


def _verify_workspace(session: Session, store, workspace_id) -> int:
    versions = list(
        session.scalars(
            select_in_workspace(ContextPageVersion, workspace_id=workspace_id)
        )
    )
    by_id = {version.id: version for version in versions}
    checked = 0
    for version in versions:
        snapshot = version.version_status in {
            "published_snapshot",
            "superseded_snapshot",
        }
        if snapshot != (version.commit_oid is not None):
            raise ContextStorageServiceError("context_snapshot_invalid")
        if not snapshot:
            continue
        try:
            data = store.read(
                workspace_id=workspace_id,
                page_id=version.page_id,
                commit_oid=version.commit_oid,
                descriptor=descriptor_for(version),
            )
        except ContextVersionStorageError as exc:
            raise ContextStorageServiceError(exc.code) from exc
        if len(data) != version.content_byte_size or data != version.body_md.encode(
            "utf-8"
        ):
            raise ContextStorageServiceError("context_snapshot_invalid")
        checked += 1
    heads = {}
    for page in session.scalars(
        select_in_workspace(ContextPage, workspace_id=workspace_id)
    ):
        if page.current_published_version_id is not None:
            version = by_id.get(page.current_published_version_id)
            if version is None or version.page_id != page.id or not version.commit_oid:
                raise ContextStorageServiceError("context_snapshot_invalid")
            heads[page.id] = version.commit_oid
    try:
        if store.list_heads(workspace_id) != heads:
            raise ContextStorageServiceError("context_snapshot_invalid")
    except ContextVersionStorageError as exc:
        raise ContextStorageServiceError(exc.code) from exc
    return checked


def maintenance_observations(session) -> dict:
    """Last job outcomes; no new mutable storage or separate metrics table."""
    from backend.models.job import Job

    summary = {}
    workspace_ids = list(session.scalars(select(Workspace.id)))
    for state in ("completed", "failed"):
        jobs = [
            session.scalar(
                select_in_workspace(
                    Job,
                    workspace_id=workspace_id,
                    conditions=(
                        Job.job_type == "context_repository_maintenance",
                        Job.status == state,
                    ),
                )
                .order_by(Job.updated_at.desc())
                .limit(1)
            )
            for workspace_id in workspace_ids
        ]
        job = max(
            (job for job in jobs if job is not None),
            key=lambda row: row.updated_at,
            default=None,
        )
        summary["last_success" if state == "completed" else "last_failure"] = (
            {
                "at": job.updated_at.isoformat(),
                "workspace_id": str(job.workspace_id),
                "error": job.last_error if state == "failed" else None,
            }
            if job
            else None
        )
    return summary
