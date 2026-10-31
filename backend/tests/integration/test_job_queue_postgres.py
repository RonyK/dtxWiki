import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from backend.models.job import Job
from backend.models.workspace import Workspace
from backend.services.job_queue import JobQueueService
from backend.tests.utils.postgres import create_isolated_postgres_metadata

# Every test here reflects and drops the tables of whatever PostgreSQL database
# it is pointed at, and CI aims several of these files at one database, so they
# must not run beside each other under `-n`. The mark belongs in the module
# rather than in a conftest hook: xdist reads the group off the item while
# building its node id, before pytest_collection_modifyitems runs, so a mark
# added there arrives too late to reach the scheduler.
pytestmark = pytest.mark.xdist_group("postgres")


def postgres_test_url() -> str | None:
    database_url = os.getenv("DTXWIKI_POSTGRES_TEST_DATABASE_URL") or os.getenv(
        "DTXWIKI_DATABASE_URL"
    )
    if database_url and database_url.startswith("postgresql"):
        return database_url
    return None


@pytest.mark.skipif(
    postgres_test_url() is None,
    reason=(
        "requires DTXWIKI_POSTGRES_TEST_DATABASE_URL or PostgreSQL "
        "DTXWIKI_DATABASE_URL"
    ),
)
def test_postgres_conditional_claim_prevents_double_claim():
    engine = create_engine(postgres_test_url())
    create_isolated_postgres_metadata(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    queue = JobQueueService()
    workspace_id = None
    job_id = None

    try:
        with session_factory.begin() as session:
            workspace = Workspace(
            slug=f"postgres-conditional-claim-{uuid4()}",
            title="Postgres conditional claim test",
            name="Postgres conditional claim test",
            )
            session.add(workspace)
            session.flush()
            workspace_id = workspace.id
            job = queue.enqueue(
                session=session,
                workspace_id=workspace.id,
                job_type="extraction",
                trigger_type="postgres_conditional_claim_test",
                idempotency_key=f"postgres-conditional-claim-{uuid4()}",
            )
            job_id = job.id

        with session_factory.begin() as session_one:
            first_claim = queue.claim_next(
                session=session_one,
                workspace_id=workspace_id,
            )
        with session_factory.begin() as session_two:
            second_claim = queue.claim_next(
                session=session_two,
                workspace_id=workspace_id,
            )

        assert first_claim is not None
        assert first_claim.id == job_id
        assert second_claim is None
    finally:
        if workspace_id is not None:
            with session_factory.begin() as session:
                if job_id is not None:
                    session.execute(delete(Job).where(Job.id == job_id))
                session.execute(delete(Workspace).where(Workspace.id == workspace_id))
