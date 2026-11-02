"""The focused-view boundary for incremental read-together projection.

Legacy REFERENCED touches and citation registrations retain their existing
history/presentation meaning. Neither is evidence of a focused body view.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from backend.policy.mcp_scopes import FOCUSED_READ_TOOLS
from backend.services.mcp_artifact_activity import InsertedArtifactUsage

FOCUSED_VIEW_VERB = "viewed"


def prepare_artifact_usage(
    session: Session,
    *,
    workspace_id: UUID,
    verb: str,
    result_status: str,
    tool_name: str,
) -> None:
    """Take the usage lock before artifact writes, only for a real view."""
    if (
        verb != FOCUSED_VIEW_VERB
        or result_status != "succeeded"
        or tool_name not in FOCUSED_READ_TOOLS
    ):
        return
    # The legacy runtime does not emit VIEWED. Keep the feature dependency
    # behind its explicit persisted verb until read-together is installed.
    from backend.services.co_access import lock_co_access_workspace

    lock_co_access_workspace(session, workspace_id)


def project_inserted_artifact_usage(
    session: Session, event: InsertedArtifactUsage
) -> None:
    """Project a new VIEWED row; the caller must not pass duplicate inserts."""
    if (
        event.verb != FOCUSED_VIEW_VERB
        or event.result_status != "succeeded"
        or event.tool_name not in FOCUSED_READ_TOOLS
    ):
        return
    from backend.services.co_access import record_co_access_read

    record_co_access_read(
        session,
        workspace_id=event.workspace_id,
        chat_session_id=event.chat_session_id,
        artifact_id=event.artifact_id,
    )
