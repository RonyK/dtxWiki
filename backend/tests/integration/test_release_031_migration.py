"""One populated 0.3.1 release upgrade per supported dialect.

Data written at the 0.2.3/0.2.4 schema goes through the one-way release
revision: Git snapshots, historical body release, MCP call lifecycle,
document identities and shared drafts. Assertions describe the final state.
"""

import json
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pygit2
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.context_versions.adapters.git_libgit2 import GitSnapshotStore
from backend.context_versions.application import ContextVersionWriter
from backend.context_versions.domain import ContentPayload
from backend.models import (
    ClientDelegatedTask,
    ContextPage,
    Job,
    LibraryItem,
    LintFinding,
    LintRun,
)
from backend.models.ask import AskSession, AskTurn
from backend.models.context import ContextPageVersion
from backend.models.coverage import KnowledgeCoverageGap
from backend.models.mcp_trace import MCPToolCall, MCPWriteIdempotency
from backend.models.review import ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import MCPClient
from backend.schemas.context_write import ReviewDecisionRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_review_disposition import context_version_review_metadata
from backend.services.review_service import ReviewService
from backend.tests.integration.test_context_review_publish import _seed_review
from backend.tests.utils.postgres import drop_reflected_postgres_metadata
from backend.utils.block_identity import (
    compute_block_map_with_ids,
    validated_block_map_for_version,
)
from backend.utils.content_hash import content_hash

PARENT = "00ba02774c4a"
REVISION = "c3d2e4f601ab"
GIT_COLUMNS = {
    "commit_oid",
    "content_format",
    "format_version",
    "media_type",
    "encoding",
    "content_byte_size",
}
LIFECYCLE_COLUMNS = {"actor_kind", "actor_id", "idempotency_key", "retryable"}
LIFECYCLE_INDEX = "ix_mcp_tool_call_actor_key_started"


def _stored_versions(engine):
    with engine.connect() as connection:
        table = sa.Table(
            "context_page_version", sa.MetaData(), autoload_with=connection
        )
        return {
            UUID(str(row["id"])): row
            for row in connection.execute(sa.select(table)).mappings()
        }


def _version_columns(engine):
    return {
        item["name"]: item
        for item in sa.inspect(engine).get_columns("context_page_version")
    }


def _seed_git_history(engine, workspace_id, key):
    """Published history with legacy block maps for the Git phases."""

    git = SimpleNamespace(
        page_id=uuid4(),
        other_page_id=uuid4(),
        now=datetime(2026, 11, 9, 1, 2, 3, 456789, tzinfo=UTC),
        body="# 원문\r\n\r\n끝 공백  \r\nNFD: é",
        block_map=[{"id": "preserved-identity", "kind": "p"}],
    )
    (
        git.current_id,
        git.historical_id,
        git.editing_id,
        git.other_id,
        git.other_history_id,
        git.branch_id,
        git.draft_id,
        git.discarded_id,
    ) = (uuid4() for _ in range(8))
    with engine.begin() as connection:
        connection.execute(
            ContextPage.__table__.insert(),
            [
                dict(
                    id=page,
                    workspace_id=workspace_id,
                    slug=slug,
                    title=title,
                    created_at=git.now,
                    updated_at=git.now,
                )
                for page, slug, title in (
                    (git.page_id, "git-source", "Git source"),
                    (git.other_page_id, "git-other", "Git other"),
                )
            ],
        )
        versions = sa.Table(
            "context_page_version", sa.MetaData(), autoload_with=connection
        )
        # Current is deliberately the lower version_no: the DB pointer wins,
        # while Git migration connects historical snapshots before it.
        git.rows = [
            (git.current_id, git.page_id, 1, "published_snapshot", git.body, None),
            (
                git.historical_id,
                git.page_id,
                2,
                "superseded_snapshot",
                "earlier\r\n",
                git.current_id,
            ),
            (git.editing_id, git.page_id, 3, "draft", "private work", git.current_id),
            (git.other_id, git.other_page_id, 1, "published_snapshot", "other", None),
            (
                git.other_history_id,
                git.other_page_id,
                2,
                "superseded_snapshot",
                "reconcile base",
                git.other_id,
            ),
            (
                git.branch_id,
                git.other_page_id,
                3,
                "draft",
                "branch work",
                git.other_history_id,
            ),
            (git.draft_id, git.page_id, 4, "draft", "pending work", git.editing_id),
            (
                git.discarded_id,
                git.page_id,
                5,
                "discarded",
                "discarded work",
                git.current_id,
            ),
        ]
        git.initial_maps = {}
        for version_id, owner, number, status, content, parent in git.rows:
            initial_map = deepcopy(git.block_map)
            if version_id in (git.current_id, git.editing_id):
                initial_map = compute_block_map_with_ids(
                    content,
                    previous_body_md=None,
                    previous_block_map=None,
                    identity_seed=str(version_id),
                )
                initial_map[0]["lines"] += 1  # Legacy cache drift.
            elif version_id in (git.other_id, git.draft_id, git.other_history_id):
                initial_map = None
            git.initial_maps[version_id] = deepcopy(initial_map)
            connection.execute(
                versions.insert().values(
                    id=key(version_id),
                    workspace_id=key(workspace_id),
                    page_id=key(owner),
                    version_no=number,
                    version_status=status,
                    body_md=content,
                    block_map=initial_map,
                    parent_version_id=key(parent) if parent else None,
                    content_hash=content_hash(content),
                    graph_status="branched"
                    if version_id == git.branch_id
                    else "linear",
                    reconcile_manifest=(
                        {"preserved": True} if version_id == git.branch_id else None
                    ),
                    rendering_profile="gfm_mermaid",
                    actor_type="human",
                    created_by_actor_kind="human",
                    created_by_actor_id="migration-user",
                    created_at=git.now,
                    last_saved_at=git.now,
                )
            )
        for owner, published in (
            (git.page_id, git.current_id),
            (git.other_page_id, git.other_id),
        ):
            latest = git.draft_id if owner == git.page_id else None
            connection.execute(
                ContextPage.__table__.update()
                .where(ContextPage.id == owner)
                .values(
                    current_version_id=latest or published,
                    latest_draft_version_id=latest,
                    current_published_version_id=published,
                )
            )
    with Session(engine) as session:
        git.review_id = _seed_review(
            session, workspace_id, git.page_id, version_id=git.editing_id
        ).id
        session.commit()
    git.before = _stored_versions(engine)
    return git


