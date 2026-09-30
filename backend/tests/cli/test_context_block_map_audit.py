from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from typer.testing import CliRunner

from backend.app.db import make_engine, make_session_factory
from backend.cli import main as cli_main
from backend.models.base import Base
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.utils.block_identity import compute_block_map_with_ids

ACTOR_ID = "00000000-0000-4000-8000-00000000b10c"


def _version(page_id, workspace_id, *, number, status, body, block_map):
    return ContextPageVersion(
        id=uuid4(),
        workspace_id=workspace_id,
        page_id=page_id,
        version_no=number,
        body_md=body,
        content_hash=str(number) * 64,
        graph_status="linear",
        actor_type="human",
        actor_id=UUID(ACTOR_ID),
        version_status=status,
        created_by_actor_kind="human",
        created_by_actor_id=ACTOR_ID,
        block_map=block_map,
    )


def test_block_map_audit_counts_drifted_maps_by_status_and_writes_nothing(
    tmp_path: Path,
):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'block-map-audit.db'}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    body = "# Audit\n\nOne.\n\nTwo."
    with session_factory.begin() as session:
        seed_default_workspace(session)
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        page = ContextPage(
            id=uuid4(),
            workspace_id=workspace_id,
            slug="block-map-audit",
            title="Block Map Audit",
            page_type="overview",
            tags=[],
            aliases=[],
            lifecycle_status="draft",
        )
        session.add(page)
        session.flush()
        in_step = compute_block_map_with_ids(
            body, previous_body_md=None, previous_block_map=None
        )
        drifted = compute_block_map_with_ids(
            "# Audit\n\nOld one.\n\nTwo.",
            previous_body_md=None,
            previous_block_map=None,
        )
        session.add_all(
            [
                _version(
                    page.id,
                    workspace_id,
                    number=1,
                    status="superseded_snapshot",
                    body=body,
                    block_map=in_step,
                ),
                _version(
                    page.id,
                    workspace_id,
                    number=2,
                    status="editing",
                    body=body,
                    block_map=drifted,
                ),
                _version(
                    page.id,
                    workspace_id,
                    number=3,
                    status="draft",
                    body=body,
                    block_map=None,
                ),
            ]
        )

    result = CliRunner().invoke(
        cli_main.app, ["context", "block-map-audit", "--db", database_url]
    )

    assert result.exit_code == 0, result.output
    assert "status=editing total=1 drifted=1 missing=0" in result.stdout
    assert "status=draft total=1 drifted=0 missing=1" in result.stdout
    assert "status=superseded_snapshot total=1 drifted=0 missing=0" in result.stdout
    with session_factory() as session:
        stored = session.scalars(
            select(ContextPageVersion.block_map).where(
                ContextPageVersion.version_status == "editing"
            )
        ).one()
    assert stored == drifted
