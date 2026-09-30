from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.app.db import make_engine
from backend.models.base import Base
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.scripts.seed_dev import seed_default_workspace


def _seed_owned_ready_version(
    session_factory: sessionmaker,
) -> tuple[UUID, UUID, UUID]:
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        first_item = LibraryItem(
            workspace_id=workspace.id,
            connector_type="web",
            uri="https://example.test/first",
        )
        second_item = LibraryItem(
            workspace_id=workspace.id,
            connector_type="web",
            uri="https://example.test/second",
        )
        session.add_all([first_item, second_item])
        session.flush()
        first_version = LibraryItemVersion(
            workspace_id=workspace.id,
            library_item_id=first_item.id,
            version_no=1,
            content_hash="first-content",
            raw_storage_ref="inline://first",
            processing_status="completed",
        )
        second_version = LibraryItemVersion(
            workspace_id=workspace.id,
            library_item_id=second_item.id,
            version_no=1,
            content_hash="second-content",
            raw_storage_ref="inline://second",
            processing_status="completed",
        )
        session.add_all([first_version, second_version])
        session.flush()
        first_item.current_ready_version_id = first_version.id
        session.flush()
        return first_item.id, first_version.id, second_version.id


def _assert_cross_item_pointer_is_rejected(
    session_factory: sessionmaker,
    *,
    first_item_id: UUID,
    first_version_id: UUID,
    second_version_id: UUID,
) -> None:
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            first_item = session.get(LibraryItem, first_item_id)
            assert first_item is not None
            first_item.current_ready_version_id = second_version_id
            session.flush()

    with session_factory.begin() as session:
        first_item = session.get(LibraryItem, first_item_id)
        assert first_item is not None
        assert first_item.current_ready_version_id == first_version_id


def test_model_schema_enforces_ready_version_ownership_and_preserves_set_null(
    tmp_path: Path,
):
    engine = make_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'model-ready-owner.db').as_posix()}"
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        item_id, owned_version_id, foreign_version_id = _seed_owned_ready_version(
            session_factory
        )
        _assert_cross_item_pointer_is_rejected(
            session_factory,
            first_item_id=item_id,
            first_version_id=owned_version_id,
            second_version_id=foreign_version_id,
        )

        with session_factory.begin() as session:
            session.execute(
                delete(LibraryItemVersion).where(
                    LibraryItemVersion.id == owned_version_id
                )
            )
        with session_factory.begin() as session:
            item = session.get(LibraryItem, item_id)
            assert item is not None
            assert item.current_ready_version_id is None
    finally:
        engine.dispose()


def test_epic1_migration_installs_and_removes_ready_version_ownership(tmp_path: Path):
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / 'migration-ready-owner.db').as_posix()}"
    )
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    engine = make_engine(database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        inspector = inspect(engine)
        item_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("library_item")
        }
        version_uniques = {
            unique["name"]: tuple(unique["column_names"])
            for unique in inspector.get_unique_constraints("library_item_version")
        }
        # The final 0015 table plan extends this 2-column ownership FK to a
        # workspace-scoped 3-column FK (renamed accordingly).
        ownership = item_foreign_keys[
            "fk_library_item_current_ready_version_id_workspace_scoped"
        ]
        assert ownership["constrained_columns"] == [
            "workspace_id",
            "current_ready_version_id",
            "id",
        ]
        assert ownership["referred_table"] == "library_item_version"
        assert ownership["referred_columns"] == [
            "workspace_id",
            "id",
            "library_item_id",
        ]
        assert version_uniques["library_item_version_id_item_uq"] == (
            "id",
            "library_item_id",
        )

        item_id, owned_version_id, foreign_version_id = _seed_owned_ready_version(
            session_factory
        )
        _assert_cross_item_pointer_is_rejected(
            session_factory,
            first_item_id=item_id,
            first_version_id=owned_version_id,
            second_version_id=foreign_version_id,
        )
    finally:
        engine.dispose()

    command.downgrade(config, "0001_initial_release")
    downgraded_engine = make_engine(database_url)
    try:
        inspector = inspect(downgraded_engine)
        assert "current_ready_version_id" not in {
            column["name"] for column in inspector.get_columns("library_item")
        }
        assert "library_item_version_id_item_uq" not in {
            unique["name"]
            for unique in inspector.get_unique_constraints("library_item_version")
        }
    finally:
        downgraded_engine.dispose()
