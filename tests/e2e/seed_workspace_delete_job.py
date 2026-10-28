"""Create a retained, unclaimable job in the wrapper's isolated E2E database."""

import os
import sys
from uuid import UUID, uuid4

from sqlalchemy.orm import sessionmaker

from backend.app.db import make_engine
from backend.models.job import Job


def main() -> None:
    database_url = os.environ.get("DTXWIKI_E2E_DATABASE_URL")
    if not database_url or not os.environ.get("DTXWIKI_E2E_DATABASE_PATH"):
        raise RuntimeError("Use npm run test:e2e; the isolated database is required")
    workspace_id = UUID(sys.argv[1])
    engine = make_engine(database_url)
    try:
        with sessionmaker(engine).begin() as session:
            session.add(
                Job(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    job_type="context_media_orphan_sweep",
                    trigger_type="manual",
                    status="failed",
                    idempotency_key=str(uuid4()),
                )
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