def _assert_git_history(engine, root, tmp_path, workspace_id, git):
    stored = _stored_versions(engine)
    repo = pygit2.Repository(str(root / f"{workspace_id}.git"))
    current_oid = stored[git.current_id]["commit_oid"]
    historical_oid = stored[git.historical_id]["commit_oid"]
    assert repo[historical_oid].parent_ids == []
    assert repo[current_oid].parent_ids == [pygit2.Oid(hex=historical_oid)]
    assert repo.references[f"refs/heads/context/{git.page_id}"].target == pygit2.Oid(
        hex=current_oid
    )
    assert repo[stored[git.other_id]["commit_oid"]].parent_ids == [
        pygit2.Oid(hex=stored[git.other_history_id]["commit_oid"])
    ]
    # SQL parents of committed versions follow their Git commit parents.
    git_parents = {
        git.current_id: git.historical_id,
        git.historical_id: None,
        git.other_id: git.other_history_id,
        git.other_history_id: None,
        git.branch_id: git.other_history_id,
    }
    # Released history and the closed branch draft keep body and map in Git only.
    released = (git.historical_id, git.other_history_id, git.branch_id)
    for version_id, owner, _number, _status, content, _parent in git.rows:
        row = stored[version_id]
        assert row["body_md"] == (None if version_id in released else content)
        if version_id in (git.current_id, git.other_id, git.editing_id, git.draft_id):
            # The live published version and its draft chain get a valid map
            # that keeps every existing block identity.
            assert (
                validated_block_map_for_version(row["block_map"], content)
                == row["block_map"]
            )
            if git.initial_maps[version_id] is not None:
                assert [entry["id"] for entry in row["block_map"]] == [
                    entry["id"] for entry in git.initial_maps[version_id]
                ]
        elif version_id in released:
            assert row["block_map"] is None
        else:
            assert row["block_map"] == git.initial_maps[version_id]
        assert (
            row["content_format"],
            row["format_version"],
            row["media_type"],
            row["encoding"],
        ) == ("markdown", 1, "text/markdown", "utf-8")
        if version_id not in git_parents:
            continue
        fields = ["content_hash", "last_saved_at", "created_at", "graph_status"]
        if version_id == git.branch_id:
            assert row["version_status"] == "closed"
        else:
            fields.append("version_status")
        for field in fields:
            assert row[field] == git.before[version_id][field]
        assert (
            UUID(str(row["parent_version_id"])) if row["parent_version_id"] else None
        ) == git_parents[version_id]
        commit = repo[row["commit_oid"]]
        assert commit.message == f"Context snapshot {version_id}\n"
        assert commit.author.email == "git@noreply.dtxwiki.com"
        assert commit.committer.offset == 0
        assert row["content_byte_size"] == len(content.encode("utf-8"))
        tree = repo[repo[commit.tree["context"].id][str(owner)].id]
        assert repo[tree["content.md"].id].data == content.encode("utf-8")
        # Released rows keep their exact map only in Git.
        archived_map = (
            git.initial_maps[version_id] if version_id in released else row["block_map"]
        )
        if archived_map is not None:
            assert json.loads(repo[tree["metadata.json"].id].data) == {
                "schema_version": 1,
                "block_map": archived_map,
            }
        else:
            assert "metadata.json" not in tree

    # The runtime writer produces the same commits for the same snapshots.
    runtime = GitSnapshotStore(tmp_path / "runtime-repositories")
    runtime_historical = runtime.commit(
        workspace_id=workspace_id,
        page_id=git.page_id,
        version_id=git.historical_id,
        created_at=git.now,
        payload=ContentPayload(
            b"earlier\r\n", block_map=git.initial_maps[git.historical_id]
        ),
        parents=[],
    )
    assert runtime_historical == historical_oid
    assert (
        runtime.commit(
            workspace_id=workspace_id,
            page_id=git.page_id,
            version_id=git.current_id,
            created_at=git.now,
            payload=ContentPayload(
                git.body.encode("utf-8"), block_map=stored[git.current_id]["block_map"]
            ),
            parents=[runtime_historical],
        )
        == current_oid
    )

    columns = _version_columns(engine)
    assert columns["body_md"]["nullable"] and columns["encoding"]["nullable"]
    assert columns["commit_oid"]["nullable"]
    assert not any(
        columns[name]["nullable"]
        for name in ("content_format", "format_version", "media_type")
    )
    with engine.begin() as connection:
        table = sa.Table(
            "context_page_version", sa.MetaData(), autoload_with=connection
        )
        # JSON null also decodes to Python None; require an actual SQL NULL so
        # raw-column predicates observe the released map.
        assert connection.scalar(
            sa.select(table.c.block_map.is_(None)).where(
                table.c.id == stored[git.historical_id]["id"]
            )
        )
        # Open drafts and discarded work stay SQL-only; the open review stays.
        assert not any(
            stored[version_id]["commit_oid"]
            for version_id in (git.editing_id, git.draft_id, git.discarded_id)
        )
        assert (
            connection.scalar(
                sa.select(ReviewItem.status).where(ReviewItem.id == git.review_id)
            )
            == "pending"
        )
        # pg8000 exposes PostgreSQL CHECK violations as ProgrammingError;
        # assert the named database constraint, independent of driver class.
        with pytest.raises(sa.exc.DBAPIError, match="body_stored_check"):
            with connection.begin_nested():
                connection.execute(
                    table.update()
                    .where(table.c.id == stored[git.discarded_id]["id"])
                    .values(body_md=None)
                )


