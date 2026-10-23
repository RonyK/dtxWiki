"""Create authored version fixtures through the production preparation gate."""

from uuid import UUID

from sqlalchemy.orm import Session

from backend.domain.prepared_body import PreparedBody
from backend.models.context import ContextPage
from backend.models.workspace import Workspace
from backend.services.body_preparer import BodyInput, BodyPreparer, WriteContext


def prepare_context_body(
    session: Session,
    *,
    page_id: UUID,
    body_md: str,
    actor_kind: str,
    actor_id: str,
) -> PreparedBody:
    page = session.get(ContextPage, page_id)
    assert page is not None
    workspace = session.get(Workspace, page.workspace_id)
    assert workspace is not None
    return BodyPreparer().prepare(
        BodyInput(body_md),
        WriteContext(
            workspace=workspace,
            page=page,
            session=session,
            actor_kind=actor_kind,
            actor_id=actor_id,
            actor_provenance="browser_session",
        ),
        # These fixtures model stored historical bytes rather than a user's
        # current Auto-fix policy. Validation and hash derivation still run.
        transitional_skip_fixes=True,
    )
