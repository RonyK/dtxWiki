"""Add exact-candidate review siblings only in the wrapper's isolated database."""

import os
import sys
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.db import make_engine
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.review import ReviewItem
from backend.services.db_write_intent import acquire_workspace_write_intent


def main() -> None:
    database_url = os.environ.get("DTXWIKI_E2E_DATABASE_URL")
    if not database_url or not os.environ.get("DTXWIKI_E2E_DATABASE_PATH"):
        raise RuntimeError("Use npm run test:e2e; the isolated database is required")
    workspace_id, review_id = (UUID(value) for value in sys.argv[1:3])
    engine = make_engine(database_url)
    try:
        with sessionmaker(engine).begin() as session:
            acquire_workspace_write_intent(session)
            review = session.get(ReviewItem, review_id)
            if review is None or review.workspace_id != workspace_id:
                raise RuntimeError("Review must belong to the test workspace")
            source = session.get(ChangeSet, review.change_set_id)
            operation = session.scalar(
                select(ChangeOperation).where(
                    ChangeOperation.change_set_id == review.change_set_id,
                    ChangeOperation.target_type == "context_page",
                )
            )
            if source is None or operation is None or not operation.diff_ref:
                raise RuntimeError("The fixture requires a real submitted candidate")
            review.risk_level = "medium"
            source.risk_level = "medium"
            for risk in ("low", "high"):
                change_set = ChangeSet(
                    workspace_id=workspace_id,
                    reason=f"Parallel {risk} risk review of the exact candidate",
                    trigger_type="context_page_update",
                    actor_type=source.actor_type,
                    actor_id=source.actor_id,
                    status="pending_review",
                    risk_level=risk,
                )
                session.add(change_set)
                session.flush()
                session.add(
                    ChangeOperation(
                        workspace_id=workspace_id,
                        change_set_id=change_set.id,
                        target_type="context_page",
                        target_id=operation.target_id,
                        operation=operation.operation,
                        diff_ref=operation.diff_ref,
                        diff_summary=operation.diff_summary,
                    )
                )
                session.add(
                    ReviewItem(
                        workspace_id=workspace_id,
                        change_set_id=change_set.id,
                        review_type="context_update",
                        status="pending",
                        risk_level=risk,
                        decision_summary=f"Parallel {risk} risk review",
                    )
                )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