def _parent_calls(connection):
    # SQLite reflects UUID storage as CHAR; restore UUID processors while
    # reflecting only the columns actually present at the parent revision.
    return sa.Table(
        "mcp_tool_call",
        sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid()),
        sa.Column("client_id", sa.Uuid()),
        sa.Column("request_id", sa.Uuid()),
        autoload_with=connection,
    )


def _seed_mcp_call(engine, workspace_id):
    """A call logged before the lifecycle columns existed."""

    call = SimpleNamespace(
        id=uuid4(),
        request_id=uuid4(),
        now=datetime(2026, 11, 10, 10, 0, tzinfo=UTC),
    )
    with Session(engine) as session:
        client = MCPClient(
            workspace_id=workspace_id,
            name="migration-client",
            client_type="system_managed",
            scopes=["*"],
            status="active",
        )
        session.add(client)
        session.flush()
        session.execute(
            _parent_calls(session.connection())
            .insert()
            .values(
                id=call.id,
                workspace_id=workspace_id,
                client_id=client.id,
                request_id=call.request_id,
                schema_version="llmwiki.mcp.v1",
                tool_name="patch_context_edit",
                input_summary={"page_id": "historical-page"},
                result_status="succeeded",
                result_summary="Historical call preserved.",
                result_detail={"page_id": "historical-page"},
                warnings=[],
                provider="unknown",
                model_id="unknown",
                params_summary={},
                client_self_reported=False,
                started_at=call.now,
                completed_at=call.now + timedelta(seconds=1),
                latency_ms=1000,
            )
        )
        session.commit()
    call.indexes = {
        index["name"] for index in sa.inspect(engine).get_indexes("mcp_tool_call")
    }
    return call


def _assert_mcp_call(engine, dialect, call):
    inspector = sa.inspect(engine)
    columns = {
        column["name"]: column for column in inspector.get_columns("mcp_tool_call")
    }
    assert all(columns[name]["nullable"] for name in LIFECYCLE_COLUMNS)
    indexes = {index["name"]: index for index in inspector.get_indexes("mcp_tool_call")}
    assert set(indexes) == call.indexes | {LIFECYCLE_INDEX}
    assert not indexes[LIFECYCLE_INDEX]["unique"]
    assert indexes[LIFECYCLE_INDEX]["column_names"] == [
        "workspace_id",
        "actor_kind",
        "actor_id",
        "idempotency_key",
        "started_at",
        "id",
    ]
    # Partial: unkeyed calls, the bulk of the log, carry no index entry.
    where = indexes[LIFECYCLE_INDEX]["dialect_options"][f"{dialect}_where"]
    assert "idempotency_key IS NOT NULL" in str(where)
    with Session(engine) as session:
        historical = session.get(MCPToolCall, call.id)
        assert historical.request_id == call.request_id
        assert historical.input_summary == {"page_id": "historical-page"}
        assert historical.result_summary == "Historical call preserved."
        assert historical.result_detail == {"page_id": "historical-page"}
        assert historical.completed_at == call.now + timedelta(seconds=1)
        assert all(getattr(historical, name) is None for name in LIFECYCLE_COLUMNS)


