from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.models.base import Base
from backend.models.context import ContextPage
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.page_service import (
    PagePublicVersionConflictError,
    PageService,
)


def test_concurrent_unlisted_share_and_publish_never_exposes_stale_version(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'public-share-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory.begin() as session:
            workspace = seed_default_workspace(session)
            workspace.visibility = "team"
        service = PageService(factory)
        original = service.write_page(
            workspace_slug="default",
            slug="share-publish-race",
            title="Share Publish Race",
            body_md="# Share Publish Race\n\nVersion one.",
            actor_kind="human",
            actor_id="dev-local-user",
            actor_provenance="browser_session",
        )
        start = Barrier(2, timeout=10)

        def share():
            start.wait()
            try:
                service.set_page_public(
                    original.id,
                    True,
                    actor_kind="human",
                    actor_id="dev-local-user",
                    actor_role="owner",
                    actor_provenance="browser_session",
                    acknowledge_public_exposure=True,
                    expected_published_version_id=original.version_id,
                )
                return "shared"
            except PagePublicVersionConflictError:
                return "stale"

        def publish():
            start.wait()
            return service.update_page(
                page_id=original.id,
                title="Share Publish Race",
                body_md="# Share Publish Race\n\nVersion two.",
                actor_kind="human",
                actor_id="dev-local-user",
                base_version_id=original.version_id,
                base_content_hash=original.content_hash,
                actor_provenance="browser_session",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            share_future = executor.submit(share)
            publish_future = executor.submit(publish)
            share_result = share_future.result(timeout=20)
            published = publish_future.result(timeout=20)

        assert share_result in {"shared", "stale"}
        with factory() as session:
            workspace = session.scalar(
                select(Workspace).where(Workspace.slug == "default")
            )
            page = session.get(ContextPage, original.id)
            assert workspace.visibility == "team"
            assert page.current_published_version_id == published.version_id
            assert page.is_public is False
            assert page.public_exposure_scope is None
            assert page.public_exposed_version_id is None
    finally:
        engine.dispose()
