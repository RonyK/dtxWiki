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
from backend.models.context import ContextPage
from backend.scripts.seed_dev import seed_default_workspace
from backend.tests.utils.postgres import drop_reflected_postgres_metadata

PARENT = "00ba02774c4a"
REVISION = "74e5d4f912a6"
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
                        content_hash="unchanged-normalized-hash",
                        graph_status="linear",
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
        assert repo[converted[other_id]["commit_oid"]].parent_ids == []
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
    finally:
        if dialect == "postgresql":
            drop_reflected_postgres_metadata(engine)
        engine.dispose()