def _seed_locators(engine, dialect, workspace_id, key):
    """Stored maintenance, Ask and receipt locators in the retired formats."""

    loc = SimpleNamespace(now=datetime(2026, 11, 15, tzinfo=UTC))
    (
        loc.page_id,
        loc.version_id,
        loc.library_id,
        loc.task_id,
        loc.finding_id,
        loc.orphan_id,
        loc.library_finding_id,
        loc.receipt_id,
        loc.ask_session_id,
        loc.ask_turn_id,
        loc.gap_id,
        loc.augment_id,
        loc.missing_augment_id,
    ) = (uuid4() for _ in range(13))
    with Session(engine) as session:
        session.add(
            ContextPage(
                id=loc.page_id,
                workspace_id=workspace_id,
                slug="renamed-page",
                title="Page",
            )
        )
        session.flush()
        # Seed the parent revision's version columns, not the current mapper's.
        versions = sa.Table(
            "context_page_version", sa.MetaData(), autoload_with=session.connection()
        )
        session.execute(
            versions.insert().values(
                id=key(loc.version_id),
                workspace_id=key(workspace_id),
                page_id=key(loc.page_id),
                version_no=1,
                body_md="# Editing\n",
                content_hash=content_hash("# Editing\n"),
                version_status="editing",
                graph_status="linear",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="kept-author",
                editing_revision=1,
                rendering_profile="gfm_mermaid",
                created_at=loc.now,
                last_saved_at=loc.now,
            )
        )
        session.flush()
        session.get(ContextPage, loc.page_id).current_version_id = loc.version_id
        session.add(
            LibraryItem(
                id=loc.library_id,
                workspace_id=workspace_id,
                connector_type="url",
                uri="https://example.com/retained",
                title="Library",
            )
        )
        run = LintRun(
            workspace_id=workspace_id, trigger_type="manual", status="completed"
        )
        session.add(run)
        session.flush()
        mixed = {
            "concept_path": "/context/old-page.md",
            "nested": [
                {"page_id": str(loc.page_id), "concept_path": "/context/older-page.md"},
                {"concept_path": "/context/missing.md", "note": "retained evidence"},
                {"expected_concept_path": f"/library/{loc.library_id}.md"},
                {"page_id": str(loc.page_id), "note": "identity only"},
            ],
        }

        def finding(identity, target_type, target_id, rule_id, action, **extra):
            return LintFinding(
                id=identity,
                workspace_id=workspace_id,
                lint_run_id=run.id,
                target_type=target_type,
                target_id=target_id,
                rule_id=rule_id,
                suggested_action=action,
                **extra,
            )

        session.add_all(
            [
                ClientDelegatedTask(
                    id=loc.task_id,
                    workspace_id=workspace_id,
                    task_type="page_meta_refresh",
                    target_kind="context_page",
                    target_id=loc.page_id,
                    payload=mixed,
                    source="lint",
                    fingerprint="mixed",
                ),
                finding(
                    loc.finding_id,
                    "context_page",
                    loc.page_id,
                    "maintenance.page_meta_suggestion",
                    json.dumps(
                        {"concept_path": "/context/old-page.md", "aliases": ["Keep"]},
                        separators=(",", ":"),
                        indent=2,
                    ),
                    message_args=mixed,
                ),
                finding(
                    loc.library_finding_id,
                    "library_item",
                    loc.library_id,
                    "maintenance.import_batch_classification",
                    json.dumps(
                        {
                            "expected_concept_path": f"/library/{loc.library_id}.md",
                            "note": "Keep",
                        }
                    ),
                ),
                finding(
                    loc.orphan_id,
                    "context_page",
                    uuid4(),
                    "maintenance.page_meta_suggestion",
                    json.dumps(
                        {
                            "concept_path": "/context/renamed-page.md",
                            "aliases": ["Must not retarget"],
                        }
                    ),
                ),
                MCPWriteIdempotency(
                    id=loc.receipt_id,
                    workspace_id=workspace_id,
                    actor_kind="human",
                    actor_id="kept-author",
                    tool_name="begin_context_edit",
                    idempotency_key="receipt-key",
                    request_fingerprint="same-fingerprint",
                    result_payload={
                        "page_id": str(loc.page_id),
                        "version_id": str(loc.version_id),
                        "version_status": "editing",
                        "concept_path": "/context/old-page.md",
                        "stable_url": f"/p/{workspace_id}/{loc.page_id}",
                        "latest_draft_version_id": str(loc.version_id),
                        "body_md": "PRIVATE RECEIPT BODY",
                    },
                    created_at=loc.now,
                    expires_at=loc.now + timedelta(days=1),
                ),
            ]
        )
        session.add(
            AskSession(
                id=loc.ask_session_id,
                workspace_id=workspace_id,
                title="Kept Ask",
                actor_kind="human",
                placement_tree_node_id=session.scalar(
                    sa.select(WorkspaceTreeNode.id)
                    .where(WorkspaceTreeNode.workspace_id == workspace_id)
                    .limit(1)
                ),
            )
        )
        session.flush()
        session.add(
            AskTurn(
                id=loc.ask_turn_id,
                workspace_id=workspace_id,
                ask_session_id=loc.ask_session_id,
                turn_no=1,
                user_message="Kept question",
                assistant_message="Kept answer",
                retrieval_trace=[
                    {
                        "type": "context_page",
                        "id": str(loc.page_id),
                        "concept_path": "/context/old-page.md",
                        "title": "Kept title",
                        "score": "1.0",
                        "debug": "PRIVATE TRACE BODY",
                    },
                    {
                        "type": "library_item",
                        "id": str(loc.library_id),
                        "concept_path": "/library/old-label.md",
                        "title": "Kept library",
                    },
                ],
            )
        )
        session.add(
            KnowledgeCoverageGap(
                id=loc.gap_id,
                workspace_id=workspace_id,
                query_norm="retained query",
                surface="search_mcp",
                nearest_candidates=[
                    {
                        "kind": "context_page",
                        "id": str(loc.page_id),
                        "concept_path": "/context/old-page.md",
                        "title": "Keep candidate",
                    }
                ],
                rewrite_attempts=[
                    {
                        "candidates": [
                            {
                                "kind": "library_item",
                                "id": str(loc.library_id),
                                "concept_path": "/library/renamed.md",
                            }
                        ]
                    }
                ],
            )
        )
        session.add_all(
            finding(
                identity,
                "knowledge_coverage_gap",
                loc.gap_id,
                "maintenance.coverage_gap_verdict",
                json.dumps(
                    {
                        "verdict": "augment_existing",
                        "concept_path": concept_path,
                        "rationale": rationale,
                    }
                ),
            )
            for identity, concept_path, rationale in (
                (loc.augment_id, "/context/renamed-page.md", "Keep"),
                (loc.missing_augment_id, "/context/missing.md", "Missing"),
            )
        )
        session.commit()
    if dialect == "sqlite":
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO search_doc_fts "
                    "(context_header, body, source_kind, entity_id, workspace_id) "
                    "VALUES (:header, '', 'context_page', :id, :workspace)"
                ),
                {
                    "header": "Title: Page\nPath: /context/old-page.md\nTags: keep",
                    "id": str(loc.page_id),
                    "workspace": str(workspace_id),
                },
            )
    return loc


