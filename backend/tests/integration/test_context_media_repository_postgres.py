import os
import threading

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import backend.context_media.adapters.sqlalchemy as context_media_sqlalchemy
from backend.context_media.adapters.sqlalchemy import (
    SqlAlchemyContextMediaRepository,
)
from backend.context_media.application import RegisterMediaAsset, SyncVersionUsage
from backend.context_media.domain import (
    ActorIdentity,
    ContextMediaOwnershipError,
)
from backend.models.context_media import (
    ContextMediaAsset,
    ContextMediaLogicalCharge,
    ContextMediaUsage,
)
from backend.scripts.seed_dev import seed_default_workspace
from backend.tests.integration.test_context_media_repository import (
    NOW,
    _candidate,
    _seed_page,
    _seed_shared_image,
)
from backend.tests.utils.postgres import (
    create_isolated_postgres_metadata,
    drop_reflected_postgres_metadata,
)

# Every test here reflects and drops the tables of whatever PostgreSQL database
# it is pointed at, and CI aims several of these files at one database, so they
# must not run beside each other under `-n`. The mark belongs in the module
# rather than in a conftest hook: xdist reads the group off the item while
# building its node id, before pytest_collection_modifyitems runs, so a mark
# added there arrives too late to reach the scheduler.
pytestmark = pytest.mark.xdist_group("postgres")


def _postgres_url() -> str | None:
    value = os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    return value if value and value.startswith("postgresql") else None


@pytest.mark.skipif(
    _postgres_url() is None,
    reason="requires the dedicated PostgreSQL migration test database",
)
def test_postgres_usage_and_tombstone_serialize_on_page_then_asset_lock(
    monkeypatch,
) -> None:
    database_url = _postgres_url()
    assert database_url is not None
    engine = create_engine(database_url)
    create_isolated_postgres_metadata(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    tombstone_page_locked = threading.Event()
    release_tombstone = threading.Event()
    usage_page_lock_attempted = threading.Event()
    tombstone_errors: list[BaseException] = []
    usage_errors: list[BaseException] = []
    tombstone_thread: threading.Thread | None = None
    usage_thread: threading.Thread | None = None
    original_lock_page = context_media_sqlalchemy._lock_context_page

    def tracking_lock_page(session, *, workspace_id, page_id):
        if threading.current_thread().name == "context-usage-writer":
            usage_page_lock_attempted.set()
        page = original_lock_page(
            session,
            workspace_id=workspace_id,
            page_id=page_id,
        )
        if threading.current_thread().name == "context-media-tombstone":
            tombstone_page_locked.set()
            assert release_tombstone.wait(timeout=10)
        return page

    monkeypatch.setattr(
        context_media_sqlalchemy,
        "_lock_context_page",
        tracking_lock_page,
    )
    try:
        actor = ActorIdentity("human", "postgres-context-media-race")
        with session_factory.begin() as session:
            workspace = seed_default_workspace(session)
            page, version = _seed_page(session, workspace.id, actor)
            blob, inspection_id = _seed_shared_image(session, workspace.id)
            repository = SqlAlchemyContextMediaRepository(session)
            asset = RegisterMediaAsset().execute(
                repository,
                _candidate(
                    workspace.id,
                    page.id,
                    version.id,
                    blob,
                    inspection_id,
                    actor,
                ),
                now=NOW,
            )
            unused = repository.get_page_asset_usage(
                workspace.id, page.id, asset.asset_id
            )
            assert unused is not None
            workspace_id = workspace.id
            page_id = page.id
            page_version_id = version.id
            asset_id = asset.asset_id
            logical_reference = asset.logical_reference

        def tombstone() -> None:
            try:
                with session_factory.begin() as session:
                    SqlAlchemyContextMediaRepository(session).tombstone_and_enqueue_gc(
                        unused
                    )
            except BaseException as exc:  # asserted below
                tombstone_errors.append(exc)

        def replace_usage() -> None:
            try:
                with session_factory.begin() as session:
                    SyncVersionUsage().execute(
                        SqlAlchemyContextMediaRepository(session),
                        workspace_id=workspace_id,
                        page_id=page_id,
                        page_version_id=page_version_id,
                        body_markdown=f"![race]({logical_reference})",
                    )
            except BaseException as exc:  # asserted below
                usage_errors.append(exc)

        tombstone_thread = threading.Thread(
            target=tombstone,
            name="context-media-tombstone",
        )
        usage_thread = threading.Thread(
            target=replace_usage,
            name="context-usage-writer",
        )
        tombstone_thread.start()
        assert tombstone_page_locked.wait(timeout=10)
        usage_thread.start()
        assert usage_page_lock_attempted.wait(timeout=10)
        assert usage_thread.is_alive()
        release_tombstone.set()
        tombstone_thread.join(timeout=10)
        usage_thread.join(timeout=10)

        assert not tombstone_thread.is_alive()
        assert not usage_thread.is_alive()
        assert tombstone_errors == []
        assert len(usage_errors) == 1
        assert isinstance(usage_errors[0], ContextMediaOwnershipError)
        with session_factory.begin() as session:
            persisted = session.get(
                ContextMediaAsset,
                {"workspace_id": workspace_id, "id": asset_id},
            )
            charge = session.scalar(
                select(ContextMediaLogicalCharge).where(
                    ContextMediaLogicalCharge.workspace_id == workspace_id,
                    ContextMediaLogicalCharge.asset_id == asset_id,
                )
            )
            usage = session.scalar(
                select(ContextMediaUsage).where(
                    ContextMediaUsage.workspace_id == workspace_id,
                    ContextMediaUsage.asset_id == asset_id,
                )
            )
            assert persisted is not None
            assert persisted.association_state == "tombstoned"
            assert charge is not None and charge.state == "released"
            assert usage is None
    finally:
        release_tombstone.set()
        if tombstone_thread is not None:
            tombstone_thread.join(timeout=10)
        if usage_thread is not None:
            usage_thread.join(timeout=10)
        drop_reflected_postgres_metadata(engine)
        engine.dispose()
