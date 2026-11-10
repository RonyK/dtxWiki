"""One populated upgrade/downgrade/re-upgrade traversal per dialect."""

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pygit2
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.context_versions.adapters.git_libgit2 import GitSnapshotStore
from backend.context_versions.codecs import MarkdownCodec
from backend.context_versions.domain import ContentPayload
from backend.models.context import ContextPage
from backend.scripts.seed_dev import seed_default_workspace
from backend.tests.utils.postgres import drop_reflected_postgres_metadata
from backend.utils.content_hash import content_hash

PARENT = "00ba02774c4a"
REVISION = "74e5d4f912a6"
BODY_REVISION = "9d8695419436"
NEW_COLUMNS = {
    "commit_oid",
    "content_format",
    "format_version",
    "media_type",
    "encoding",
    "content_byte_size",
}


@pytest.mark.release_migration
@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_context_git_snapshot_migration_traversal(tmp_path, dialect):
    url = (
        f"sqlite+pysqlite:///{tmp_path / 'migration.sqlite'}"
        if dialect == "sqlite"
        else os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    )
    if not url or not url.startswith(dialect):
        pytest.skip("requires a dedicated PostgreSQL migration test database")
    engine = sa.create_engine(url)
    assert not (set(sa.inspect(engine).get_table_names()) - {"alembic_version"})
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", url)
    root = tmp_path / "repositories"
    page_id, other_page_id = uuid4(), uuid4()
    current_id, historical_id, editing_id, other_id = (uuid4() for _ in range(4))
    other_history_id, branch_id, draft_id, discarded_id = (uuid4() for _ in range(4))
    now = datetime(2026, 11, 9, 1, 2, 3, 456789, tzinfo=UTC)
    body = "# 원문\r\n\r\n끝 공백  \r\nNFD: e\u0301"
    block_map = [{"id": "preserved-identity", "kind": "p"}]
    key = (lambda value: value.hex) if dialect == "sqlite" else (lambda value: value)
    try:
        command.upgrade(config, PARENT)
        with Session(engine) as session:
            workspace_id = seed_default_workspace(session).id
            session.commit()
        with engine.begin() as connection:
            connection.execute(
                ContextPage.__table__.insert(),
                [
                    dict(
                        id=page_id,
                        workspace_id=workspace_id,
                        slug="git-source",
                        title="Git source",
                        created_at=now,
                        updated_at=now,
                    ),
                    dict(
                        id=other_page_id,
                        workspace_id=workspace_id,
                        slug="git-other",
                        title="Git other",
                        created_at=now,
                        updated_at=now,
                    ),
                ],
            )
            versions = sa.Table(
                "context_page_version", sa.MetaData(), autoload_with=connection
            )
            # Current is deliberately the lower version_no: the DB pointer wins,
            # while Git migration connects historical snapshots before it.
            rows = [
                (current_id, page_id, 1, "published_snapshot", body, None),
                (
                    historical_id,
                    page_id,
                    2,
                    "superseded_snapshot",
                    "earlier\r\n",
                    current_id,
                ),
                (editing_id, page_id, 3, "editing", "private work", current_id),
                (other_id, other_page_id, 1, "published_snapshot", "other", None),
                (
                    other_history_id,
                    other_page_id,
                    2,
                    "superseded_snapshot",
                    "reconcile base",
                    other_id,
                ),
                (branch_id, other_page_id, 3, "draft", "branch work", other_history_id),
                (draft_id, page_id, 4, "draft", "pending work", current_id),
                (discarded_id, page_id, 5, "discarded", "discarded work", current_id),
            ]
            for version_id, owner, number, status, content, parent in rows:
                connection.execute(
                    versions.insert().values(
                        id=key(version_id),
                        workspace_id=key(workspace_id),
                        page_id=key(owner),
                        version_no=number,
                        version_status=status,
                        body_md=content,
                        block_map=block_map,
                        parent_version_id=key(parent) if parent else None,
                        content_hash=content_hash(content),
                        graph_status="branched"
                        if version_id == branch_id
                        else "linear",
                        reconcile_manifest=(
                            {"preserved": True} if version_id == branch_id else None
                        ),
                        rendering_profile="gfm_mermaid",
                        actor_type="human",
                        created_by_actor_kind="human",
                        created_by_actor_id="migration-user",
                        created_at=now,
                        last_saved_at=now,
                    )
                )
            for owner, published in ((page_id, current_id), (other_page_id, other_id)):
                connection.execute(
                    ContextPage.__table__.update()
                    .where(ContextPage.id == owner)
                    .values(
                        current_version_id=published,
                        current_published_version_id=published,
                    )
                )

        # Same traversal: prove missing root is rejected before the new DDL.
        with pytest.raises(RuntimeError, match="absolute.*Config.attributes"):
            command.upgrade(config, REVISION)
        assert not NEW_COLUMNS.intersection(
            {
                item["name"]
                for item in sa.inspect(engine).get_columns("context_page_version")
            }
        )
        config.attributes["context_repositories_root"] = root
        command.upgrade(config, REVISION)
        with engine.connect() as connection:
            table = sa.Table(
                "context_page_version", sa.MetaData(), autoload_with=connection
            )
            converted = {
                UUID(str(row["id"])): row
                for row in connection.execute(sa.select(table)).mappings()
            }
        repo = pygit2.Repository(str(root / f"{workspace_id}.git"))
        current_oid = converted[current_id]["commit_oid"]
        historical_oid = converted[historical_id]["commit_oid"]
        assert repo[historical_oid].parent_ids == []
        assert repo[current_oid].parent_ids == [pygit2.Oid(hex=historical_oid)]
        assert repo.references[f"refs/heads/context/{page_id}"].target == pygit2.Oid(
            hex=current_oid
        )
        assert repo[converted[other_id]["commit_oid"]].parent_ids == [
            pygit2.Oid(hex=converted[other_history_id]["commit_oid"])
        ]
        assert converted[editing_id]["commit_oid"] is None
        for version_id, _owner, _number, _status, content, parent in rows:
            row = converted[version_id]
            assert row["body_md"] == content and row["block_map"] == block_map
            assert (
                UUID(str(row["parent_version_id"]))
                if row["parent_version_id"]
                else None
            ) == parent
            assert (
                row["content_format"],
                row["format_version"],
                row["media_type"],
                row["encoding"],
            ) == ("markdown", 1, "text/markdown", "utf-8")
            if row["commit_oid"]:
                commit = repo[row["commit_oid"]]
                assert commit.message == f"Context snapshot {version_id}\n"
                assert commit.author.email == "git@noreply.dtxwiki.com"
                assert commit.committer.offset == 0
                assert row["content_byte_size"] == len(content.encode("utf-8"))
                blob = repo[commit.tree["context"].id]
                blob = repo[blob[str(_owner)].id]
                assert repo[blob["content.md"].id].data == content.encode("utf-8")

        runtime = GitSnapshotStore(tmp_path / "runtime-repositories")
        runtime_historical = runtime.commit(
            workspace_id=workspace_id,
            page_id=page_id,
            version_id=historical_id,
            created_at=now,
            payload=MarkdownCodec().encode("earlier\r\n"),
            parents=[],
        )
        assert runtime_historical == historical_oid
        assert (
            runtime.commit(
                workspace_id=workspace_id,
                page_id=page_id,
                version_id=current_id,
                created_at=now,
                payload=MarkdownCodec().encode(body),
                parents=[runtime_historical],
            )
            == current_oid
        )
        columns = {
            item["name"]: item
            for item in sa.inspect(engine).get_columns("context_page_version")
        }
        assert not columns["body_md"]["nullable"]
        assert not columns["content_format"]["nullable"]
        assert not columns["format_version"]["nullable"]
        assert not columns["media_type"]["nullable"]
        assert columns["encoding"]["nullable"]
        assert columns["commit_oid"]["nullable"]

        command.downgrade(config, PARENT)
        assert not NEW_COLUMNS.intersection(
            {
                item["name"]
                for item in sa.inspect(engine).get_columns("context_page_version")
            }
        )
        assert (root / f"{workspace_id}.git").is_dir()
        with engine.connect() as connection:
            table = sa.Table(
                "context_page_version", sa.MetaData(), autoload_with=connection
            )
            preserved = {
                UUID(str(row["id"])): row
                for row in connection.execute(sa.select(table)).mappings()
            }
        assert all(
            preserved[item]["body_md"] == converted[item]["body_md"]
            and preserved[item]["block_map"] == block_map
            for item in converted
        )
        command.upgrade(config, REVISION)
        with engine.connect() as connection:
            table = sa.Table(
                "context_page_version", sa.MetaData(), autoload_with=connection
            )
            restored = {
                UUID(str(row["id"])): row["commit_oid"]
                for row in connection.execute(sa.select(table)).mappings()
            }
        assert restored == {item: row["commit_oid"] for item, row in converted.items()}

        # Mix new body+identity snapshots with the immutable content-only
        # history created above. Only an exact archived map can release SQL.
        snapshots = GitSnapshotStore(root)
        archived_history_oid = snapshots.commit(
            workspace_id=workspace_id,
            page_id=other_page_id,
            version_id=other_history_id,
            created_at=now,
            payload=ContentPayload(b"reconcile base", block_map=block_map),
            parents=[],
        )
        archived_current_oid = snapshots.commit(
            workspace_id=workspace_id,
            page_id=other_page_id,
            version_id=other_id,
            created_at=now,
            payload=ContentPayload(b"other", block_map=block_map),
            parents=[archived_history_oid],
        )
        snapshots.set_head(workspace_id, other_page_id, archived_current_oid)
        restored.update(
            {other_history_id: archived_history_oid, other_id: archived_current_oid}
        )
        with engine.begin() as connection:
            for version_id in (other_history_id, other_id):
                connection.execute(
                    table.update()
                    .where(table.c.id == key(version_id))
                    .values(commit_oid=restored[version_id])
                )

        # The same populated traversal exercises the destructive second
        # revision. A corrupt SQL copy must abort before nullable DDL or body
        # removal, even though an earlier row has already verified successfully.
        with engine.begin() as connection:
            table = sa.Table(
                "context_page_version", sa.MetaData(), autoload_with=connection
            )
            connection.execute(
                table.update()
                .where(table.c.id == key(historical_id))
                .values(body_md="not the recorded Git bytes")
            )
        with pytest.raises(RuntimeError, match="verification failed"):
            command.upgrade(config, BODY_REVISION)
        assert not next(
            item
            for item in sa.inspect(engine).get_columns("context_page_version")
            if item["name"] == "body_md"
        )["nullable"]
        with engine.begin() as connection:
            connection.execute(
                table.update()
                .where(table.c.id == key(historical_id))
                .values(body_md=converted[historical_id]["body_md"])
            )
            connection.execute(
                table.update()
                .where(table.c.id == key(other_history_id))
                .values(block_map=[{"id": "not-the-archived-identity", "kind": "p"}])
            )
        # A matching body cannot detect identity loss: this independent guard
        # must also refuse before DDL, using the same migration/DB traversal.
        with pytest.raises(RuntimeError, match="verification failed"):
            command.upgrade(config, BODY_REVISION)
        assert not next(
            item
            for item in sa.inspect(engine).get_columns("context_page_version")
            if item["name"] == "body_md"
        )["nullable"]
        with engine.begin() as connection:
            connection.execute(
                table.update()
                .where(table.c.id == key(other_history_id))
                .values(block_map=block_map)
            )
        command.upgrade(config, BODY_REVISION)

        def stored_versions():
            with engine.connect() as connection:
                table = sa.Table(
                    "context_page_version", sa.MetaData(), autoload_with=connection
                )
                return {
                    UUID(str(row["id"])): row
                    for row in connection.execute(sa.select(table)).mappings()
                }

        released = stored_versions()
        assert released[historical_id]["body_md"] is None
        assert released[historical_id]["block_map"] == block_map
        assert released[other_history_id]["body_md"] is None
        assert released[other_history_id]["block_map"] is None
        with engine.connect() as connection:
            # JSON null also decodes to Python None; require an actual SQL
            # NULL so raw-column predicates observe the released map.
            assert connection.scalar(
                sa.select(table.c.block_map.is_(None)).where(
                    table.c.id == key(other_history_id)
                )
            )
        for item in (
            current_id,
            other_id,
            editing_id,
            branch_id,
            draft_id,
            discarded_id,
        ):
            assert released[item]["body_md"] == converted[item]["body_md"]
            assert released[item]["block_map"] == block_map
        assert {item: row["commit_oid"] for item, row in released.items()} == restored
        assert next(
            item
            for item in sa.inspect(engine).get_columns("context_page_version")
            if item["name"] == "body_md"
        )["nullable"]
        assert "context_page_version_body_stored_check" in {
            item["name"]
            for item in sa.inspect(engine).get_check_constraints("context_page_version")
        }
        with engine.begin() as connection:
            # pg8000 exposes PostgreSQL CHECK violations as ProgrammingError;
            # assert the named database constraint, independent of driver class.
            with pytest.raises(sa.exc.DBAPIError, match="body_stored_check"):
                with connection.begin_nested():
                    connection.execute(
                        table.update()
                        .where(table.c.id == key(editing_id))
                        .values(body_md=None)
                    )

        # Downgrade also validates the complete corpus before restoring any
        # row. Invalid mapping on one history must leave both NULL bodies and
        # the CHECK unchanged instead of partially repairing the database.
        with engine.begin() as connection:
            connection.execute(
                table.update()
                .where(table.c.id == key(other_history_id))
                .values(commit_oid=restored[historical_id])
            )
        with pytest.raises(RuntimeError, match="verification failed"):
            command.downgrade(config, REVISION)
        failed_restore = stored_versions()
        assert failed_restore[historical_id]["body_md"] is None
        assert failed_restore[other_history_id]["body_md"] is None
        with engine.begin() as connection:
            connection.execute(
                table.update()
                .where(table.c.id == key(other_history_id))
                .values(commit_oid=restored[other_history_id])
            )
        command.downgrade(config, REVISION)
        hydrated = stored_versions()
        assert all(
            hydrated[item]["body_md"] == original["body_md"]
            for item, original in converted.items()
        )
        assert hydrated[historical_id]["block_map"] == block_map
        assert hydrated[other_history_id]["block_map"] == block_map
        assert not next(
            item
            for item in sa.inspect(engine).get_columns("context_page_version")
            if item["name"] == "body_md"
        )["nullable"]
        command.upgrade(config, BODY_REVISION)
        rereleased = stored_versions()
        assert rereleased[historical_id]["body_md"] is None
        assert rereleased[historical_id]["block_map"] == block_map
        assert rereleased[other_history_id]["block_map"] is None
        assert {item: row["commit_oid"] for item, row in rereleased.items()} == restored
    finally:
        if dialect == "postgresql":
            drop_reflected_postgres_metadata(engine)
        engine.dispose()