def _assert_locators(engine, dialect, workspace_id, loc):
    page_url = f"/p/{workspace_id}/{loc.page_id}"
    library_url = f"/workspaces/{workspace_id}/library/items/{loc.library_id}"
    with Session(engine) as session:
        payload = session.get(ClientDelegatedTask, loc.task_id).payload
        assert payload["page_id"] == str(loc.page_id)
        assert payload["stable_url"] == page_url
        assert payload["nested"][:3] == [
            {"page_id": str(loc.page_id), "stable_url": page_url},
            {"locator": "/context/missing.md", "note": "retained evidence"},
            {"expected_kind": "library_item", "expected_id": str(loc.library_id)},
        ]
        assert json.loads(
            session.get(LintFinding, loc.finding_id).suggested_action
        ) == {
            "page_id": str(loc.page_id),
            "stable_url": page_url,
            "aliases": ["Keep"],
        }
        assert json.loads(
            session.get(LintFinding, loc.library_finding_id).suggested_action
        ) == {
            "expected_kind": "library_item",
            "expected_id": str(loc.library_id),
            "note": "Keep",
        }
        assert session.get(LintFinding, loc.orphan_id) is None
        assert session.get(LintFinding, loc.missing_augment_id) is None
        assert json.loads(
            session.get(LintFinding, loc.augment_id).suggested_action
        ) == {
            "verdict": "augment_existing",
            "page_id": str(loc.page_id),
            "stable_url": page_url,
            "rationale": "Keep",
        }
        assert session.get(AskTurn, loc.ask_turn_id).retrieval_trace == [
            {
                "type": "context_page",
                "id": str(loc.page_id),
                "stable_url": page_url,
                "title": "Kept title",
                "score": "1.0",
                "debug": "PRIVATE TRACE BODY",
            },
            {
                "type": "library_item",
                "id": str(loc.library_id),
                "stable_url": library_url,
                "title": "Kept library",
            },
        ]
        gap = session.get(KnowledgeCoverageGap, loc.gap_id)
        assert gap.nearest_candidates == [
            {
                "kind": "context_page",
                "id": str(loc.page_id),
                "stable_url": page_url,
                "title": "Keep candidate",
            }
        ]
        assert gap.rewrite_attempts == [
            {
                "candidates": [
                    {
                        "kind": "library_item",
                        "id": str(loc.library_id),
                        "stable_url": library_url,
                    }
                ]
            }
        ]
        receipt = session.get(MCPWriteIdempotency, loc.receipt_id)
        assert receipt.request_fingerprint == "same-fingerprint"
        assert receipt.result_payload == {
            "page_id": str(loc.page_id),
            "version_id": str(loc.version_id),
            "version_status": "draft",
            "stable_url": page_url,
            "body_md": "PRIVATE RECEIPT BODY",
        }
        assert session.get(ContextPage, loc.page_id).current_version_id == (
            loc.version_id
        )
    with engine.connect() as connection:
        jobs = sa.Table("job", sa.MetaData(), autoload_with=connection)
        pages = sa.Table("context_page", sa.MetaData(), autoload_with=connection)
        assert {
            (row.target_ref["kind"], row.target_ref["id"])
            for row in connection.execute(
                sa.select(jobs.c.target_ref).where(
                    jobs.c.idempotency_key.like("locator-index:%")
                )
            )
        } == {
            ("context_page", str(UUID(str(page))))
            for page in connection.scalars(sa.select(pages.c.id))
        } | {("library_item", str(loc.library_id))}
        if dialect == "sqlite":
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT context_header FROM search_doc_fts "
                        "WHERE entity_id = :id"
                    ),
                    {"id": str(loc.page_id)},
                )
                == "Title: Page\nTags: keep"
            )


