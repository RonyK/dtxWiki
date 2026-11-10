"""Publication must retain its SQL lock until Git and SQL finish together."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pygit2
import pytest
from sqlalchemy import select
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
from backend.services.context_version_storage import (
    ContextStorageServiceError,
    align_publication,
    verify_context_storage,
)
from backend.services.db_write_intent import acquire_write_intent
from backend.services.review_service import ReviewService
from backend.tests.integration.test_context_review_publish import (
    ACTOR_UUID,
    _approve,
    _detail,
    _published_page_with_draft,
)
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
            assert first._body_md is None
            assert first.block_map is None
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


def test_git_bodies_follow_publication_undo_and_storage_failure(session_factory):
    """One DB traversal covers the shared read/head rule and its rollback."""
    with session_factory.begin() as session:
        workspace, published, draft = _published_page_with_draft(session, "Git bodies")
        workspace_id, page_id = workspace.id, published.page_id
        old_id, current_id = published.draft_version_id, draft.draft_version_id
        old = session.get(ContextPageVersion, old_id)
        current = session.get(ContextPageVersion, current_id)
        old_body, current_body = old.body_md, current.body_md
        original_saved_at = old.last_saved_at
        approved = _approve(session, workspace_id, draft.review_item_id)
        store = session.info["context_snapshot_store"]
        current_oid = current.commit_oid
        assert old._body_md is None and old.block_map is None
        assert old.body_md == old_body and old._body_md is None
        assert old.last_saved_at == original_saved_at
        # The synonym preserves raw SQL expressions and never materializes a
        # historical read back into the database.
        assert (
            session.scalar(
                select(ContextPageVersion.body_md).where(
                    ContextPageVersion.id == old_id
                )
            )
            is None
        )
        ReviewService().undo_decision(
            session,
            workspace_id,
            draft.review_item_id,
            approved.undo.decision_id,
            actor_kind="human",
            actor_id=str(ACTOR_UUID),
            actor_user_id=ACTOR_UUID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        assert old._body_md == old_body
        assert old.last_saved_at == original_saved_at
        assert current._body_md == current_body and current.commit_oid is None
        assert store.list_heads(workspace_id)[page_id] == old.commit_oid
        _approve(session, workspace_id, draft.review_item_id)
        assert old._body_md is None and current.commit_oid == current_oid
        assert current._body_md == current_body
        verify_context_storage(session, store)
        # A readable Git copy must not hide an absent current DB FULL copy
        # from operator verification, even though ordinary reads can recover it.
        current.body_md = None
        with pytest.raises(ContextStorageServiceError):
            verify_context_storage(session, store)
        current.body_md = current_body
        page = session.get(ContextPage, page_id)
        page.current_published_version_id = old_id
        store.set_head(workspace_id, page_id, old.commit_oid)
        with pytest.raises(ContextStorageServiceError):
            verify_context_storage(session, store)
        page.current_published_version_id = current_id
        store.set_head(workspace_id, page_id, current_oid)
        with pytest.raises(ContextStorageServiceError):
            align_publication(session, page, None)
        assert store.list_heads(workspace_id)[page_id] == current_oid

    with session_factory() as session:
        historical = session.get(ContextPageVersion, old_id)
        session.expire(historical, ["_body_md"])
        session.expunge(historical)
        with pytest.raises(ContextStorageServiceError) as detached:
            _ = historical.body_md
        assert detached.value.error_code == "context_repository_unavailable"

    # A new publication may not destroy the DB's last exact copy if its
    # supposedly immutable Git snapshot differs. All SQL changes roll back.
    with pytest.raises(
        ContextStorageServiceError, match="storage operation"
    ) as failure:
        with session_factory.begin() as session:
            page = get_page_by_id_for_update(session, page_id)
            current = session.get(ContextPageVersion, current_id)
            current.body_md += "\nUnexpected mutation"
            candidate = ContextPageVersion(
                workspace_id=workspace_id,
                page_id=page_id,
                version_no=3,
                parent_version_id=current_id,
                body_md="# Next\n",
                content_hash=content_hash("# Next\n"),
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id=str(ACTOR_UUID),
                version_status="draft",
            )
            session.add(candidate)
            session.flush()
            page.current_version_id = page.latest_draft_version_id = candidate.id
            apply_context_publication(
                session,
                page=page,
                version=candidate,
                require_latest_draft=True,
            )
    assert failure.value.error_code == "context_snapshot_invalid"
    with session_factory() as session:
        assert session.get(ContextPageVersion, current_id)._body_md == current_body
        assert store.list_heads(workspace_id)[page_id] == current_oid

    repository = store.repository_path(workspace_id)
    unavailable = repository.with_suffix(".offline")
    repository.rename(unavailable)
    try:
        with session_factory() as session:
            assert session.get(ContextPageVersion, current_id).body_md == current_body
            with pytest.raises(ContextStorageServiceError) as failure:
                _ = session.get(ContextPageVersion, old_id).body_md
            assert failure.value.error_code == "context_snapshot_invalid"
            # Review rows carry no body: the unreadable comparison base only
            # marks the diff unavailable and the read still succeeds.
            detail = _detail(session, workspace_id, draft.review_item_id)
            content = detail.evidence.operations[0].content
            assert content.can_diff is False
            assert content.unavailable_reason == "baseline_unavailable"
    finally:
        unavailable.rename(repository)
