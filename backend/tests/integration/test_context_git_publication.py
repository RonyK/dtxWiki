"""Publication must retain its SQL lock until Git and SQL finish together."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pygit2
import pytest
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from backend.app.db import make_engine
from backend.cli import main as cli_main
from backend.context_versions.adapters.git_libgit2 import GitSnapshotStore
from backend.context_versions.application import ContextVersionWriter
from backend.context_versions.codecs import MarkdownCodec
from backend.context_versions.domain import ContextVersionStorageError
from backend.models.context import ContextPage, ContextPageVersion
from backend.repositories.context_page_repo import get_page_by_id_for_update
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_lifecycle import apply_context_publication
from backend.services.context_version_storage import ContextStorageServiceError
from backend.services.db_write_intent import acquire_write_intent
from backend.utils.content_hash import content_hash


def test_same_context_publication_failure_rolls_back_before_waiting_retry(
    _empty_sqlite_schema,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DTXWIKI_DATA_DIR", str(tmp_path))
    database = tmp_path / "publication.sqlite"
    with sqlite3.connect(database) as connection:
        _empty_sqlite_schema.backup(connection)
    engine = make_engine(f"sqlite+pysqlite:///{database}")
    store = GitSnapshotStore(tmp_path / "repositories")
    writer = ContextVersionWriter(store, store)
    factory = sessionmaker(
        engine,
        expire_on_commit=False,
        info={"context_snapshot_store": store, "context_version_writer": writer},
    )
    ready, release, attempted, acquired = Event(), Event(), Event(), Event()
    failed_oids = []

    class FailAfterHead(ContextVersionWriter):
        def align_head(self, workspace_id, page_id, commit_oid):
            super().align_head(workspace_id, page_id, commit_oid)
            failed_oids.append(commit_oid)
            ready.set()
            assert release.wait(timeout=15)
            raise ContextVersionStorageError("context_repository_unavailable")

    def publish(version_id, *, fail=False):
        with factory.begin() as session:
            if not fail:
                attempted.set()
            acquire_write_intent(session)
            if not fail:
                acquired.set()
            page = get_page_by_id_for_update(session, page_id)
            version = session.get(ContextPageVersion, version_id)
            apply_context_publication(
                session,
                page=page,
                version=version,
                require_latest_draft=True,
                context_version_writer=FailAfterHead(store, store) if fail else writer,
            )
            return version.commit_oid

    try:
        with factory.begin() as session:
            workspace = seed_default_workspace(session)
            workspace_id = workspace.id
            page = ContextPage(
                workspace_id=workspace_id,
                slug="git-publication",
                title="Git publication",
            )
            session.add(page)
            session.flush()
            page_id = page.id
            first = ContextPageVersion(
                workspace_id=workspace_id,
                page_id=page_id,
                version_no=1,
                body_md="# First\n\nOriginal content.\n",
                content_hash=content_hash("# First\n\nOriginal content.\n"),
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id=str(uuid4()),
                version_status="draft",
                created_at=datetime(2026, 11, 9, tzinfo=UTC),
                block_map=[{"id": "preserved-first-map"}],
            )
            session.add(first)
            session.flush()
            first_id = first.id
            page.current_version_id = page.latest_draft_version_id = first_id
            acquire_write_intent(session)
            apply_context_publication(
                session,
                page=page,
                version=first,
                require_latest_draft=True,
                context_version_writer=writer,
            )
            original_oid, original_body = first.commit_oid, first.body_md
            second = ContextPageVersion(
                workspace_id=workspace_id,
                page_id=page_id,
                version_no=2,
                parent_version_id=first_id,
                body_md="# Second\r\n\r\nNew content.  \n",
                content_hash=content_hash("# Second\r\n\r\nNew content.  \n"),
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id=str(uuid4()),
                version_status="draft",
                created_at=datetime(2026, 11, 9, 1, tzinfo=UTC),
                block_map=[{"id": "preserved-second-map"}],
            )
            session.add(second)
            session.flush()
            second_id, second_body = second.id, second.body_md
            page.current_version_id = page.latest_draft_version_id = second_id
        assert store.list_heads(workspace_id) == {page_id: original_oid}

        with ThreadPoolExecutor(max_workers=2) as executor:
            failing = executor.submit(publish, second_id, fail=True)
            try:
                assert ready.wait(timeout=15)
                waiting = executor.submit(publish, second_id)
                assert attempted.wait(timeout=15)
                # Prove a real SQLite writer is held. No sleep or throughput
                # threshold stands in for the transaction's locking behavior.
                with sqlite3.connect(database, timeout=0) as probe:
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        probe.execute("UPDATE workspace SET slug=slug WHERE 1=0")
                assert not acquired.is_set()
                with factory() as session:
                    assert (
                        session.get(ContextPage, page_id).current_published_version_id
                        == first_id
                    )
                    assert session.get(ContextPageVersion, second_id).commit_oid is None
                assert store.list_heads(workspace_id) == {page_id: failed_oids[0]}
            finally:
                release.set()
            with pytest.raises(ContextStorageServiceError):
                failing.result(timeout=15)
            published_oid = waiting.result(timeout=15)

        assert acquired.is_set()
        assert published_oid == failed_oids[0]
        with factory() as session:
            first = session.get(ContextPageVersion, first_id)
            second = session.get(ContextPageVersion, second_id)
            assert first.version_status == "superseded_snapshot"
            assert first.body_md == original_body
            assert first.block_map == [{"id": "preserved-first-map"}]
            assert second.version_status == "published_snapshot"
            assert second.body_md == second_body
            assert second.content_byte_size == len(second_body.encode("utf-8"))
            assert second.block_map == [{"id": "preserved-second-map"}]
            assert (
                session.get(ContextPage, page_id).current_published_version_id
                == second_id
            )
        assert store.list_heads(workspace_id) == {page_id: published_oid}
        assert store.read(
            workspace_id=workspace_id,
            page_id=page_id,
            commit_oid=published_oid,
            descriptor=MarkdownCodec.descriptor,
        ) == second_body.encode("utf-8")
        repository = pygit2.Repository(store.repository_path(workspace_id))
        try:
            assert [str(oid) for oid in repository[published_oid].parent_ids] == [
                original_oid
            ]
        finally:
            repository.free()
        verified = CliRunner().invoke(
            cli_main.app,
            ["context", "storage", "verify", "--db", str(engine.url)],
        )
        assert verified.exit_code == 0, verified.output
        assert "Verified 2 snapshots" in verified.output
        # Replaying this published snapshot reuses its commit and aligns a
        # stale derived head without ever making the commit its own parent.
        store.set_head(workspace_id, page_id, original_oid)
        stale = CliRunner().invoke(
            cli_main.app,
            ["context", "storage", "verify", "--db", str(engine.url)],
        )
        assert stale.exit_code == 2
        assert "context_snapshot_invalid" in stale.output
        with factory.begin() as session:
            acquire_write_intent(session)
            page = get_page_by_id_for_update(session, page_id)
            apply_context_publication(
                session,
                page=page,
                version=session.get(ContextPageVersion, second_id),
                require_latest_draft=False,
                context_version_writer=writer,
            )
        assert store.list_heads(workspace_id) == {page_id: published_oid}
    finally:
        release.set()
        engine.dispose()