@pytest.mark.release_migration
@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_release_031_upgrade_traversal(tmp_path, dialect):
    url = (
        f"sqlite+pysqlite:///{tmp_path / 'release-031.sqlite'}"
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
    now = datetime(2026, 11, 14, tzinfo=UTC)
    page_id, actor_id, change_id, review_id = (uuid4() for _ in range(4))
    published_id, editing_id, rejected_id, cycle_id, shared_id, discarded_id = (
        uuid4() for _ in range(6)
    )
    block_map = [{"id": "retained-block", "kind": "paragraph"}]
    withdrawn_id, withdrawn_change, withdrawn_review = (uuid4() for _ in range(3))
    reconcile_id, reconcile_change, reconcile_review, original_decision = (
        uuid4() for _ in range(4)
    )

    def key(value):
        return value.hex if dialect == "sqlite" else value

    def page_reviews(connection, status):
        return (
            connection.execute(
                sa.select(reviews).where(
                    reviews.c.status == status,
                    reviews.c.change_set_id.in_(
                        sa.select(operations.c.change_set_id).where(
                            operations.c.target_id == key(page_id)
                        )
                    ),
                )
            )
            .mappings()
            .all()
        )

    try:
        command.upgrade(config, PARENT)
        with Session(engine) as session:
            workspace_id = seed_default_workspace(session).id
            session.commit()
        metadata = sa.MetaData()
        pages, versions, changes, operations, reviews = (
            sa.Table(name, metadata, autoload_with=engine)
            for name in (
                "context_page",
                "context_page_version",
                "change_set",
                "change_operation",
                "review_item",
            )
        )
        store = GitSnapshotStore(root)
        with engine.begin() as connection:
            connection.execute(
                pages.insert().values(
                    id=key(page_id),
                    workspace_id=key(workspace_id),
                    slug="mutable-draft",
                    title="Page",
                    page_type="concept",
                    tags=["tag"],
                    aliases=[],
                    lifecycle_status="published",
                    created_at=now,
                    updated_at=now,
                    stale_candidate=False,
                    is_public=False,
                )
            )
            connection.execute(
                changes.insert().values(
                    id=key(change_id),
                    workspace_id=key(workspace_id),
                    created_at=now,
                    updated_at=now,
                    reason="rejected draft",
                    trigger_type="manual",
                    actor_type="human",
                    actor_id=key(actor_id),
                    status="rejected",
                    risk_level="low",
                    cite_call_skipped=False,
                )
            )
            connection.execute(
                reviews.insert().values(
                    id=key(review_id),
                    workspace_id=key(workspace_id),
                    change_set_id=key(change_id),
                    created_at=now,
                    updated_at=now,
                    review_type="context_update",
                    status="rejected",
                    risk_level="low",
                )
            )
            connection.execute(
                operations.insert().values(
                    id=key(uuid4()),
                    workspace_id=key(workspace_id),
                    change_set_id=key(change_id),
                    created_at=now,
                    target_type="context_page",
                    target_id=key(page_id),
                    operation="update",
                    diff_ref=f"context_page_version:{rejected_id}",
                )
            )
            seed = [
                (published_id, None, "published_snapshot", "# Published\n"),
                (editing_id, published_id, "editing", "# Work\n"),
                (rejected_id, rejected_id, "draft", "# Rejected\n"),
                (cycle_id, None, "draft", "# Cycle\n"),
                (shared_id, editing_id, "draft", "# Shared\n"),
                (discarded_id, published_id, "discarded", "# Private discard\n"),
                (reconcile_id, published_id, "draft", "# Reconciliation\n"),
                (withdrawn_id, withdrawn_id, "draft", "# Withdrawn\n"),
            ]
            for number, (version_id, parent_id, status, body) in enumerate(seed, 1):
                connection.execute(
                    versions.insert().values(
                        id=key(version_id),
                        workspace_id=key(workspace_id),
                        page_id=key(page_id),
                        version_no=number,
                        parent_version_id=key(parent_id) if parent_id else None,
                        version_status=status,
                        body_md=body,
                        content_hash=content_hash(body),
                        graph_status="linear",
                        actor_type="human",
                        actor_id=key(actor_id),
                        created_by_actor_kind="human",
                        created_by_actor_id=str(actor_id),
                        rendering_profile="gfm_mermaid",
                        created_at=now + timedelta(minutes=number - 1),
                        last_saved_at=now + timedelta(minutes=number - 1),
                        editing_revision=1,
                        block_map=block_map,
                    )
                )
            connection.execute(
                versions.update()
                .where(versions.c.id == key(cycle_id))
                .values(parent_version_id=key(cycle_id))
            )
            connection.execute(
                changes.insert().values(
                    id=key(reconcile_change),
                    workspace_id=key(workspace_id),
                    created_at=now,
                    updated_at=now,
                    reason="old branch request",
                    trigger_type="manual",
                    actor_type="human",
                    actor_id=key(actor_id),
                    status="pending_review",
                    risk_level="high",
                    cite_call_skipped=False,
                )
            )
            connection.execute(
                reviews.insert().values(
                    id=key(reconcile_review),
                    workspace_id=key(workspace_id),
                    change_set_id=key(reconcile_change),
                    created_at=now,
                    updated_at=now,
                    review_type="context_reconciliation",
                    status="pending",
                    risk_level="high",
                )
            )
            connection.execute(
                operations.insert().values(
                    id=key(uuid4()),
                    workspace_id=key(workspace_id),
                    change_set_id=key(reconcile_change),
                    created_at=now,
                    target_type="context_page",
                    target_id=key(page_id),
                    operation="patch_draft_diverged_manifest",
                    diff_ref=f"context_page_version:{reconcile_id}",
                )
            )
            connection.execute(
                changes.insert().values(
                    id=key(withdrawn_change),
                    workspace_id=key(workspace_id),
                    created_at=now,
                    updated_at=now,
                    reason="withdrawn draft",
                    trigger_type="manual",
                    actor_type="human",
                    actor_id=key(actor_id),
                    status="withdrawn",
                    risk_level="low",
                    cite_call_skipped=False,
                )
            )
            connection.execute(
                reviews.insert().values(
                    id=key(withdrawn_review),
                    workspace_id=key(workspace_id),
                    change_set_id=key(withdrawn_change),
                    created_at=now,
                    updated_at=now,
                    review_type="context_update",
                    status="withdrawn",
                    risk_level="low",
                )
            )
            connection.execute(
                operations.insert().values(
                    id=key(uuid4()),
                    workspace_id=key(workspace_id),
                    change_set_id=key(withdrawn_change),
                    created_at=now,
                    target_type="context_page",
                    target_id=key(page_id),
                    operation="update",
                    diff_ref=f"context_page_version:{withdrawn_id}",
                )
            )
            decisions = sa.Table("review_decision", metadata, autoload_with=connection)
            connection.execute(
                decisions.insert().values(
                    id=key(original_decision),
                    workspace_id=key(workspace_id),
                    review_item_id=key(review_id),
                    created_at=now,
                    decision_type="reject",
                    actor_type="human",
                    actor_id=key(actor_id),
                    decision_reason="Original refusal",
                    resulting_review_status="rejected",
                    resulting_change_set_status="rejected",
                )
            )
            ledger = sa.Table(
                "mcp_write_idempotency", sa.MetaData(), autoload_with=connection
            )
            connection.execute(
                ledger.insert().values(
                    id=key(uuid4()),
                    workspace_id=key(workspace_id),
                    actor_kind="human",
                    actor_id=str(actor_id),
                    tool_name="begin_context_edit",
                    idempotency_key="existing-receipt",
                    request_fingerprint="unchanged-fingerprint",
                    result_payload={
                        "version_id": str(editing_id),
                        "version_status": "editing",
                        "latest_draft_version_id": str(editing_id),
                    },
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                )
            )
            connection.execute(
                pages.update()
                .where(pages.c.id == key(page_id))
                .values(
                    current_version_id=key(shared_id),
                    current_published_version_id=key(published_id),
                    latest_draft_version_id=key(shared_id),
                )
            )
        # The released parent can have old draft reminder jobs in flight.
        with Session(engine) as session:
            session.add_all(
                Job(
                    workspace_id=workspace_id,
                    job_type="context_reconciliation_reminder",
                    trigger_type="manual",
                    status=status,
                    target_ref={"review_item_id": str(reconcile_review)},
                    next_attempt_at=now,
                )
                for status in ("queued", "running")
            )
            session.commit()
        git = _seed_git_history(engine, workspace_id, key)
        call = _seed_mcp_call(engine, workspace_id)
        loc = _seed_locators(engine, dialect, workspace_id, key)

        # A missing repository root is refused before any DDL.
        with pytest.raises(RuntimeError, match="absolute.*Config.attributes"):
            command.upgrade(config, REVISION)
        assert not GIT_COLUMNS & _version_columns(engine).keys()
        # A stored hash that no longer matches its body is refused after the Git
        # snapshots are written. The failed upgrade keeps the parent schema and
        # every SQL body, and the corrected data upgrades over the same Git refs.
        config.attributes["context_repositories_root"] = root
        with engine.begin() as connection:
            connection.execute(
                versions.update()
                .where(versions.c.id == key(git.historical_id))
                .values(content_hash=content_hash("stale"))
            )
        with pytest.raises(RuntimeError, match="verification failed"):
            command.upgrade(config, REVISION)
        engine.dispose()
        assert not GIT_COLUMNS & _version_columns(engine).keys()
        assert _stored_versions(engine)[git.historical_id]["body_md"] == "earlier\r\n"
        with engine.begin() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == PARENT
            )
            connection.execute(
                versions.update()
                .where(versions.c.id == key(git.historical_id))
                .values(content_hash=git.before[git.historical_id]["content_hash"])
            )
        command.upgrade(config, REVISION)
        engine.dispose()
        _assert_git_history(engine, root, tmp_path, workspace_id, git)
        _assert_mcp_call(engine, dialect, call)
        _assert_locators(engine, dialect, workspace_id, loc)
        table = sa.Table("context_page_version", sa.MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            rows = {
                UUID(str(row["id"])): row
                for row in connection.execute(sa.select(table)).mappings()
            }
            published_oid = rows[published_id]["commit_oid"]
            assert published_oid is not None
            assert (
                store.read(
                    workspace_id=workspace_id,
                    page_id=page_id,
                    commit_oid=published_oid,
                    descriptor=ContentPayload(b"").descriptor,
                )
                == b"# Published\n"
            )
            assert (
                rows[editing_id]["version_status"]
                == rows[shared_id]["version_status"]
                == "draft"
            )
            assert UUID(str(rows[shared_id]["parent_version_id"])) == published_id
            assert rows[rejected_id]["version_status"] == "closed"
            assert rows[rejected_id]["body_md"] is None
            assert (
                store.read(
                    workspace_id=workspace_id,
                    page_id=page_id,
                    commit_oid=rows[rejected_id]["commit_oid"],
                    descriptor=ContentPayload(b"").descriptor,
                )
                == b"# Rejected\n"
            )
            assert store.read_parents(
                workspace_id=workspace_id,
                page_id=page_id,
                commit_oid=rows[rejected_id]["commit_oid"],
            ) == (published_oid,)
            assert UUID(str(rows[cycle_id]["parent_version_id"])) == rejected_id
            assert rows[discarded_id]["version_status"] == "discarded"
            assert rows[discarded_id]["body_md"] == "# Private discard\n"
            assert rows[shared_id]["page_metadata"]["title"] == "Page"
            pending_reviews = page_reviews(connection, "pending")
            assert len(pending_reviews) == 4
            inspections = [
                review
                for review in pending_reviews
                if (review["risk_signals"] or {})
                .get("migration_parent_repaired", {})
                .get("inspection_only")
            ]
            assert {
                review["risk_signals"]["migration_parent_repaired"]["version_id"]
                for review in inspections
            } == {str(rejected_id), str(withdrawn_id), str(cycle_id)}
            # The intent signal survives draft saves; the verdict is derived.
            assert all(
                review["risk_signals"]["caller_needs_review"] for review in inspections
            )
            normal = next(
                row
                for row in pending_reviews
                if UUID(str(row["id"])) == reconcile_review
            )
            assert normal["review_type"] == "context_update"
            assert (
                connection.scalar(
                    sa.select(operations.c.operation).where(
                        operations.c.change_set_id == key(reconcile_change)
                    )
                )
                == "update"
            )
            assert store.list_heads(workspace_id)[page_id] == published_oid
            ledger_row = (
                connection.execute(
                    sa.select(ledger).where(
                        ledger.c.idempotency_key == "existing-receipt"
                    )
                )
                .mappings()
                .one()
            )
            assert ledger_row["request_fingerprint"] == "unchanged-fingerprint"
            assert ledger_row["result_payload"] == {
                "version_id": str(editing_id),
                "version_status": "draft",
            }
            jobs = sa.Table("job", sa.MetaData(), autoload_with=connection)
            queued = (
                connection.execute(
                    sa.select(jobs).where(
                        jobs.c.idempotency_key == f"pr3-index:context_page:{page_id}"
                    )
                )
                .mappings()
                .one()
            )
            assert queued["job_type"] == "embed" and queued["status"] == "queued"
            assert queued["target_ref"] == {"kind": "context_page", "id": str(page_id)}

        writer = ContextVersionWriter(store, store)
        with Session(
            engine,
            info={"context_snapshot_store": store, "context_version_writer": writer},
        ) as session:
            reminders = session.scalars(
                sa.select(Job).where(Job.job_type == "context_reconciliation_reminder")
            ).all()
            assert len(reminders) == 2
            assert all(
                job.status == "completed" and job.next_attempt_at is None
                for job in reminders
            )
            service = ReviewService(context_version_writer=writer)
            ended_versions = session.scalars(
                sa.select(ContextPageVersion).where(
                    ContextPageVersion.id.in_((rejected_id, withdrawn_id))
                )
            ).all()
            before = context_version_review_metadata(
                session, ended_versions, workspace_id=workspace_id
            )
            assert (before[rejected_id].status, before[rejected_id].review_item_id) == (
                "rejected",
                review_id,
            )
            assert (
                before[withdrawn_id].status,
                before[withdrawn_id].review_item_id,
            ) == ("withdrawn", withdrawn_review)
            for inspection in inspections:
                acknowledged = service.apply_decision(
                    session=session,
                    workspace_id=workspace_id,
                    review_item_id=UUID(str(inspection["id"])),
                    request=ReviewDecisionRequest(decision_type="approve"),
                    actor_kind="human",
                    actor_id=str(actor_id),
                    actor_role="owner",
                    actor_provenance="browser_session",
                )
                assert acknowledged.status == "approved"
            assert (
                context_version_review_metadata(
                    session, ended_versions, workspace_id=workspace_id
                )
                == before
            )
            requested = service.apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=reconcile_review,
                request=ReviewDecisionRequest(
                    decision_type="request_changes",
                    decision_reason="Please inspect this draft",
                ),
                actor_kind="human",
                actor_id=str(actor_id),
                actor_role="owner",
                actor_provenance="browser_session",
            )
            assert requested.status == "changes_requested"
            session.commit()
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(reviews.c.status).where(reviews.c.id == key(review_id))
                )
                == "rejected"
            )
            assert (
                connection.scalar(
                    sa.select(decisions.c.decision_reason).where(
                        decisions.c.id == key(original_decision)
                    )
                )
                == "Original refusal"
            )
            assert connection.scalar(
                sa.select(pages.c.current_published_version_id).where(
                    pages.c.id == key(page_id)
                )
            ) == key(published_id)
        assert store.list_heads(workspace_id)[page_id] == published_oid

        assert "uq_cpv_current_editing_per_author" not in {
            item["name"]
            for item in sa.inspect(engine).get_indexes("context_page_version")
        }
        assert "context_version_parent" in sa.inspect(engine).get_table_names()
        # This data conversion intentionally has no reverse writer. A refused
        # downgrade must leave the completed forward state unchanged.
        with pytest.raises(RuntimeError, match="pre-upgrade dtxWiki backup"):
            command.downgrade(config, PARENT)
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == REVISION
            )
            assert page_reviews(connection, "pending") == []
    finally:
        if dialect == "postgresql":
            drop_reflected_postgres_metadata(engine)
        engine.dispose()
