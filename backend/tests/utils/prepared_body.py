"""Create authored version fixtures through the production preparation gate."""

from uuid import UUID

from sqlalchemy.orm import Session

from backend.domain.prepared_body import PreparedBody
from backend.models.context import ContextPage
from backend.models.workspace import Workspace
from backend.services.body_preparer import BodyInput, BodyPreparer, WriteContext
from backend.services.lint import LintPolicy


class _HistoricalBodyPolicy(LintPolicy):
    def fix_on_save(self, module) -> bool:
        # Model stored historical bytes through the same validation/hash gate.
        return False


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
    return BodyPreparer(policy_factory=_HistoricalBodyPolicy).prepare(
        BodyInput(body_md),
        WriteContext(
            workspace=workspace,
            page=page,
            session=session,
            actor_kind=actor_kind,
            actor_id=actor_id,
            actor_provenance="browser_session",
        ),
    )
