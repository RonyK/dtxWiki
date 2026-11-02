"""Best-effort derived co-access projection; source trace capture stays durable."""

import logging
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.policy.mcp_scopes import FOCUSED_READ_TOOLS
from backend.services.mcp_artifact_activity import InsertedArtifactUsage

logger = logging.getLogger(__name__)
FOCUSED_VIEW_VERB = "viewed"


@contextmanager
def co_access_write_budget(session: Session):
    """A slow administrative writer must not stall a focused MCP read."""
    if session.get_bind().dialect.name != "postgresql":
        yield
        return
    previous = session.scalar(text("SELECT current_setting('lock_timeout')"))
    session.execute(text("SELECT set_config('lock_timeout', '25ms', true)"))
    yield
    session.execute(
        text("SELECT set_config('lock_timeout', :value, true)"), {"value": previous}
    )


def project_inserted_artifact_usage(
    session: Session, event: InsertedArtifactUsage
) -> None:
    """Keep raw events even if derived usage work fails or maintenance owns it."""
    if (
        event.verb != FOCUSED_VIEW_VERB
        or event.result_status != "succeeded"
        or event.tool_name not in FOCUSED_READ_TOOLS
    ):
        return
    from backend.services.co_access import record_co_access_read

    try:
        # begin_nested flushes the original trace before the derived savepoint.
        # Its rollback cannot erase the call, artifact or independent read base.
        with session.begin_nested():
            with co_access_write_budget(session):
                record_co_access_read(
                    session,
                    workspace_id=event.workspace_id,
                    chat_session_id=event.chat_session_id,
                    artifact_id=event.artifact_id,
                )
    except Exception:
        logger.exception("Co-access projection deferred; original read trace retained")
        from backend.services.job_queue import JobQueueService

        try:
            with session.begin_nested():
                JobQueueService().enqueue(
                    session,
                    event.workspace_id,
                    "co_access_repair",
                    "usage",
                    idempotency_key=f"co-access:{event.artifact_id}",
                    target_ref={
                        "chat_session_id": str(event.chat_session_id),
                        "artifact_id": str(event.artifact_id),
                    },
                )
        except Exception:
            logger.exception(
                "Could not queue co-access repair; source evidence retained"
            )
